"""HTTP 端到端测试：登录、停运段校验、重排提交、worker 计算、幂等与权限。

cd backend && PYTHONPATH=/tmp/pylibs:. python -m tests.test_reschedule_api
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_db_dir = tempfile.mkdtemp(prefix="reschedule-api-")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_dir}/app.db"
os.environ["SEED_DEMO"] = "false"
os.environ["ARTIFACTS_DIR"] = tempfile.mkdtemp(prefix="reschedule-artifacts-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import insert, select  # noqa: E402

from src.constants.enums import State  # noqa: E402
from src.main import app  # noqa: E402
from src.models import database as db_module  # noqa: E402
from src.models.database import engine  # noqa: E402
from src.models.tables import datasets, reschedules, users  # noqa: E402
from src.services.domain import sample_rows  # noqa: E402
from src.utils.security import protect  # noqa: E402


class FakeCache:
    def set(self, *args, **kwargs):
        return True

    def get(self, *args, **kwargs):
        return b"1"

    def ping(self):
        return True


db_module.cache = FakeCache()


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok - {message}")


def main():
    with TestClient(app) as client:
        with engine.begin() as connection:
            for username, role in (("analyst", "analyst"), ("viewer", "viewer")):
                connection.execute(insert(users).values(
                    username=username, role=role, password=protect(f"{username}-pw"),
                ))
            dataset_id = connection.execute(insert(datasets).values(
                name="sample.csv", checksum="b" * 64, records=sample_rows(),
                owner="seed", created=time.time(),
            )).inserted_primary_key[0]

        login = client.post("/login", json={"username": "analyst", "password": "analyst-pw"})
        check(login.status_code == 200, "分析员登录成功")
        token = login.json()["token"]
        viewer_login = client.post("/login", json={"username": "viewer", "password": "viewer-pw"})
        viewer_token = viewer_login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        viewer_headers = {"Authorization": f"Bearer {viewer_token}"}

        print("test_dates_and_empty_outages")
        dates = client.get(f"/reschedule/dates/{dataset_id}", headers=headers)
        check(dates.status_code == 200 and len(dates.json()["dates"]) == 4, "返回四个有撤展记录的日期")
        plan_date = dates.json()["dates"][-1]
        box = client.get(f"/reschedule/outages/{dataset_id}/{plan_date}", headers=headers)
        check(box.json()["scope_version"] == 0 and box.json()["outages"] == [], "初始无停运段且版本为 v0")

        print("test_outage_validation")
        bad = client.put("/reschedule/outages", headers=headers, json={
            "dataset_id": dataset_id, "plan_date": plan_date,
            "outages": [{"elevator_id": "B-HL99", "start_at": "19:00", "end_at": "20:00"}],
        })
        check(bad.status_code == 422, "未知货梯停运段被拒绝")
        overlap = client.put("/reschedule/outages", headers=headers, json={
            "dataset_id": dataset_id, "plan_date": plan_date,
            "outages": [
                {"elevator_id": "B-HL01", "start_at": "19:00", "end_at": "20:00"},
                {"elevator_id": "B-HL01", "start_at": "19:30", "end_at": "20:30"},
            ],
        })
        check(overlap.status_code == 422, "重叠停运段被拒绝")
        check(client.get("/reschedule/dates/99999", headers=headers).status_code == 404, "未知数据版本返回 404")

        print("test_outage_update_versions")
        saved = client.put("/reschedule/outages", headers=headers, json={
            "dataset_id": dataset_id, "plan_date": plan_date,
            "outages": [{"elevator_id": "B-HL01", "start_at": "19:00", "end_at": "19:30"}],
        })
        check(saved.status_code == 200 and saved.json()["scope_version"] == 1, "有效停运段保存为 v1")

        print("test_submit_and_process")
        submitted = client.post("/reschedule", headers=headers, json={
            "dataset_id": dataset_id, "plan_date": plan_date, "threshold_minutes": 30,
        })
        check(submitted.status_code == 202 and submitted.json()["reused"] is False, "重排任务入队")
        job_id = submitted.json()["id"]

        from src.services.reschedule_workflow import claim_reschedule  # noqa: E402
        from src.worker import recalculate  # noqa: E402

        job = claim_reschedule()
        recalculate(job)
        detail = client.get(f"/reschedule/{job_id}", headers=headers)
        check(detail.status_code == 200 and detail.json()["state"] == State.completed.value, "worker 完成计算并落库")
        result = detail.json()["result"]
        check(result["metrics"]["签收趟数"] == 10, "样例每日撤展 10 趟全部纳入")
        check(len(result["details"]) == 10, "逐趟输出新安排或未排原因")
        check(all("planned_start_at" in row for row in result["details"]), "每趟含新开工字段")
        unscheduled = [row for row in result["details"] if not row["scheduled"]]
        check(all(row["unscheduled_reason"] for row in unscheduled), "未排趟次均有原因")
        check(all(row["work_minutes"] > 0 for row in result["details"]), "实际搬运用时为工时减排队且为正")

        print("test_idempotent_retry_over_http")
        retry = client.post("/reschedule", headers=headers, json={
            "dataset_id": dataset_id, "plan_date": plan_date, "threshold_minutes": 30,
        })
        check(retry.json()["reused"] is True and retry.json()["id"] == job_id, "同参重试复用任务，不新增")
        with engine.connect() as connection:
            count = len(connection.execute(select(reschedules)).mappings().all())
        check(count == 1, "仍是一条重排记录")

        print("test_permissions")
        forbidden = client.put("/reschedule/outages", headers=viewer_headers, json={
            "dataset_id": dataset_id, "plan_date": plan_date, "outages": [],
        })
        check(forbidden.status_code == 403, "只读账号不能修改停运段")
        forbidden_queue = client.post("/reschedule", headers=viewer_headers, json={
            "dataset_id": dataset_id, "plan_date": plan_date, "threshold_minutes": 30,
        })
        check(forbidden_queue.status_code == 403, "只读账号不能提交重排")
        visible = client.get(f"/reschedule/{job_id}", headers=viewer_headers)
        check(visible.status_code == 200, "只读账号可查看排班结果")

        print("test_superseded_on_outage_change")
        second = client.post("/reschedule", headers=headers, json={
            "dataset_id": dataset_id, "plan_date": plan_date, "threshold_minutes": 20,
        })
        client.put("/reschedule/outages", headers=headers, json={
            "dataset_id": dataset_id, "plan_date": plan_date,
            "outages": [{"elevator_id": "B-HL02", "start_at": "20:00", "end_at": "20:30"}],
        })
        stale = client.get(f"/reschedule/{second.json()['id']}", headers=headers).json()
        check(stale["state"] == State.superseded.value, "停运更新后排队任务自动作废")
        listing = client.get("/reschedule", headers=headers)
        check(len(listing.json()) >= 2, "重排任务列表可查")

    print("\n全部 HTTP 端到端测试通过。")


if __name__ == "__main__":
    main()
