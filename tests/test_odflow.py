"""odflow 单元测试：CSV 解析校验 + 全趟分配可行性 + 站对人次上下界。

关键正确性保障：对小规模随机情形，用暴力枚举所有可行分配得到的
上下界与最大流实现交叉验证。
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import odflow
from app.odflow import (combination_problems, is_feasible, pair_range,
                        parse_stop_totals, parse_verified)

SEQS = [1, 2, 3, 4]


# ---------------- CSV 解析：逐停站上下车总数 ----------------

def test_parse_stop_totals_ok():
    csv = ("stop_sequence,boardings,alightings\n"
           "1,15,0\n2,10,3\n3,5,6\n4,0,21\n").encode()
    margins, errors = parse_stop_totals(csv, SEQS)
    assert errors == []
    assert margins == [(1, 15, 0), (2, 10, 3), (3, 5, 6), (4, 0, 21)]


def test_parse_stop_totals_noncontiguous_sequences():
    # 停站序号不必连续，按班次实际序号覆盖即可
    csv = ("stop_sequence,boardings,alightings\n"
           "10,2,0\n20,0,1\n30,0,1\n").encode()
    margins, errors = parse_stop_totals(csv, [10, 20, 30])
    assert errors == []
    assert margins == [(10, 2, 0), (20, 0, 1), (30, 0, 1)]


def test_parse_stop_totals_bad_number():
    csv = ("stop_sequence,boardings,alightings\n"
           "1,-1,0\n2,10,3\n3,5,6\n4,0,21\n").encode()
    margins, errors = parse_stop_totals(csv, SEQS)
    assert margins is None
    assert any(e["field"] == "boardings" and e["row"] == 2 for e in errors)


def test_parse_stop_totals_unknown_and_duplicate_and_missing():
    csv = ("stop_sequence,boardings,alightings\n"
           "1,15,0\n1,15,0\n9,1,0\n").encode()
    margins, errors = parse_stop_totals(csv, SEQS)
    assert margins is None
    msgs = [e["message"] for e in errors]
    assert any("重复" in m for m in msgs)
    assert any("不属于该班次" in m for m in msgs)
    assert any("缺少停站序号" in m for m in msgs)  # 2、3、4 未覆盖


def test_parse_stop_totals_header_and_encoding():
    margins, errors = parse_stop_totals("stop_sequence,boardings\n".encode(), SEQS)
    assert margins is None and any("缺少必备列" in e["message"] for e in errors)
    margins, errors = parse_stop_totals(b"\xff\xfe\x00", SEQS)
    assert margins is None and any("UTF-8" in e["message"] for e in errors)
    margins, errors = parse_stop_totals(
        "stop_sequence,boardings,alightings\n".encode(), SEQS)
    assert margins is None and any("没有数据行" in e["message"] for e in errors)


def test_parse_stop_totals_bom_and_extra_spaces():
    csv = ("﻿stop_sequence,boardings,alightings\n"
           " 1 , 15 , 0 \n2,10,3\n3,5,6\n4,0,21\n").encode()
    margins, errors = parse_stop_totals(csv, SEQS)
    assert errors == []
    assert margins[0] == (1, 15, 0)


# ---------------- CSV 解析：已核实起讫站对 ----------------

def test_parse_verified_ok_with_zero():
    csv = ("board_stop_sequence,alight_stop_sequence,passengers\n"
           "1,3,0\n1,4,12\n").encode()
    pairs, errors = parse_verified(csv, SEQS)
    assert errors == []
    assert pairs == [(1, 3, 0), (1, 4, 12)]


def test_parse_verified_empty_allowed():
    pairs, errors = parse_verified(
        "board_stop_sequence,alight_stop_sequence,passengers\n".encode(), SEQS)
    assert errors == []
    assert pairs == []


def test_parse_verified_board_must_precede_alight():
    csv = ("board_stop_sequence,alight_stop_sequence,passengers\n"
           "3,1,2\n2,2,1\n").encode()
    pairs, errors = parse_verified(csv, SEQS)
    assert pairs is None
    assert sum("必须早于" in e["message"] for e in errors) == 2


def test_parse_verified_duplicate_pair_and_bad_value():
    csv = ("board_stop_sequence,alight_stop_sequence,passengers\n"
           "1,3,2\n1,3,5\n2,4,-3\n").encode()
    pairs, errors = parse_verified(csv, SEQS)
    assert pairs is None
    assert any("重复" in e["message"] for e in errors)
    assert any(e["field"] == "passengers" for e in errors)


# ---------------- 可行性 ----------------

def test_feasible_simple():
    assert is_feasible([15, 10, 5, 0], [0, 3, 6, 21], {})
    # 总和不配平
    assert not is_feasible([15, 10, 5, 0], [0, 3, 6, 20], {})
    # 末站上车 / 首站下车
    assert not is_feasible([1, 0], [0, 0], {})
    assert not is_feasible([0, 0], [1, 0], {})


def test_feasible_with_fixed_zero():
    # 钉死 x[1→3]=0 后仍可行（存在 x13=0 的分配）
    assert is_feasible([15, 10, 5, 0], [0, 3, 6, 21], {(0, 2): 0})
    # 钉死 x[1→2]=0 不可行：第 2 站下车 3 人只能来自第 1 站
    assert not is_feasible([15, 10, 5, 0], [0, 3, 6, 21], {(0, 1): 0})


def test_combination_problems_messages():
    problems = combination_problems([15, 10, 5, 1], [0, 3, 6, 21], [], SEQS)
    assert any("配平" in p for p in problems)
    assert any("末站" in p for p in problems)
    problems = combination_problems([15, 10, 5, 0], [2, 3, 6, 21], [], SEQS)
    assert any("首站" in p for p in problems)
    problems = combination_problems([15, 10, 5, 0], [0, 3, 6, 21],
                                    [(0, 3, 99)], SEQS)
    assert any("超过上车站" in p for p in problems)
    problems = combination_problems([15, 10, 5, 0], [0, 3, 6, 21],
                                    [(1, 2, 99)], SEQS)
    assert any("超过下车站" in p for p in problems)
    # 各项单独合法但整体无解（钉死 1→3 与 1→4 后，第 1 站 15 人无处可去）
    problems = combination_problems([15, 10, 5, 0], [0, 3, 6, 21],
                                    [(0, 2, 0), (0, 3, 0)], SEQS)
    assert problems == ["不存在同时满足各停站上下车总数与全部已核实人次的全趟分配"]
    assert combination_problems([15, 10, 5, 0], [0, 3, 6, 21], [], SEQS) == []


# ---------------- 站对上下界 ----------------

def test_pair_range_fixed_pair():
    lo, hi = pair_range([15, 10, 5, 0], [0, 3, 6, 21], {(0, 2): 0}, 0, 2)
    assert (lo, hi) == (0, 0)


def test_pair_range_known_case():
    # 样例数据：钉死 x13=0 后全部单元被强制（x12=3 受第 2 站下车 3 人钳制，
    # x14=15−3=12，x23=6，x24=4，x34=5）
    fixed = {(0, 2): 0}
    b, a = [15, 10, 5, 0], [0, 3, 6, 21]
    assert pair_range(b, a, fixed, 0, 3) == (12, 12)
    assert pair_range(b, a, fixed, 0, 1) == (3, 3)
    assert pair_range(b, a, fixed, 1, 3) == (4, 4)
    assert pair_range(b, a, fixed, 1, 2) == (6, 6)
    assert pair_range(b, a, fixed, 2, 3) == (5, 5)


def test_pair_range_no_constraint_pair_free():
    # 无核实时 x13 可以为 0 也可以很大
    lo, hi = pair_range([15, 10, 5, 0], [0, 3, 6, 21], {}, 0, 2)
    assert lo == 0 and hi == 6  # 受第 3 站下车总数 6 限制


def test_pair_range_requires_feasible():
    try:
        pair_range([1, 0], [0, 0], {}, 0, 1)
    except ValueError:
        return
    raise AssertionError("不可行组合应抛 ValueError")


# ---------------- 与暴力枚举交叉验证 ----------------

def _brute_force(boardings, alightings, fixed, p, q):
    """枚举所有可行分配，返回 (min, max)；不可行返回 None。"""
    n = len(boardings)
    cells = [(i, j) for i in range(n) for j in range(i + 1, n)]
    best = [None, None]

    def rec(k, b_rem, a_rem, x_pq):
        if k == len(cells):
            if all(v == 0 for v in b_rem) and all(v == 0 for v in a_rem):
                best[0] = x_pq if best[0] is None else min(best[0], x_pq)
                best[1] = x_pq if best[1] is None else max(best[1], x_pq)
            return
        i, j = cells[k]
        if (i, j) in fixed:
            v = fixed[(i, j)]
            if b_rem[i] >= v and a_rem[j] >= v:
                b_rem[i] -= v
                a_rem[j] -= v
                rec(k + 1, b_rem, a_rem, x_pq + (v if (i, j) == (p, q) else 0))
                b_rem[i] += v
                a_rem[j] += v
            return
        for v in range(min(b_rem[i], a_rem[j]) + 1):
            b_rem[i] -= v
            a_rem[j] -= v
            rec(k + 1, b_rem, a_rem, x_pq + (v if (i, j) == (p, q) else 0))
            b_rem[i] += v
            a_rem[j] += v

    rec(0, list(boardings), list(alightings), 0)
    return None if best[0] is None else tuple(best)


def test_against_brute_force_random():
    rng = random.Random(20261122)
    for trial in range(300):
        n = rng.randint(2, 5)
        cells = [(i, j) for i in range(n) for j in range(i + 1, n)]
        # 由随机分配生成边际，保证可行
        assign = {c: rng.randint(0, 4) for c in cells}
        b = [sum(assign[(i, j)] for j in range(i + 1, n)) for i in range(n)]
        a = [sum(assign[(i, j)] for i in range(j)) for j in range(n)]
        # 随机取若干单元按生成值钉死（含 0），组合必然可行
        fixed = {}
        for c in cells:
            if rng.random() < 0.35:
                fixed[c] = assign[c]
        assert is_feasible(b, a, fixed)
        for (p, q) in cells:
            expected = _brute_force(b, a, fixed, p, q)
            got = pair_range(b, a, fixed, p, q)
            assert got == expected, (
                f"trial={trial} n={n} b={b} a={a} fixed={fixed} "
                f"pair=({p},{q}) got={got} expected={expected}")


def test_feasibility_matches_brute_force_random():
    rng = random.Random(77003)
    for trial in range(200):
        n = rng.randint(2, 4)
        cells = [(i, j) for i in range(n) for j in range(i + 1, n)]
        # 随机（可能不可行）的边际与固定值
        b = [rng.randint(0, 5) for _ in range(n)]
        a = [rng.randint(0, 5) for _ in range(n)]
        fixed = {}
        for c in cells:
            if rng.random() < 0.3:
                fixed[c] = rng.randint(0, 4)
        brute = _brute_force(b, a, fixed, *cells[0]) is not None
        assert is_feasible(b, a, fixed) == brute, (
            f"trial={trial} n={n} b={b} a={a} fixed={fixed}")
