"""DFS 最优性暴力对照与性能测试。

cd backend && python3 -m tests.test_reschedule_optimal
"""
import itertools
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.reschedule import earliest_start, schedule_elevator  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok - {message}")


def brute_optimal(trips, outages):
    """全排列 + 全子集穷举，小实例下的参考最优解（含编号序列平局规则）。"""
    n = len(trips)
    best = None
    for size in range(n, -1, -1):
        size_feasible = False
        for combo in itertools.combinations(range(n), size):
            feasible_overs = []
            for perm in itertools.permutations(combo):
                cursor, ok, perm_over = 0.0, True, 0
                for i in perm:
                    arrival, work, is_over, _ = trips[i]
                    start = earliest_start(max(cursor, arrival), work, outages)
                    if start is None:
                        ok = False
                        break
                    cursor = start + work
                    perm_over += int(is_over)
                if ok:
                    feasible_overs.append(perm_over)
            if feasible_overs:
                size_feasible = True
                key = (-size, min(feasible_overs), sorted(trips[i][3] for i in combo))
                if best is None or key < best:
                    best = key
        if size_feasible:
            return best
    return best or (0, 0, [])


def dfs_key(trips, outages):
    entries, exact = schedule_elevator(trips, outages)
    chosen = [entry for entry in entries if "start" in entry]
    # 业务目标只有两级：最大完工数、并列时最少超限；编号序列仅用于结果确定性。
    return (
        -len(chosen),
        sum(1 for entry in chosen if trips[entry["index"]][2]),
    ), exact


def main():
    rng = random.Random(20261124)
    print("test_brute_force_equivalence")
    for case in range(150):
        n = rng.randrange(1, 8)
        trips = []
        for i in range(n):
            arrival = rng.randrange(0, 200)
            work = rng.randrange(10, 80)
            trips.append((arrival, work, work > 40, f"T{i}"))
        outages = []
        if rng.random() < 0.5:
            start = rng.randrange(0, 180)
            outages.append((start, start + rng.randrange(10, 30)))
        got, exact = dfs_key(trips, outages)
        check(exact, f"第 {case} 组小实例（n={n}）走精确求解")
        expect = brute_optimal(trips, outages)
        check(got == expect[:2], f"第 {case} 组 DFS 与暴力最优一致：{got}")

    print("test_realistic_scale_performance")
    # 样例台账单梯单日约 10 趟：小实例精确，大实例启发式，都需在 2 秒内完成且满足约束。
    for scale in (6, 8, 10, 14):
        rng2 = random.Random(scale)
        trips = [
            (rng2.randrange(0, 220), rng2.randrange(8, 60), rng2.random() < 0.3, f"T{i:02d}")
            for i in range(scale)
        ]
        started = time.perf_counter()
        entries, exact = schedule_elevator(trips, [(60, 80), (150, 165)])
        elapsed = time.perf_counter() - started
        if scale <= 8:
            check(exact, f"{scale} 趟实例走精确分支定界")
        else:
            check(not exact, f"{scale} 趟实例走启发式并显式标注")
        check(elapsed < 2, f"{scale} 趟实例耗时 {elapsed:.3f}s 低于 2s")
        check(all("start" in entry or "unscheduled_reason" in entry for entry in entries),
              f"{scale} 趟实例每趟都有去向")

    print("test_reproduced_case")
    # 回归：早期按到达固定顺序的 DFS 在该实例少排一趟。
    trips = [(114, 46, True, "T0"), (155, 19, False, "T1"), (30, 75, True, "T2"),
             (107, 31, False, "T3"), (193, 53, True, "T4"), (38, 72, True, "T5")]
    got, _ = dfs_key(trips, [(171, 183)])
    check(got[0] == -4, "回归实例可排 4 趟（任意作业顺序）")

    print("\n最优性与性能测试全部通过。")


if __name__ == "__main__":
    main()
