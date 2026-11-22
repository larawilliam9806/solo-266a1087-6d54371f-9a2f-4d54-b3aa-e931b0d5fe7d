"""SQLite 存储层。

设计要点：
- 每个版本的数据整体写入、永不修改（immutable），通过 meta 表中的
  current_version_id 指针标识当前正式版。
- 发布 = 单个事务内插入全部数据 + 拨动指针，对查询方而言要么看到
  旧完整版本、要么看到新完整版本，绝不出现半新半旧。
- 失败版本只写 versions/version_errors 元数据，不写任何时刻数据，
  也不触碰指针，当前正式版保持不变。
- WAL 模式：写入期间不阻塞读取。
"""
from __future__ import annotations

import json
import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "data", "gtfs.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('published', 'failed')),
    error_count  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stops (
    version_id INTEGER NOT NULL,
    stop_id    TEXT NOT NULL,
    stop_name  TEXT NOT NULL DEFAULT '',
    stop_lat   REAL,
    stop_lon   REAL,
    PRIMARY KEY (version_id, stop_id)
);

CREATE TABLE IF NOT EXISTS routes (
    version_id       INTEGER NOT NULL,
    route_id         TEXT NOT NULL,
    route_short_name TEXT NOT NULL DEFAULT '',
    route_long_name  TEXT NOT NULL DEFAULT '',
    route_type       INTEGER,
    PRIMARY KEY (version_id, route_id)
);

CREATE TABLE IF NOT EXISTS calendar (
    version_id INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    monday     INTEGER NOT NULL,
    tuesday    INTEGER NOT NULL,
    wednesday  INTEGER NOT NULL,
    thursday   INTEGER NOT NULL,
    friday     INTEGER NOT NULL,
    saturday   INTEGER NOT NULL,
    sunday     INTEGER NOT NULL,
    start_date TEXT NOT NULL,   -- ISO YYYY-MM-DD，可字典序比较
    end_date   TEXT NOT NULL,
    PRIMARY KEY (version_id, service_id)
);

CREATE TABLE IF NOT EXISTS trips (
    version_id    INTEGER NOT NULL,
    route_id      TEXT NOT NULL,
    service_id    TEXT NOT NULL,
    trip_id       TEXT NOT NULL,
    trip_headsign TEXT NOT NULL DEFAULT '',
    direction_id  INTEGER,
    PRIMARY KEY (version_id, trip_id)
);
CREATE INDEX IF NOT EXISTS idx_trips_route ON trips (version_id, route_id);

CREATE TABLE IF NOT EXISTS stop_times (
    version_id     INTEGER NOT NULL,
    trip_id        TEXT NOT NULL,
    stop_sequence  INTEGER NOT NULL,
    stop_id        TEXT NOT NULL,
    arrival_secs   INTEGER NOT NULL,   -- 自运营日 00:00 起的秒数，可 >= 86400
    departure_secs INTEGER NOT NULL,
    PRIMARY KEY (version_id, trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS idx_stop_times_stop ON stop_times (version_id, stop_id);

CREATE TABLE IF NOT EXISTS version_errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    file       TEXT NOT NULL,
    row        INTEGER,
    field      TEXT,
    message    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_version_errors ON version_errors (version_id);

-- 起讫站客流核算：每次提交的底表（生效或被拒）都留痕，永不修改
CREATE TABLE IF NOT EXISTS od_tables (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL,           -- 所属运行图（正式版）
    service_date TEXT NOT NULL,              -- 运营日 YYYY-MM-DD
    trip_id      TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('stop_totals', 'verified_od')),
    status       TEXT NOT NULL CHECK (status IN ('active', 'rejected')),
    csv_content  TEXT,                       -- active 表的原始 CSV
    error_count  INTEGER NOT NULL DEFAULT 0,
    errors_json  TEXT,                       -- rejected 表的错误明细
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_od_tables_scope
    ON od_tables (version_id, service_date, trip_id, kind, id);

-- 每个 (运行图, 运营日, 班次) 当前生效的两份底表指针；
-- 换交 = 单事务内插新表 + 拨指针，查询方永远看到同一时点生效的组合
CREATE TABLE IF NOT EXISTS od_effective (
    version_id   INTEGER NOT NULL,
    service_date TEXT NOT NULL,
    trip_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    table_id     INTEGER NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (version_id, service_date, trip_id, kind)
);
"""


def db_path() -> str:
    return os.environ.get("GTFS_DB_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def current_version_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'current_version_id'").fetchone()
    return int(row["value"]) if row else None


def publish_version(conn: sqlite3.Connection, label: str,
                    tables: dict[str, list[tuple]]) -> int:
    """单事务写入整版数据并拨动指针。调用方保证 tables 已通过校验。"""
    with conn:  # 事务：全部成功才提交，异常自动回滚
        cur = conn.execute(
            "INSERT INTO versions (label, status, published_at) "
            "VALUES (?, 'published', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (label,))
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO stops (version_id, stop_id, stop_name, stop_lat, stop_lon) "
            "VALUES (?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["stops"]])
        conn.executemany(
            "INSERT INTO routes (version_id, route_id, route_short_name, "
            "route_long_name, route_type) VALUES (?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["routes"]])
        conn.executemany(
            "INSERT INTO calendar (version_id, service_id, monday, tuesday, "
            "wednesday, thursday, friday, saturday, sunday, start_date, end_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["calendar"]])
        conn.executemany(
            "INSERT INTO trips (version_id, route_id, service_id, trip_id, "
            "trip_headsign, direction_id) VALUES (?, ?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["trips"]])
        conn.executemany(
            "INSERT INTO stop_times (version_id, trip_id, stop_sequence, stop_id, "
            "arrival_secs, departure_secs) VALUES (?, ?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["stop_times"]])
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('current_version_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(vid),))
    return vid


def record_failed_version(conn: sqlite3.Connection, label: str,
                          errors: list[dict]) -> int:
    """记录失败版本及其错误明细；不触碰正式版指针。"""
    with conn:
        cur = conn.execute(
            "INSERT INTO versions (label, status, error_count) VALUES (?, 'failed', ?)",
            (label, len(errors)))
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO version_errors (version_id, file, row, field, message) "
            "VALUES (?, ?, ?, ?, ?)",
            [(vid, e["file"], e["row"], e["field"], e["message"]) for e in errors])
    return vid


# ---------------- 起讫站客流核算 ----------------

def effective_od_tables(conn: sqlite3.Connection, version_id: int,
                        service_date: str, trip_id: str) -> dict:
    """单条 SELECT（同一快照）读取当前生效的两份底表。

    返回 {"stop_totals": {...} | None, "verified_od": {...} | None}。
    指针与内容在同一条查询里读出，因此永远是同一时点生效的组合。
    """
    rows = conn.execute(
        "SELECT e.kind, t.id, t.csv_content, t.created_at "
        "FROM od_effective e JOIN od_tables t ON t.id = e.table_id "
        "WHERE e.version_id = ? AND e.service_date = ? AND e.trip_id = ?",
        (version_id, service_date, trip_id)).fetchall()
    out = {"stop_totals": None, "verified_od": None}
    for r in rows:
        out[r["kind"]] = {"id": r["id"], "csv_content": r["csv_content"],
                          "created_at": r["created_at"]}
    return out


def swap_od_table(conn: sqlite3.Connection, version_id: int, service_date: str,
                  trip_id: str, kind: str, csv_content: str, check) -> int:
    """原子换交一份底表，返回新表 id。

    BEGIN IMMEDIATE 串行化所有换交：交错提交在事务内重新读取
    “实际生效的组合”并交给 check(current) 核验；check 抛异常
    （如 odflow.OdRejected）或写库失败都会整体回滚，
    原表和原结果继续可查。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = effective_od_tables(conn, version_id, service_date, trip_id)
        check(current)  # 核验新表与当前生效组合的全趟分配可行性
        cur = conn.execute(
            "INSERT INTO od_tables (version_id, service_date, trip_id, kind, "
            "status, csv_content) VALUES (?, ?, ?, ?, 'active', ?)",
            (version_id, service_date, trip_id, kind, csv_content))
        table_id = cur.lastrowid
        conn.execute(
            "INSERT INTO od_effective (version_id, service_date, trip_id, kind, "
            "table_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(version_id, service_date, trip_id, kind) "
            "DO UPDATE SET table_id = excluded.table_id, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')",
            (version_id, service_date, trip_id, kind, table_id))
        conn.execute("COMMIT")
        return table_id
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def record_rejected_od(conn: sqlite3.Connection, version_id: int,
                       service_date: str, trip_id: str, kind: str,
                       errors: list[dict]) -> None:
    """记录一次被拒绝的提交（审计用），独立事务，不触碰生效指针。"""
    with conn:
        conn.execute(
            "INSERT INTO od_tables (version_id, service_date, trip_id, kind, "
            "status, error_count, errors_json) "
            "VALUES (?, ?, ?, ?, 'rejected', ?, ?)",
            (version_id, service_date, trip_id, kind, len(errors),
             json.dumps(errors, ensure_ascii=False)))


def list_od_records(conn: sqlite3.Connection, version_id: int,
                    service_date: str, trip_id: str, limit: int = 10
                    ) -> list[dict]:
    """该班次最近的提交记录（含被拒绝的），新的在前。"""
    rows = conn.execute(
        "SELECT id, kind, status, error_count, errors_json, created_at "
        "FROM od_tables WHERE version_id = ? AND service_date = ? "
        "AND trip_id = ? ORDER BY id DESC LIMIT ?",
        (version_id, service_date, trip_id, limit)).fetchall()
    return [dict(r) for r in rows]
