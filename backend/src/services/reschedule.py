"""撤展临时停梯的重排算法（纯函数，仅依赖标准库）。

口径：
- 仅处理选定日期 18:00–22:00 撤展卸货窗口（左闭右开）内的签收趟次。
- 每趟必须在到达排队点之后才能开工，开工时刻不早于到达时刻。
- 实际搬运用时 = 任务工时 - 排队分钟（排队不再占用新排班的梯位）。
- 任务绑定签收记录上的原货梯，同一货梯串行，相邻趟次不得重叠。
- 任何趟次的占用区间 [开工, 完工) 不得与该梯停运段相交，也不得跨越停运段。
- 22:00 收馆为硬截止，先最大化完工趟数，并列时按固定阈值让超限趟数最少。
"""
from datetime import datetime

TEARDOWN_START_HOUR = 18
WINDOW_MINUTES = 240
DEFAULT_THRESHOLD = 30
# 单梯精确搜索的单阶段节点预算；8 趟以内最坏情况约 60 万节点、亚秒级完成。
SEARCH_NODE_BUDGET = 800_000
# 趟数不超过该阈值时做分支定界精确求解；超过后组合爆炸，改用贪心启发式并在结果标注。
EXACT_TRIP_LIMIT = 8
MAX_OUTAGES_PER_REQUEST = 20


def parse_clock(value, label="时间"):
    """把 HH:MM 解析为相对 18:00 的分钟偏移。"""
    if not isinstance(value, str):
        raise ValueError(f"{label}须填写 HH:MM 格式。")
    try:
        hour, minute = value.strip().split(":")
        hour_i, minute_i = int(hour), int(minute)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{label}须填写 HH:MM 格式。") from error
    if not (0 <= minute_i <= 59) or not (TEARDOWN_START_HOUR <= hour_i <= 22):
        raise ValueError(f"{label}须落在撤展窗口 18:00–22:00 内。")
    offset = (hour_i - TEARDOWN_START_HOUR) * 60 + minute_i
    if not (0 <= offset <= WINDOW_MINUTES):
        raise ValueError(f"{label}须落在撤展窗口 18:00–22:00 内。")
    return offset


def _parse_plan_date(value):
    if not isinstance(value, str):
        raise ValueError("请选择需要重排的日期（YYYY-MM-DD）。")
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("重排日期无效，请使用 YYYY-MM-DD。") from error
    return parsed.strftime("%Y-%m-%d")


def validate_outages(outages, known_elevators):
    """校验停运段并按货梯分组，返回 {电梯: [(start, end), ...]}（已排序）。"""
    if not isinstance(outages, list):
        raise ValueError("停运时段须为数组。")
    if len(outages) > MAX_OUTAGES_PER_REQUEST:
        raise ValueError(f"单次最多提交 {MAX_OUTAGES_PER_REQUEST} 个停运时段。")
    grouped = {}
    for index, item in enumerate(outages):
        label = f"第 {index + 1} 个停运时段"
        if not isinstance(item, dict):
            raise ValueError(f"{label}格式有误。")
        elevator = str(item.get("elevator_id", "")).strip()
        if not elevator:
            raise ValueError(f"{label}缺少货梯编号。")
        if known_elevators and elevator not in known_elevators:
            raise ValueError(f"{label}的货梯 {elevator} 在所选日期没有签收记录。")
        start = parse_clock(item.get("start_at"), f"{label}开始时间")
        end = parse_clock(item.get("end_at"), f"{label}结束时间")
        if end <= start:
            raise ValueError(f"{label}结束时间须晚于开始时间。")
        grouped.setdefault(elevator, []).append((start, end))
    for elevator, windows in grouped.items():
        windows.sort()
        for previous, current in zip(windows, windows[1:]):
            if current[0] < previous[1]:
                raise ValueError(f"货梯 {elevator} 的停运时段存在重叠，请先合并后再提交。")
    return grouped


def _teardown_rows(rows, plan_date):
    selected = []
    for row in rows:
        started = row.get("started_at", "")
        try:
            parsed = datetime.fromisoformat(started)
        except (TypeError, ValueError):
            continue
        if parsed.strftime("%Y-%m-%d") == plan_date and TEARDOWN_START_HOUR <= parsed.hour < 22:
            selected.append((row, parsed))
    return selected


def earliest_start(ready, work, outages, deadline=WINDOW_MINUTES):
    """在停运段约束下，返回不早于 ready 的最早可行开工时刻；无法在截止前完工返回 None。"""
    start = ready
    if work <= 0:
        return None
    while True:
        if start + work > deadline + 1e-9:
            return None
        blocking = next(
            (window for window in outages if start < window[1] and start + work > window[0]),
            None,
        )
        if blocking is None:
            return start
        start = max(start, blocking[1])


def _build_greedy(trips, outages, deadline):
    """到达顺序与短作业优先两种贪心，取较优方案作为精确搜索超时或大实例的保底。"""
    candidates = []
    orders = [
        sorted(range(len(trips)), key=lambda i: (trips[i][0], trips[i][1], trips[i][3])),
        sorted(range(len(trips)), key=lambda i: (trips[i][1], trips[i][0], trips[i][3])),
    ]
    for order in orders:
        cursor = 0.0
        entries = []
        for index in order:
            arrival, work, over, task_id = trips[index]
            start = earliest_start(max(cursor, arrival), work, outages, deadline)
            if start is None:
                entries.append({"index": index, "unscheduled_reason": None})
                continue
            entries.append({"index": index, "start": start, "end": start + work})
            cursor = start + work
        candidates.append(_entries_key(entries, trips))
    return min(candidates, key=lambda item: item[0])[1]


def _entries_key(entries, trips):
    chosen = [entry for entry in entries if "start" in entry]
    count = len(chosen)
    over_count = sum(1 for entry in chosen if trips[entry["index"]][2])
    ids = sorted(trips[entry["index"]][3] for entry in chosen)
    return ((-count, over_count, ids), entries)


def _simulate_order(order, trips, outages, deadline):
    """按给定顺序串行模拟，返回 (entries, 完工数, 超限数)。"""
    cursor = 0.0
    entries = []
    over_count = 0
    for index in order:
        arrival, work, over, _ = trips[index]
        start = earliest_start(max(cursor, arrival), work, outages, deadline)
        if start is None:
            entries.append({"index": index, "unscheduled_reason": None})
            continue
        entries.append({"index": index, "start": start, "end": start + work})
        cursor = start + work
        over_count += int(over)
    return entries, sum(1 for entry in entries if "start" in entry), over_count


def heuristic_elevator(trips, outages, deadline=WINDOW_MINUTES):
    """大实例启发式：多起点贪心 + 相邻交换局部搜索，按两级目标取最优。"""
    count = len(trips)
    orders = [
        sorted(range(count), key=lambda i: (trips[i][0], trips[i][1], trips[i][3])),
        sorted(range(count), key=lambda i: (trips[i][1], trips[i][0], trips[i][3])),
        sorted(range(count), key=lambda i: (trips[i][0] + trips[i][1], trips[i][3])),
        sorted(range(count), key=lambda i: (int(trips[i][2]), trips[i][1], trips[i][0])),
    ]
    best = None
    for order in orders:
        entries, completed, over_count = _simulate_order(order, trips, outages, deadline)
        key = (-completed, over_count, sorted(trips[e["index"]][3] for e in entries if "start" in e))
        if best is None or key < best[0]:
            best = (key, entries, list(order))
    # 相邻交换局部搜索：尝试所有相邻位置交换，接受两级目标改善。
    order = best[2]
    improved = True
    rounds = 0
    while improved and rounds < 4:
        improved = False
        rounds += 1
        for position in range(len(order) - 1):
            candidate = order[:]
            candidate[position], candidate[position + 1] = candidate[position + 1], candidate[position]
            entries, completed, over_count = _simulate_order(candidate, trips, outages, deadline)
            key = (-completed, over_count,
                   sorted(trips[e["index"]][3] for e in entries if "start" in e))
            if key < best[0]:
                best = (key, entries, candidate)
                order = candidate
                improved = True
    return best[1], False


def _slot_bound(remaining, cursor, trips, outages, deadline):
    """逐槽上界：每次取能在最早时刻开工的趟次，得到可完工趟数的乐观上界。"""
    pending = set(remaining)
    current = cursor
    count = 0
    while pending:
        best = None
        for index in pending:
            arrival, work, _, _ = trips[index]
            start = earliest_start(max(current, arrival), work, outages, deadline)
            if start is not None and (best is None or start < best[0]):
                best = (start, index)
        if best is None:
            break
        pending.remove(best[1])
        current = best[0] + trips[best[1]][1]
        count += 1
    return count


def _search_best(trips, outages, deadline, fixed_count=None):
    """分支定界搜索。

    fixed_count 为 None 时求最大完工趟数；给定趟数时在恰好排入该数量的
    可行方案中最小化超限趟数。返回 (best, 预算是否耗尽, 节点数)。
    """
    count = len(trips)
    budget = SEARCH_NODE_BUDGET
    nodes = 0
    best = None

    def search(remaining, cursor, chosen_ids, over_count):
        nonlocal budget, nodes, best
        nodes += 1
        if nodes > budget:
            budget = -1
            return
        chosen_size = len(chosen_ids)
        if fixed_count is not None:
            # 趟数已固定：选多了直接返回，剩余趟数不足以补齐目标趟数时剪枝。
            if chosen_size > fixed_count or chosen_size + len(remaining) < fixed_count:
                return
            bound = fixed_count
        else:
            bound = chosen_size + _slot_bound(remaining, cursor, trips, outages, deadline)
            if best is not None and bound < -best[0][0]:
                return

        if not remaining:
            if fixed_count is not None and chosen_size != fixed_count:
                return
            key = (-chosen_size, over_count, sorted(trips[i][3] for i in chosen_ids))
            if best is None or key < best[0]:
                best = (key, tuple(chosen_ids))
            return

        feasible = []
        for index in remaining:
            arrival, work, _, _ = trips[index]
            start = earliest_start(max(cursor, arrival), work, outages, deadline)
            if start is not None:
                feasible.append((start, work, trips[index][3], index, start + work))
        feasible.sort()
        for _, _, _, index, next_cursor in feasible:
            search(
                tuple(i for i in remaining if i != index),
                next_cursor,
                (*chosen_ids, index),
                over_count + int(trips[index][2]),
            )
        for index in remaining:
            search(
                tuple(i for i in remaining if i != index),
                cursor,
                chosen_ids,
                over_count,
            )

    search(tuple(range(count)), 0.0, (), 0)
    return best, budget < 0, nodes


def schedule_elevator(trips, outages, deadline=WINDOW_MINUTES):
    """单梯调度：贪心初始解 + 两阶段分支定界求精确最优。

    每一步可从所有未处理趟次中任选一趟（满足到达与停运约束即可），
    能覆盖任意作业顺序，而不只是到达顺序。第一阶段最大化完工趟数，
    第二阶段在相同完工数下最小化超限趟数，再按任务编号序列取得确定性结果。
    trips: [(arrival_offset, work_minutes, is_over_limit, task_id), ...]
    返回 (entries, exact)，entries 覆盖该梯全部趟次；exact=False 表示启发式结果。
    """
    count = len(trips)
    if count > EXACT_TRIP_LIMIT:
        return heuristic_elevator(trips, outages, deadline)
    greedy_entries = _build_greedy(trips, outages, deadline)

    first, exhausted_first, _ = _search_best(trips, outages, deadline)
    if exhausted_first:
        return greedy_entries, False
    max_count = -first[0][0]

    second, exhausted_second, _ = _search_best(trips, outages, deadline, fixed_count=max_count)
    if exhausted_second:
        return greedy_entries, False
    best_choice = second[1]

    chosen_set = set(best_choice)
    entries = []
    cursor = 0.0
    for index in best_choice:
        arrival, work, _, _ = trips[index]
        start = earliest_start(max(cursor, arrival), work, outages, deadline)
        entries.append({"index": index, "start": start, "end": start + work})
        cursor = start + work
    for index in range(count):
        if index not in chosen_set:
            entries.append({"index": index, "unscheduled_reason": None})
    return entries, True


def _unscheduled_reason(trip, outages, deadline):
    arrival, work = trip[0], trip[1]
    if arrival + work > deadline + 1e-9:
        return "到达过晚，22:00 收馆前无法完工"
    if earliest_start(arrival, work, outages, deadline) is None:
        return "受停运时段阻隔，收馆前没有可用梯位"
    return "可用梯位不足，本趟未排入收馆前排班"


def _clock(plan_date, offset):
    total = TEARDOWN_START_HOUR * 60 + int(round(offset))
    return f"{plan_date} {total // 60:02d}:{total % 60:02d}"


def reschedule(rows, options):
    """依据签收记录与停运段重排选定日期的撤展搬运。"""
    threshold = options.get("threshold_minutes", DEFAULT_THRESHOLD)
    if type(threshold) not in (int, float) or not 1 <= threshold <= 480:
        raise ValueError("固定阈值无效，请填写 1 至 480 分钟之间的数值。")
    plan_date = _parse_plan_date(options.get("plan_date"))
    day_rows = _teardown_rows(rows, plan_date)
    if not day_rows:
        raise ValueError(f"{plan_date} 没有撤展卸货（18:00–22:00）的签收记录，请更换日期。")

    known_elevators = {row["elevator_id"].strip() for row, _ in day_rows}
    grouped_outages = validate_outages(options.get("outages", []), known_elevators)

    elevators = sorted(known_elevators)
    per_elevator = {elevator: [] for elevator in elevators}
    for row, parsed in day_rows:
        duration = row["duration_minutes"]
        wait = row["wait_minutes"]
        work = round(duration - wait, 4)
        if work <= 0:
            raise ValueError(
                f"任务 {row['task_id']} 的实际搬运用时（工时减排队）不大于 0，请核对排队登记。"
            )
        arrival = (parsed.hour - TEARDOWN_START_HOUR) * 60 + parsed.minute
        per_elevator[row["elevator_id"].strip()].append(
            (arrival, work, work > threshold, row["task_id"])
        )

    details = []
    elevator_schedules = []
    total = completed = over_scheduled = 0
    degraded = []
    for elevator in elevators:
        trips = per_elevator[elevator]
        outages = grouped_outages.get(elevator, [])
        entries, exact = schedule_elevator(trips, outages)
        if not exact:
            degraded.append(elevator)
        chosen = [entry for entry in entries if "start" in entry]
        chosen.sort(key=lambda entry: (entry["start"], trips[entry["index"]][3]))
        schedule_rows = []
        for entry in chosen:
            index = entry["index"]
            arrival, work, over, task_id = trips[index][:4]
            total += 1
            completed += 1
            over_scheduled += int(over)
            row = {
                "task_id": task_id,
                "elevator_id": elevator,
                "arrival_at": _clock(plan_date, arrival),
                "work_minutes": round(work, 2),
                "planned_start_at": _clock(plan_date, entry["start"]),
                "planned_end_at": _clock(plan_date, entry["end"]),
                "is_over_limit": over,
                "scheduled": True,
                "unscheduled_reason": "",
            }
            details.append(row)
            schedule_rows.append(row)
        skipped = [entry for entry in entries if "start" not in entry]
        for entry in skipped:
            index = entry["index"]
            arrival, work, over, task_id = trips[index][:4]
            total += 1
            details.append({
                "task_id": task_id,
                "elevator_id": elevator,
                "arrival_at": _clock(plan_date, arrival),
                "work_minutes": round(work, 2),
                "planned_start_at": "",
                "planned_end_at": "",
                "is_over_limit": over,
                "scheduled": False,
                "unscheduled_reason": _unscheduled_reason(trips[index], outages, WINDOW_MINUTES),
            })
        elevator_schedules.append({
            "elevator_id": elevator,
            "trips": len(trips),
            "scheduled": len(chosen),
            "unscheduled": len(skipped),
            "outages": [
                {"start_at": _clock(plan_date, start), "end_at": _clock(plan_date, end)}
                for start, end in outages
            ],
            "schedule": schedule_rows,
        })

    details.sort(key=lambda row: (row["elevator_id"], row["scheduled"] is False,
                                  row["planned_start_at"], row["task_id"]))
    unscheduled_total = total - completed
    headline = (
        f"{plan_date} 撤展 {total} 趟，收馆前可完工 {completed} 趟"
        f"（其中 {over_scheduled} 趟实际搬运用时超 {threshold:g} 分钟阈值），未排 {unscheduled_total} 趟。"
    )
    notes = [
        "仅重排所选日期 18:00–22:00 撤展窗口内的签收趟次，任务绑定原货梯，不跨梯调整。",
        "每趟开工不早于到达排队点时刻；实际搬运用时＝原任务工时－排队分钟，排队不再占用新梯位。",
        "同一货梯串行作业且相邻趟次不重叠；任何趟次不得与该梯停运时段相交或跨越停运段。",
        "22:00 收馆为硬截止：先最大化收馆前完工趟数，并列时按当前固定阈值让超限趟数最少。",
        "超限按实际搬运用时严格大于固定阈值判定，与装卸工时分析使用同一阈值口径。",
        "更新停运时段后会生成新版本重排；并发情况下旧版本结果自动作废，不覆盖最新排班。",
    ]
    for elevator in elevators:
        outages = grouped_outages.get(elevator, [])
        if outages:
            spans = "、".join(f"{start:g}–{end:g}" for start, end in outages)
            notes.append(f"货梯 {elevator} 停运 {spans} 分钟（相对 18:00 偏移）。")
    if degraded:
        notes.append(
            "货梯 " + "、".join(degraded) + f" 单梯趟数超过 {EXACT_TRIP_LIMIT} 趟，已采用多起点贪心与局部交换"
            "启发式求解（约束全部满足，但不保证完工趟数全局最优），建议人工复核未排趟次。"
        )

    return {
        "headline": headline,
        "plan_date": plan_date,
        "threshold_minutes": threshold,
        "metrics": {
            "签收趟数": total,
            "收馆前完工趟数": completed,
            "未排趟数": unscheduled_total,
            "完工趟中超限趟数": over_scheduled,
            "覆盖货梯数": len(elevators),
        },
        "details": details,
        "elevator_schedules": elevator_schedules,
        "notes": notes,
        "degraded": degraded,
    }


def available_dates(rows):
    """返回台账中存在撤展窗口记录的日期列表。"""
    dates = set()
    for row in rows:
        try:
            parsed = datetime.fromisoformat(row.get("started_at", ""))
        except (TypeError, ValueError):
            continue
        if TEARDOWN_START_HOUR <= parsed.hour < 22:
            dates.add(parsed.strftime("%Y-%m-%d"))
    return sorted(dates)
