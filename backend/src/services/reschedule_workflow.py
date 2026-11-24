"""撤展停梯重排的编排：停运段版本化、幂等入队、并发守卫、worker 领取与落库。"""
import hashlib
import json
import time

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update

from src.constants.enums import State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import datasets, outage_scopes, outage_windows, reschedules
from src.services.reschedule import available_dates, reschedule, validate_outages


def _require_dataset(connection, dataset_id):
    dataset = fetch_one(connection, datasets, dataset_id)
    if dataset is None:
        raise HTTPException(404, '数据版本不存在。')
    return dataset


def _scope_version(connection, dataset_id, plan_date):
    """读取范围版本；没有任何停运段编辑记录时为 0（含已清空的情况，版本仍持久化）。"""
    row = connection.execute(
        select(outage_scopes.c.version).where(
            outage_scopes.c.dataset_id == dataset_id,
            outage_scopes.c.plan_date == plan_date,
        )
    ).scalar()
    return row or 0


def list_outages(dataset_id, plan_date):
    with engine.connect() as connection:
        _require_dataset(connection, dataset_id)
        scope_version = _scope_version(connection, dataset_id, plan_date)
        rows = connection.execute(
            select(outage_windows).where(
                outage_windows.c.dataset_id == dataset_id,
                outage_windows.c.plan_date == plan_date,
            ).order_by(outage_windows.c.elevator_id, outage_windows.c.start_at)
        ).mappings().all()
        return {
            'dataset_id': dataset_id,
            'plan_date': plan_date,
            'scope_version': scope_version,
            'outages': [
                {
                    'id': row['id'],
                    'elevator_id': row['elevator_id'],
                    'start_at': row['start_at'],
                    'end_at': row['end_at'],
                }
                for row in rows
            ],
        }


def replace_outages(body, actor):
    """整组替换某日停运段；每次有效更新让该范围的版本号 +1。"""
    now = time.time()
    with engine.begin() as connection:
        dataset = _require_dataset(connection, body.dataset_id)
        plan_date = body.plan_date.strip()
        known = set(available_dates(dataset['records']))
        if plan_date not in known:
            raise ValueError('所选日期没有撤展卸货签收记录，请先核对日期。')
        elevators = {
            row['elevator_id'].strip()
            for row in dataset['records']
            if row.get('started_at', '').startswith(plan_date)
        }
        outages = [
            item if isinstance(item, dict)
            else {'elevator_id': item.elevator_id, 'start_at': item.start_at, 'end_at': item.end_at}
            for item in body.outages
        ]
        validate_outages(outages, elevators)

        previous_version = _scope_version(connection, body.dataset_id, plan_date)
        version = previous_version + 1
        # 范围版本独立持久化：即使本次清空全部停运段，版本号也不会回退。
        connection.execute(
            outage_scopes.delete().where(
                outage_scopes.c.dataset_id == body.dataset_id,
                outage_scopes.c.plan_date == plan_date,
            )
        )
        connection.execute(insert(outage_scopes).values(
            dataset_id=body.dataset_id, plan_date=plan_date, version=version,
            owner=actor, updated=now,
        ))
        connection.execute(
            outage_windows.delete().where(
                outage_windows.c.dataset_id == body.dataset_id,
                outage_windows.c.plan_date == plan_date,
            )
        )
        for item in outages:
            connection.execute(insert(outage_windows).values(
                dataset_id=body.dataset_id, plan_date=plan_date,
                elevator_id=item['elevator_id'].strip(), start_at=item['start_at'].strip(),
                end_at=item['end_at'].strip(), version=version, owner=actor, updated=now,
            ))
        # 新版本生效：尚未完成的同范围重排任务全部作废，旧结果晚到也不能覆盖新排班。
        connection.execute(
            update(reschedules).where(
                reschedules.c.dataset_id == body.dataset_id,
                reschedules.c.plan_date == plan_date,
                reschedules.c.scope_version < version,
                or_(reschedules.c.state == State.queued.value,
                    reschedules.c.state == State.running.value),
            ).values(state=State.superseded.value, finished=now)
        )
        add_event(connection, actor, 'outage', version, now)
    return {'dataset_id': body.dataset_id, 'plan_date': plan_date,
            'scope_version': version, 'outages': outages}


def _request_checksum(dataset_id, plan_date, threshold, outages, scope_version):
    payload = json.dumps(
        {'dataset_id': dataset_id, 'plan_date': plan_date, 'threshold_minutes': threshold,
         'outages': outages, 'scope_version': scope_version},
        sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def submit_reschedule(body, actor):
    now = time.time()
    with engine.begin() as connection:
        dataset = _require_dataset(connection, body.dataset_id)
        plan_date = body.plan_date.strip()
        threshold = body.threshold_minutes
        if type(threshold) not in (int, float) or not 1 <= threshold <= 480:
            raise ValueError('固定阈值无效，请填写 1 至 480 分钟之间的数值。')
        if plan_date not in set(available_dates(dataset['records'])):
            raise ValueError('所选日期没有撤展卸货签收记录，请先核对日期。')
        scope_version = _scope_version(connection, body.dataset_id, plan_date)
        snapshot = [
            {'elevator_id': row['elevator_id'], 'start_at': row['start_at'], 'end_at': row['end_at']}
            for row in connection.execute(
                select(outage_windows).where(
                    outage_windows.c.dataset_id == body.dataset_id,
                    outage_windows.c.plan_date == plan_date,
                ).order_by(outage_windows.c.elevator_id, outage_windows.c.start_at)
            ).mappings()
        ]
        checksum = _request_checksum(body.dataset_id, plan_date, threshold, snapshot, scope_version)
        # 幂等：同一版本、同一参数的重试直接复用任务，不新增排班记录。
        existing = connection.execute(
            select(reschedules).where(reschedules.c.request_checksum == checksum).limit(1)
        ).mappings().one_or_none()
        if existing is not None:
            return {'id': existing['id'], 'state': existing['state'], 'reused': True,
                    'scope_version': scope_version}

        connection.execute(
            update(reschedules).where(
                reschedules.c.dataset_id == body.dataset_id,
                reschedules.c.plan_date == plan_date,
                reschedules.c.scope_version < scope_version,
                or_(reschedules.c.state == State.queued.value,
                    reschedules.c.state == State.running.value),
            ).values(state=State.superseded.value, finished=now)
        )
        key = connection.execute(insert(reschedules).values(
            dataset_id=body.dataset_id, plan_date=plan_date, threshold_minutes=threshold,
            outage_snapshot=snapshot, scope_version=scope_version, request_checksum=checksum,
            state=State.queued.value, owner=actor, created=now,
        )).inserted_primary_key[0]
        add_event(connection, actor, 'reschedule', key, now)
    return {'id': key, 'state': State.queued.value, 'reused': False, 'scope_version': scope_version}


def claim_reschedule():
    now = time.time()
    claimable = or_(
        reschedules.c.state == State.queued.value,
        and_(reschedules.c.state == State.running.value, reschedules.c.started < now - 120),
    )
    with engine.begin() as connection:
        job = connection.execute(
            select(reschedules).where(claimable).order_by(reschedules.c.id).limit(1)
        ).mappings().one_or_none()
        if job is None:
            return None
        key = job['id']
        claimed = connection.execute(
            update(reschedules).where(reschedules.c.id == key, claimable)
            .values(state=State.running.value, started=now)
        )
        if claimed.rowcount != 1:
            return None
        dataset = fetch_one(connection, datasets, job['dataset_id'])
        return key, now, dataset['records'], {
            'plan_date': job['plan_date'],
            'threshold_minutes': job['threshold_minutes'],
            'outages': job['outage_snapshot'],
        }, job['scope_version']


def finish_reschedule(key, claim, scope_version, result, error):
    now = time.time()
    state = State.failed.value if error else State.completed.value
    with engine.begin() as connection:
        if error:
            saved = connection.execute(
                update(reschedules).where(
                    reschedules.c.id == key, reschedules.c.started == claim,
                    reschedules.c.state == State.running.value,
                ).values(state=state, error=error, finished=now)
            )
        else:
            # 落库前再校验版本：计算期间停运段已更新时，旧结果只能作废，绝不覆盖新排班。
            saved = connection.execute(
                update(reschedules).where(
                    reschedules.c.id == key, reschedules.c.started == claim,
                    reschedules.c.state == State.running.value,
                    reschedules.c.scope_version == scope_version,
                ).values(state=state, result=result, finished=now)
            )
            if not saved.rowcount:
                connection.execute(
                    update(reschedules).where(
                        reschedules.c.id == key, reschedules.c.state == State.running.value,
                    ).values(state=State.superseded.value, finished=now)
                )
                add_event(connection, 'worker', 'superseded', key, now)
                return False
        if saved.rowcount:
            add_event(connection, 'worker', state, key, now)
        return bool(saved.rowcount)


def list_reschedules():
    with engine.connect() as connection:
        rows = connection.execute(
            select(reschedules).order_by(reschedules.c.id.desc()).limit(100)
        ).mappings().all()
        return [dict(row) for row in rows]
