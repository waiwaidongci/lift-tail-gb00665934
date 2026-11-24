"""撤展重排纯算法测试，仅依赖标准库：python -m tests.test_reschedule"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.reschedule import (  # noqa: E402
    WINDOW_MINUTES,
    earliest_start,
    reschedule,
    schedule_elevator,
    validate_outages,
)

DATE = "2026-11-22"


def row(task_id, elevator, hour, minute, duration, wait):
    return {
        "task_id": task_id,
        "elevator_id": elevator,
        "started_at": f"{DATE} {hour:02d}:{minute:02d}",
        "duration_minutes": duration,
        "wait_minutes": wait,
    }


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok - {message}")


def test_earliest_start():
    print("test_earliest_start")
    # 无停运段
    check(earliest_start(10, 20, []) == 10, "无停运段时按就绪时刻开工")
    # 任务与停运段相交 -> 顺延到停运结束
    check(earliest_start(10, 30, [(20, 40)]) == 40, "与停运段相交时顺延至停运结束")
    # 任务整体在停运段之后
    check(earliest_start(45, 10, [(20, 40)]) == 45, "停运段已结束时不受影响")
    # 任务整体在停运段之前且可完工
    check(earliest_start(5, 10, [(20, 40)]) == 5, "停运段之前可完工时保持原时刻")
    # 顺延后超出收馆
    check(earliest_start(200, 50, [(210, 220)]) is None, "跨越收馆截止时无解")
    # 连续两个停运段，需跳过两次
    check(earliest_start(10, 100, [(40, 60), (90, 110)]) == 110, "连续停运段逐段顺延")


def test_validate_outages():
    print("test_validate_outages")
    grouped = validate_outages([
        {"elevator_id": "B-HL01", "start_at": "19:00", "end_at": "19:30"},
        {"elevator_id": "B-HL01", "start_at": "20:00", "end_at": "20:15"},
    ], {"B-HL01"})
    check(grouped["B-HL01"] == [(60, 90), (120, 135)], "停运段按分钟偏移解析并排序")
    for bad, message in [
        ([{"elevator_id": "B-HL01", "start_at": "20:00", "end_at": "19:00"}], "结束早于开始"),
        ([{"elevator_id": "B-HL01", "start_at": "19:00", "end_at": "20:00"},
          {"elevator_id": "B-HL01", "start_at": "19:30", "end_at": "20:30"}], "停运段重叠"),
        ([{"elevator_id": "B-HL99", "start_at": "19:00", "end_at": "20:00"}], "未知货梯"),
        ([{"elevator_id": "B-HL01", "start_at": "17:00", "end_at": "18:30"}], "窗口外时间"),
    ]:
        try:
            validate_outages(bad, {"B-HL01"})
        except ValueError:
            check(True, f"拒绝非法停运段：{message}")
        else:
            raise AssertionError(f"应拒绝：{message}")


def test_basic_no_outage():
    print("test_basic_no_outage")
    rows = [
        row("T1", "B-HL01", 18, 0, 30, 5),    # work 25, arrival 0
        row("T2", "B-HL01", 18, 10, 20, 4),   # work 16, arrival 10
        row("T3", "B-HL02", 18, 5, 40, 10),   # work 30, arrival 5
    ]
    result = reschedule(rows, {"plan_date": DATE, "threshold_minutes": 30, "outages": []})
    check(result["metrics"]["签收趟数"] == 3, "三趟全部纳入重排")
    check(result["metrics"]["收馆前完工趟数"] == 3, "无停运段时全部完工")
    by_id = {item["task_id"]: item for item in result["details"]}
    check(by_id["T1"]["planned_start_at"] == f"{DATE} 18:00", "到达即可开工")
    check(by_id["T1"]["work_minutes"] == 25, "实际搬运＝工时减排队")
    check(by_id["T2"]["planned_start_at"] == f"{DATE} 18:25", "同梯串行，T2 等 T1 完工")
    check(by_id["T3"]["planned_start_at"] == f"{DATE} 18:05", "另一部梯按各自到达开工")
    check(by_id["T3"]["is_over_limit"] is False, "30 分钟实际搬运不超 30 阈值")


def test_arrival_release():
    print("test_arrival_release")
    # 同梯：T1 18:00 到，工时 20；T2 18:30 才到（到达 30）。
    rows = [
        row("T1", "B-HL01", 18, 0, 20, 0),
        row("T2", "B-HL01", 18, 30, 20, 0),
    ]
    result = reschedule(rows, {"plan_date": DATE, "outages": []})
    by_id = {item["task_id"]: item for item in result["details"]}
    check(by_id["T2"]["planned_start_at"] == f"{DATE} 18:30", "不能早于到达时刻开工")


def test_outage_skipping():
    print("test_outage_skipping")
    # T1 work 30 at arrival 0；停运 18:20-18:50（20-50）。
    rows = [row("T1", "B-HL01", 18, 0, 30, 0)]
    result = reschedule(rows, {
        "plan_date": DATE,
        "outages": [{"elevator_id": "B-HL01", "start_at": "18:20", "end_at": "18:50"}],
    })
    item = result["details"][0]
    check(item["scheduled"] and item["planned_start_at"] == f"{DATE} 18:50",
          "与停运段冲突时顺延到停运结束后开工")
    check(item["planned_end_at"] == f"{DATE} 19:20", "完工时刻随开工顺延")


def test_maximize_completed():
    print("test_maximize_completed")
    # 同梯三个短任务（各 40 分钟）+ 一个 170 分钟长任务：
    # 三个短任务共 120 分钟可完工 3 趟；带长任务最多 2 趟（170+40=210，再加一趟超 240）。
    rows = [
        row("LONG", "B-HL01", 18, 0, 170, 0),
        row("S1", "B-HL01", 18, 0, 40, 0),
        row("S2", "B-HL01", 18, 0, 40, 0),
        row("S3", "B-HL01", 18, 0, 40, 0),
    ]
    result = reschedule(rows, {"plan_date": DATE, "outages": []})
    check(result["metrics"]["收馆前完工趟数"] == 3, "优先最大化完工趟数（放弃长任务）")
    unscheduled = [item for item in result["details"] if not item["scheduled"]]
    check(len(unscheduled) == 1 and unscheduled[0]["task_id"] == "LONG", "长任务被舍弃")
    check(unscheduled[0]["unscheduled_reason"], "未排趟次给出原因")


def test_over_limit_tie_break():
    print("test_over_limit_tie_break")
    # 收馆前只能完成 1 趟：一趟超限（work 40），一趟不超限（work 20），同时到达。
    rows = [
        row("OVER", "B-HL01", 21, 30, 40, 0),   # arrival 210, end 250 > 240 alone? 210+40=250 -> none feasible
        row("OK", "B-HL01", 21, 30, 20, 0),
    ]
    # OVER 单独也无法完工，应被标记为到达过晚；OK 可完工。
    result = reschedule(rows, {"plan_date": DATE, "threshold_minutes": 30, "outages": []})
    check(result["metrics"]["收馆前完工趟数"] == 1, "仅可完工一趟")
    by_id = {item["task_id"]: item for item in result["details"]}
    check(by_id["OK"]["scheduled"] and by_id["OVER"]["scheduled"] is False, "选择可完工趟次")
    check("收馆" in by_id["OVER"]["unscheduled_reason"], "到达过晚原因明确")

    # 并列场景：两趟单独都能在收馆前完工，但梯位只够一趟，一个超限一个不超限。
    rows2 = [
        row("OVER2", "B-HL01", 20, 50, 60, 0),   # arrival 170, work 60 -> 230 feasible
        row("OK2", "B-HL01", 20, 50, 40, 0),     # arrival 170, work 40 -> 210 feasible
    ]
    result2 = reschedule(rows2, {"plan_date": DATE, "threshold_minutes": 50, "outages": []})
    check(result2["metrics"]["收馆前完工趟数"] == 1, "梯位只够一趟")
    check(result2["metrics"]["完工趟中超限趟数"] == 0, "并列时优先不超限趟次")
    by_id2 = {item["task_id"]: item for item in result2["details"]}
    check(by_id2["OK2"]["scheduled"] and not by_id2["OVER2"]["scheduled"], "选中阈值内趟次")
    check("梯位不足" in by_id2["OVER2"]["unscheduled_reason"], "可单独完工但落选时给梯位不足原因")


def test_outage_blocks_fully():
    print("test_outage_blocks_fully")
    rows = [row("T1", "B-HL01", 21, 40, 30, 0)]  # arrival 220, work 30 -> 250
    # 21:50-22:00 停运（230-240），即便不停运也来不及，属到达过晚
    result = reschedule(rows, {"plan_date": DATE, "outages": []})
    check(not result["details"][0]["scheduled"], "收馆前来不及的趟次不排")

    rows2 = [row("T2", "B-HL01", 21, 0, 50, 0)]  # arrival 180, work 50 -> 230 alone feasible
    result2 = reschedule(rows2, {
        "plan_date": DATE,
        "outages": [{"elevator_id": "B-HL01", "start_at": "21:10", "end_at": "22:00"}],
    })
    check(not result2["details"][0]["scheduled"], "停运段阻隔时不排")
    check("停运" in result2["details"][0]["unscheduled_reason"], "给出停运阻隔原因")


def test_multi_elevator_independent():
    print("test_multi_elevator_independent")
    rows = [
        row(f"A{i}", "B-HL01", 18, 0, 60, 0) for i in range(4)  # 4*60=240 刚好
    ] + [
        row(f"B{i}", "B-HL02", 18, 0, 60, 0) for i in range(4)
    ]
    result = reschedule(rows, {"plan_date": DATE, "outages": []})
    check(result["metrics"]["收馆前完工趟数"] == 8, "两部梯互不影响，各排 4 趟")
    schedule = result["elevator_schedules"]
    check({item["elevator_id"] for item in schedule} == {"B-HL01", "B-HL02"}, "按梯分组输出")


def test_other_periods_ignored():
    print("test_other_periods_ignored")
    rows = [
        row("NIGHT", "B-HL01", 18, 0, 20, 0),
        {**row("MORNING", "B-HL01", 8, 0, 20, 0)},
        {**row("NOON", "B-HL01", 12, 0, 20, 0)},
    ]
    result = reschedule(rows, {"plan_date": DATE, "outages": []})
    check(result["metrics"]["签收趟数"] == 1, "仅撤展窗口趟次参与重排")
    try:
        reschedule([row("X", "B-HL01", 8, 0, 20, 0)], {"plan_date": DATE, "outages": []})
    except ValueError:
        check(True, "所选日期无撤展记录时报错")
    else:
        raise AssertionError("无撤展记录应报错")


def test_scheduler_deterministic():
    print("test_scheduler_deterministic")
    trips = [(0, 25, False, "T1"), (10, 16, False, "T2"), (5, 45, True, "T3")]
    first, _ = schedule_elevator(trips, [], 30)
    second, _ = schedule_elevator(trips, [], 30)
    check(first == second, "同一输入重复计算结果稳定")
    check(WINDOW_MINUTES == 240, "撤展窗口固定 240 分钟")


def main():
    tests = [
        test_earliest_start,
        test_validate_outages,
        test_basic_no_outage,
        test_arrival_release,
        test_outage_skipping,
        test_maximize_completed,
        test_over_limit_tie_break,
        test_outage_blocks_fully,
        test_multi_elevator_independent,
        test_other_periods_ignored,
        test_scheduler_deterministic,
    ]
    for test in tests:
        test()
    print(f"\n全部 {len(tests)} 组测试通过。")


if __name__ == "__main__":
    main()
