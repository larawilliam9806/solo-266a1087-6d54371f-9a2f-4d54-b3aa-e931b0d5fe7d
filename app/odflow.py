"""起讫站（OD）客流核算：CSV 解析校验 + 全趟分配可行性 + 站对人次上下界。

纯标准库实现，不依赖 FastAPI / SQLite，便于单元测试。

模型：一趟班次按停站顺序排列为位置 0..n-1，x[i][j]（i<j）表示在
第 i 站上车、第 j 站下车的人数。约束：
- 每个停站 i：sum_j x[i][j] = 上车总数[i]
- 每个停站 j：sum_i x[i][j] = 下车总数[j]
- 每条已核实起讫站对 (p,q)：x[p][q] = 核实人次（允许为 0）
所有 x 为非负整数。这是运输问题，约束矩阵全单位模：整数边际下
“实数可行 ⇔ 整数可行”，且极值在整数点取得。因此用最大流判定
可行性，并用流的上下界公式求任意站对在所有可行分配中的
最少 / 最多人次：

- min x[p][q] = T − 去掉边 (p,q) 后的最大流（T 为总人次）：
  不经过 (p,q) 最多能安排多少，差额被迫经过 (p,q)。
- max x[p][q]：对 w 二分，检验「上车站边际 −w、下车站边际 −w 后
  仍可行」。该谓词随 w 单调（w 可行则任意 w'<w 可把差额直接改走
  (p,q)），且「扣除 w 后可行 ⇔ 存在 x[p][q] ≥ w 的分配」。
"""
from __future__ import annotations

import csv
import io
import re
from collections import deque

MAX_ERRORS = 200  # 单表最多记录的错误条数
INT_RE = re.compile(r"^\d+$")

STOP_TOTALS_FILE = "stop_totals"
VERIFIED_FILE = "verified_od"
STOP_TOTALS_COLUMNS = ["stop_sequence", "boardings", "alightings"]
VERIFIED_COLUMNS = ["board_stop_sequence", "alight_stop_sequence", "passengers"]


class OdRejected(Exception):
    """换交被拒绝：携带错误明细 [{file, row, field, message}]。"""

    def __init__(self, errors: list[dict]):
        super().__init__(errors[0]["message"] if errors else "换交被拒绝")
        self.errors = errors


class _Bag:
    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.truncated = False

    def add(self, file: str, row: int | None, field: str | None, message: str) -> None:
        if len(self.errors) >= MAX_ERRORS:
            self.truncated = True
            return
        self.errors.append({"file": file, "row": row, "field": field,
                            "message": message})

    def __bool__(self) -> bool:
        return bool(self.errors)


def _read_csv(file: str, content: bytes, required: list[str], bag: _Bag,
              allow_empty: bool = False) -> list[dict] | None:
    """读取 CSV 为 dict 行列表，每行附带 _row（文件行号，表头=1）。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        bag.add(file, None, None, "文件不是有效的 UTF-8 编码")
        return None
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        bag.add(file, None, None, "文件为空或缺少表头")
        return None
    headers = [h.strip() if h else "" for h in reader.fieldnames]
    missing = [c for c in required if c not in headers]
    if missing:
        bag.add(file, 1, None, f"缺少必备列: {', '.join(missing)}")
        return None
    rows: list[dict] = []
    for lineno, raw in enumerate(reader, start=2):
        if None in raw:  # 列数多于表头
            bag.add(file, lineno, None, "行列数多于表头列数")
            continue
        row = {k.strip(): (v.strip() if v is not None else "")
               for k, v in raw.items() if k is not None}
        row["_row"] = lineno
        rows.append(row)
    if not rows and not allow_empty:
        bag.add(file, None, None, "文件没有数据行")
        return None
    return rows


def parse_stop_totals(content: bytes, stop_sequences: list[int]
                      ) -> tuple[list[tuple[int, int, int]] | None, list[dict]]:
    """解析「逐停站上下车总数」CSV。

    stop_sequences：该班次按停站顺序（stop_sequence 升序）的序号列表。
    返回 (margins, errors)：margins 为 [(stop_sequence, boardings, alightings)]，
    按停站顺序排列；有错误时为 None。
    表格必须恰好覆盖班次的每个停站序号一次。
    """
    bag = _Bag()
    raw = _read_csv(STOP_TOTALS_FILE, content, STOP_TOTALS_COLUMNS, bag)
    if raw is None:
        return None, bag.errors
    known = set(stop_sequences)
    parsed: dict[int, tuple[int, int]] = {}
    present: set[int] = set()   # 文件中出现过的（合法）停站序号
    seen_line: dict[int, int] = {}
    for r in raw:
        line = r["_row"]
        seq_txt = r["stop_sequence"]
        if not INT_RE.match(seq_txt):
            bag.add(STOP_TOTALS_FILE, line, "stop_sequence",
                    f"stop_sequence '{seq_txt}' 不是非负整数")
            continue
        seq = int(seq_txt)
        if seq not in known:
            bag.add(STOP_TOTALS_FILE, line, "stop_sequence",
                    f"stop_sequence {seq} 不属于该班次的停站序列")
            continue
        if seq in seen_line:
            bag.add(STOP_TOTALS_FILE, line, "stop_sequence",
                    f"stop_sequence {seq} 重复（首次出现于第 {seen_line[seq]} 行）")
            continue
        seen_line[seq] = line
        present.add(seq)
        vals: list[int] = []
        ok = True
        for field in ("boardings", "alightings"):
            v = r[field]
            if not INT_RE.match(v):
                bag.add(STOP_TOTALS_FILE, line, field,
                        f"{field} '{v}' 不是非负整数")
                ok = False
            else:
                vals.append(int(v))
        if ok:
            parsed[seq] = (vals[0], vals[1])
    missing = [s for s in stop_sequences if s not in present]
    if missing:
        shown = ", ".join(str(s) for s in missing[:10])
        more = f" 等 {len(missing)} 个" if len(missing) > 10 else ""
        bag.add(STOP_TOTALS_FILE, None, "stop_sequence",
                f"缺少停站序号的行: {shown}{more}；上下车总数表必须覆盖班次每个停站")
    if bag:
        return None, bag.errors
    return [(s, *parsed[s]) for s in stop_sequences], []


def parse_verified(content: bytes, stop_sequences: list[int]
                   ) -> tuple[list[tuple[int, int, int]] | None, list[dict]]:
    """解析「已核实起讫站对及人次」CSV。

    返回 (pairs, errors)：pairs 为 [(board_seq, alight_seq, passengers)]；
    有错误时为 None。允许只有表头没有数据行（表示没有任何已核实站对）。
    核实人次允许为 0（表示该站对核实无人）。
    """
    bag = _Bag()
    raw = _read_csv(VERIFIED_FILE, content, VERIFIED_COLUMNS, bag,
                    allow_empty=True)
    if raw is None:
        return None, bag.errors
    known = set(stop_sequences)
    seen: dict[tuple[int, int], int] = {}
    pairs: list[tuple[int, int, int]] = []
    for r in raw:
        line = r["_row"]
        seqs: list[int] = []
        ok = True
        for field in ("board_stop_sequence", "alight_stop_sequence"):
            v = r[field]
            if not INT_RE.match(v):
                bag.add(VERIFIED_FILE, line, field,
                        f"{field} '{v}' 不是非负整数")
                ok = False
            else:
                seqs.append(int(v))
        pax_txt = r["passengers"]
        if not INT_RE.match(pax_txt):
            bag.add(VERIFIED_FILE, line, "passengers",
                    f"passengers '{pax_txt}' 不是非负整数")
            ok = False
        if not ok:
            continue
        b_seq, a_seq = seqs
        for field, s in (("board_stop_sequence", b_seq),
                         ("alight_stop_sequence", a_seq)):
            if s not in known:
                bag.add(VERIFIED_FILE, line, field,
                        f"{field} {s} 不属于该班次的停站序列")
                ok = False
        if not ok:
            continue
        if b_seq >= a_seq:
            bag.add(VERIFIED_FILE, line, "alight_stop_sequence",
                    f"上车站序 {b_seq} 必须早于下车站序 {a_seq}")
            continue
        key = (b_seq, a_seq)
        if key in seen:
            bag.add(VERIFIED_FILE, line, None,
                    f"起讫站对 ({b_seq}, {a_seq}) 重复（首次出现于第 {seen[key]} 行）")
            continue
        seen[key] = line
        pairs.append((b_seq, a_seq, int(pax_txt)))
    if bag:
        return None, bag.errors
    return pairs, []


# ---------------- 最大流（Dinic，迭代 DFS） ----------------

class _Dinic:
    def __init__(self, n: int) -> None:
        self.g: list[list[list]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int) -> None:
        self.g[u].append([v, cap, len(self.g[v])])
        self.g[v].append([u, 0, len(self.g[u]) - 1])

    def max_flow(self, s: int, t: int) -> int:
        g = self.g
        n = len(g)
        flow = 0
        inf = 1 << 62
        while True:
            level = [-1] * n
            level[s] = 0
            queue = deque([s])
            while queue:
                u = queue.popleft()
                for e in g[u]:
                    if e[1] > 0 and level[e[0]] < 0:
                        level[e[0]] = level[u] + 1
                        queue.append(e[0])
            if level[t] < 0:
                return flow
            it = [0] * n
            nodes = [s]   # 当前 DFS 路径上的点
            path: list[list] = []  # path[k]：nodes[k] -> nodes[k+1] 的边
            while nodes:
                u = nodes[-1]
                if u == t:
                    f = inf
                    for e in path:
                        if e[1] < f:
                            f = e[1]
                    for e in path:
                        e[1] -= f
                        g[e[0]][e[2]][1] += f
                    flow += f
                    # 回退到最浅（最靠源点）的饱和边
                    cut = len(path)
                    for idx, e in enumerate(path):
                        if e[1] == 0:
                            cut = idx
                            break
                    del path[cut:]
                    del nodes[cut + 1:]
                    continue
                advanced = False
                edges = g[u]
                i = it[u]
                while i < len(edges):
                    e = edges[i]
                    if e[1] > 0 and level[e[0]] == level[u] + 1:
                        it[u] = i
                        path.append(e)
                        nodes.append(e[0])
                        advanced = True
                        break
                    i += 1
                if not advanced:
                    # u 是死路：弹出，并让父节点跳过指向 u 的这条边
                    nodes.pop()
                    if path:
                        path.pop()
                        it[nodes[-1]] += 1
        # unreachable


def _build(b: list[int], a: list[int], removed: set[tuple[int, int]]
           ) -> tuple[_Dinic, int, int, int]:
    """由残余边际建流网络。调用方保证 b/a 非负且总和相等。

    点：0..n-1 供给（上车），n..2n-1 需求（下车），s=2n，t=2n+1。
    返回 (dinic, s, t, total)。
    """
    n = len(b)
    s, t = 2 * n, 2 * n + 1
    d = _Dinic(2 * n + 2)
    total = sum(b)
    inf_cap = total + 1
    for i in range(n):
        if b[i]:
            d.add_edge(s, i, b[i])
        if a[i]:
            d.add_edge(n + i, t, a[i])
    for i in range(n):
        for j in range(i + 1, n):
            if (i, j) in removed:
                continue
            d.add_edge(i, n + j, inf_cap)
    return d, s, t, total


def _residual(boardings: list[int], alightings: list[int],
              fixed: dict[tuple[int, int], int]
              ) -> tuple[list[int], list[int]]:
    """从边际中扣除已核实的固定单元。"""
    b = list(boardings)
    a = list(alightings)
    for (i, j), v in fixed.items():
        b[i] -= v
        a[j] -= v
    return b, a


def _feasible_residual(b: list[int], a: list[int],
                       removed: set[tuple[int, int]]) -> bool:
    """残余边际 + 移除指定边后，是否存在全趟分配。"""
    if any(x < 0 for x in b) or any(x < 0 for x in a):
        return False
    if sum(b) != sum(a):
        return False
    d, s, t, total = _build(b, a, removed)
    return d.max_flow(s, t) == total


def is_feasible(boardings: list[int], alightings: list[int],
                fixed: dict[tuple[int, int], int]) -> bool:
    """是否存在同时满足上下车总数与全部已核实人次的全趟分配。"""
    b, a = _residual(boardings, alightings, fixed)
    return _feasible_residual(b, a, set(fixed))


def combination_problems(boardings: list[int], alightings: list[int],
                         verified: list[tuple[int, int, int]],
                         stop_sequences: list[int]) -> list[str]:
    """检查「上下车总数 + 已核实站对」组合，返回人类可读的问题列表（空=可行）。

    verified：[(上车站位置, 下车站位置, 人次)]；stop_sequences 用于错误信息。
    """
    problems: list[str] = []
    n = len(boardings)
    tb, ta = sum(boardings), sum(alightings)
    if tb != ta:
        problems.append(f"上车总数 {tb} 与下车总数 {ta} 不相等，无法配平")
    if n and boardings[-1] > 0:
        problems.append(
            f"末站（停站序号 {stop_sequences[-1]}）上车 {boardings[-1]} 人，"
            "之后没有站点可下车")
    if n and alightings[0] > 0:
        problems.append(
            f"首站（停站序号 {stop_sequences[0]}）下车 {alightings[0]} 人，"
            "之前没有站点可上车")
    fixed: dict[tuple[int, int], int] = {}
    for p, q, v in verified:
        fixed[(p, q)] = v
        if v > boardings[p]:
            problems.append(
                f"已核实站对 {stop_sequences[p]}→{stop_sequences[q]} 为 {v} 人，"
                f"超过上车站（序号 {stop_sequences[p]}）上车总数 {boardings[p]}")
        if v > alightings[q]:
            problems.append(
                f"已核实站对 {stop_sequences[p]}→{stop_sequences[q]} 为 {v} 人，"
                f"超过下车站（序号 {stop_sequences[q]}）下车总数 {alightings[q]}")
    if problems:
        return problems
    if not is_feasible(boardings, alightings, fixed):
        problems.append("不存在同时满足各停站上下车总数与全部已核实人次的全趟分配")
    return problems


def pair_range(boardings: list[int], alightings: list[int],
               fixed: dict[tuple[int, int], int],
               p: int, q: int) -> tuple[int, int]:
    """站对 (p,q)（位置，p<q）在所有可行分配中的 (最少, 最多) 人次。

    调用方保证组合可行；(p,q) 是已核实站对时直接返回 (v, v)。
    """
    if (p, q) in fixed:
        v = fixed[(p, q)]
        return v, v
    b, a = _residual(boardings, alightings, fixed)
    removed = set(fixed)
    if not _feasible_residual(b, a, removed):
        raise ValueError("组合不可行，无法核算站对区间")
    # 最少：不经过 (p,q) 最多能安排 F，则至少 total − F 被迫经过 (p,q)
    d, s, t, total = _build(b, a, removed | {(p, q)})
    lo = total - d.max_flow(s, t)
    # 最多：二分最大的 w，使残余 (b−w·e_p, a−w·e_q) 仍可行
    hi = min(b[p], a[q])
    best = 0
    while best < hi:
        mid = (best + hi + 1) // 2
        b2 = list(b)
        b2[p] -= mid
        a2 = list(a)
        a2[q] -= mid
        if _feasible_residual(b2, a2, removed):
            best = mid
        else:
            hi = mid - 1
    return lo, best
