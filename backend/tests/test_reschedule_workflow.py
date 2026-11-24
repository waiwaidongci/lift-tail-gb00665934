"""重排编排的端到端测试（sqlite 内存库 + 假缓存）。

运行方式（在安装 requirements 的环境中）：
    cd backend && python -m tests.test_reschedule_workflow
覆盖：停运段版本化、重试幂等不新增、更新停运后旧任务作废、
worker 落库版本守卫（旧结果晚到不覆盖新排班）。
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402

from src.models import database as db_module  # noqa: E402
from src.models.tables import datasets, metadata, reschedules  # noqa: E402
from src.services import reschedule_workflow as workflow  # noqa: E402


class FakeCache:
    def set(self, *args, **kwargs):
        return True


DATE = "2026-11-22"


def row(task_id, elevator, hour, minute, duration, wait):
    return {
        "task_id": task_id,
        "elevator_id": elevator,
        "started_at": f"{DATE} {hour:02d}:{minute:02d}",
        "duration_minutes": duration,
        "wait_minutes": wait,
    }


RECORDS = [
    row("T1", "B-HL01", 18, 0, 30, 5),
    row("T2", "B-HL01", 18, 10, 20, 4),
    row("T3", "B-HL02", 18, 5, 40, 10),
    row("M1", "B-HL01", 8, 0, 20, 0),
]


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok - {message}")


def setup_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    db_module.engine = engine
    workflow.engine = engine
    db_module.cache = FakeCache()
    with engine.begin() as connection:
        connection.execute(datasets.insert().values(
            id=1, name="demo", checksum="a" * 64, records=RECORDS, owner="seed", created=time.time(),
        ))
    return engine


def outage(elevator, start, end):
    return {"elevator_id": elevator, "start_at": start, "end_at": end}


def main():
    engine = setup_engine()
    body = SimpleNamespace

    print("test_idempotent_retry")
    request = body(dataset_id=1, plan_date=DATE, threshold_minutes=30)
    first = workflow.submit_reschedule(request, "analyst")
    second = workflow.submit_reschedule(request, "analyst")
    check(first["reused"] is False, "首次提交创建重排任务")
    check(second["reused"] is True and second["id"] == first["id"], "同版本同参数重试复用任务，不新增排班")
    with engine.connect() as connection:
        total = len(connection.execute(select(reschedules)).mappings().all())
    check(total == 1, "数据库中只有一条重排记录")

    print("test_worker_completes")
    job = workflow.claim_reschedule()
    check(job is not None and job[0] == first["id"], "worker 领取排队任务")
    from src.worker import recalculate  # noqa: E402

    recalculate(job)
    with engine.connect() as connection:
        saved = connection.execute(select(reschedules).where(reschedules.c.id == first["id"])).mappings().one()
    check(saved["state"] == "completed", "计算完成后落库为已完成")
    check(saved["result"]["metrics"]["签收趟数"] == 3, "结果包含三趟撤展任务")
    check(workflow.claim_reschedule() is None, "没有新的排队任务")

    print("test_outage_version_supersedes_queued")
    queued = workflow.submit_reschedule(
        body(dataset_id=1, plan_date=DATE, threshold_minutes=25), "analyst"
    )
    check(queued["reused"] is False, "不同阈值生成新任务")
    updated = workflow.replace_outages(
        body(dataset_id=1, plan_date=DATE, outages=[outage("B-HL01", "19:00", "19:30")]), "analyst"
    )
    check(updated["scope_version"] == 1, "保存停运段后版本号升到 v1")
    with engine.connect() as connection:
        stale = connection.execute(select(reschedules).where(reschedules.c.id == queued["id"])).mappings().one()
    check(stale["state"] == "superseded", "停运更新后排队中的旧版本任务直接作废")
    check(workflow.claim_reschedule() is None, "已作废任务不会被 worker 领取")

    print("test_running_result_guard")
    fresh = workflow.submit_reschedule(
        body(dataset_id=1, plan_date=DATE, threshold_minutes=25), "analyst"
    )
    check(fresh["scope_version"] == 1, "新任务绑定当前停运版本 v1")
    job2 = workflow.claim_reschedule()
    check(job2[0] == fresh["id"], "领取新版本任务")
    # 计算期间调度又更新了一次停运段（v2），v1 的任务被置为作废。
    workflow.replace_outages(
        body(dataset_id=1, plan_date=DATE, outages=[outage("B-HL01", "19:10", "19:40")]), "analyst"
    )
    from src.services.reschedule import reschedule as compute_plan  # noqa: E402

    plan = compute_plan(job2[2], job2[3])
    stored = workflow.finish_reschedule(job2[0], job2[1], job2[4], plan, None)
    check(stored is False, "旧版本结果晚到时被守卫拒绝，不覆盖新排班")
    with engine.connect() as connection:
        old = connection.execute(select(reschedules).where(reschedules.c.id == fresh["id"])).mappings().one()
    check(old["state"] == "superseded" and old["result"] is None, "旧任务标记作废且结果未写入")

    print("test_new_version_schedules_against_latest_outages")
    latest = workflow.submit_reschedule(
        body(dataset_id=1, plan_date=DATE, threshold_minutes=25), "analyst"
    )
    check(latest["scope_version"] == 2 and latest["reused"] is False, "v2 参数生成新任务")
    job3 = workflow.claim_reschedule()
    recalculate(job3)
    with engine.connect() as connection:
        done = connection.execute(select(reschedules).where(reschedules.c.id == latest["id"])).mappings().one()
    check(done["state"] == "completed", "新版本任务正常完成")
    check(done["result"]["plan_date"] == DATE, "结果绑定所选日期")

    print("test_retry_after_completion_reuses")
    again = workflow.submit_reschedule(
        body(dataset_id=1, plan_date=DATE, threshold_minutes=25), "analyst"
    )
    check(again["reused"] is True and again["id"] == latest["id"], "已完成任务的同参重试仍复用，不新增")

    print("test_clear_outages_keeps_version")
    # 清空全部停运段：范围版本必须继续 +1 且持久化，不能回退到 0 造成校验和碰撞。
    before_clear = workflow.list_outages(1, DATE)["scope_version"]
    cleared = workflow.replace_outages(body(dataset_id=1, plan_date=DATE, outages=[]), "analyst")
    check(cleared["scope_version"] == before_clear + 1, "清空停运段后版本号继续递增")
    check(workflow.list_outages(1, DATE)["scope_version"] == cleared["scope_version"], "空停运段下版本仍持久化")
    new_after_clear = workflow.submit_reschedule(
        body(dataset_id=1, plan_date=DATE, threshold_minutes=25), "analyst"
    )
    check(new_after_clear["reused"] is False and new_after_clear["scope_version"] == cleared["scope_version"],
          "清空后同阈值生成新任务（版本不同，未发生校验和碰撞）")

    print("\n全部编排测试通过。")


if __name__ == "__main__":
    main()
