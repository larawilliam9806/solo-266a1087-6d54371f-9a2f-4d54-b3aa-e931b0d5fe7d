"""FastAPI HTTP 层：版本上传发布 + 时刻查询 + 起讫站客流核算。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date as date_type
from pathlib import Path
import json

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, odflow
from .gtfs import WEEKDAY_COLUMNS, format_gtfs_time, validate_version

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="公交 GTFS 时刻表发布", lifespan=lifespan)


# ---------------- 发布 ----------------

@app.post("/api/versions", status_code=201)
async def publish(
    label: str = Form(..., min_length=1, max_length=200),
    stops: UploadFile = File(...),
    routes: UploadFile = File(...),
    trips: UploadFile = File(...),
    stop_times: UploadFile = File(...),
    calendar: UploadFile = File(...),
):
    """上传同一版本的 5 个 CSV。整版校验通过才发布，否则记录错误并保持当前正式版。"""
    uploads = {"stops": stops, "routes": routes, "trips": trips,
               "stop_times": stop_times, "calendar": calendar}
    contents: dict[str, bytes] = {}
    for name, up in uploads.items():
        contents[name] = await up.read()

    tables, errors, truncated = validate_version(contents)
    conn = db.connect()
    try:
        if tables is None:
            vid = db.record_failed_version(conn, label, errors)
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "版本校验失败，未发布；当前正式版保持不变",
                    "version_id": vid,
                    "error_count": len(errors),
                    "truncated": truncated,
                    "errors": errors,
                })
        vid = db.publish_version(conn, label, tables)
        counts = {name: len(rows) for name, rows in tables.items()}
    finally:
        conn.close()
    return {"message": "版本已发布", "version_id": vid, "label": label,
            "status": "published", "counts": counts}


# ---------------- 版本信息 ----------------

@app.get("/api/versions")
def list_versions():
    conn = db.connect()
    try:
        cur_id = db.current_version_id(conn)
        rows = conn.execute(
            "SELECT id, label, status, error_count, created_at, published_at "
            "FROM versions ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    return {"current_version_id": cur_id,
            "versions": [dict(r) for r in rows]}


@app.get("/api/versions/current")
def current_version():
    conn = db.connect()
    try:
        vid = db.current_version_id(conn)
        if vid is None:
            raise HTTPException(404, "尚未发布任何版本")
        row = conn.execute(
            "SELECT id, label, status, created_at, published_at "
            "FROM versions WHERE id = ?", (vid,)).fetchone()
    finally:
        conn.close()
    return dict(row)


@app.get("/api/versions/{version_id}")
def version_detail(version_id: int):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, label, status, error_count, created_at, published_at "
            "FROM versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"版本 {version_id} 不存在")
        errors = conn.execute(
            "SELECT file, row, field, message FROM version_errors "
            "WHERE version_id = ? ORDER BY id", (version_id,)).fetchall()
    finally:
        conn.close()
    result = dict(row)
    result["errors"] = [dict(e) for e in errors]
    return result


# ---------------- 查询（始终读取同一完整正式版） ----------------

def _require_current(conn) -> int:
    vid = db.current_version_id(conn)
    if vid is None:
        raise HTTPException(404, "尚未发布任何版本")
    return vid


def _parse_service_date(value: str) -> date_type:
    try:
        return date_type.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"date '{value}' 不是合法的 YYYY-MM-DD 日期")


@app.get("/api/routes")
def list_routes():
    conn = db.connect()
    try:
        vid = _require_current(conn)
        rows = conn.execute(
            "SELECT route_id, route_short_name, route_long_name, route_type "
            "FROM routes WHERE version_id = ? ORDER BY route_id", (vid,)).fetchall()
    finally:
        conn.close()
    return {"version_id": vid, "routes": [dict(r) for r in rows]}


@app.get("/api/routes/{route_id}/schedule")
def route_schedule(
    route_id: str,
    date: str = Query(..., description="运营日，格式 YYYY-MM-DD"),
):
    """按线路 + 运营日查询有序停站时刻。

    时刻以 GTFS 形式返回（小时可 >= 24，表示跨午夜后仍属该运营日），
    并附 day_offset（相对运营日的天数偏移）与实际钟点。
    """
    svc_date = _parse_service_date(date)
    weekday_col = WEEKDAY_COLUMNS[svc_date.weekday()]

    conn = db.connect()
    try:
        # 请求开始时读取一次指针，之后所有查询都固定在这个完整版本上
        vid = _require_current(conn)
        route = conn.execute(
            "SELECT route_id, route_short_name, route_long_name, route_type "
            "FROM routes WHERE version_id = ? AND route_id = ?",
            (vid, route_id)).fetchone()
        if route is None:
            raise HTTPException(404, f"线路 '{route_id}' 在当前版本 {vid} 中不存在")

        services = conn.execute(
            f"SELECT service_id FROM calendar WHERE version_id = ? "
            f"AND start_date <= ? AND end_date >= ? AND {weekday_col} = 1",
            (vid, date, date)).fetchall()
        service_ids = [r["service_id"] for r in services]

        trips_out = []
        if service_ids:
            placeholders = ",".join("?" for _ in service_ids)
            trips = conn.execute(
                f"SELECT trip_id, service_id, trip_headsign, direction_id "
                f"FROM trips WHERE version_id = ? AND route_id = ? "
                f"AND service_id IN ({placeholders})",
                (vid, route_id, *service_ids)).fetchall()
            for t in trips:
                stops = conn.execute(
                    "SELECT st.stop_sequence, st.stop_id, s.stop_name, "
                    "st.arrival_secs, st.departure_secs "
                    "FROM stop_times st JOIN stops s "
                    "  ON s.version_id = st.version_id AND s.stop_id = st.stop_id "
                    "WHERE st.version_id = ? AND st.trip_id = ? "
                    "ORDER BY st.stop_sequence",
                    (vid, t["trip_id"])).fetchall()
                trips_out.append({
                    "trip_id": t["trip_id"],
                    "service_id": t["service_id"],
                    "headsign": t["trip_headsign"],
                    "direction_id": t["direction_id"],
                    "stop_times": [_stop_time_dict(s) for s in stops],
                })
            trips_out.sort(
                key=lambda t: t["stop_times"][0]["departure_secs"]
                if t["stop_times"] else 0)
    finally:
        conn.close()

    return {
        "version_id": vid,
        "route": dict(route),
        "date": date,
        "weekday": weekday_col,
        "trip_count": len(trips_out),
        "trips": trips_out,
    }


def _stop_time_dict(row) -> dict:
    arr, dep = row["arrival_secs"], row["departure_secs"]
    return {
        "stop_sequence": row["stop_sequence"],
        "stop_id": row["stop_id"],
        "stop_name": row["stop_name"],
        "arrival": format_gtfs_time(arr),
        "departure": format_gtfs_time(dep),
        "arrival_secs": arr,
        "departure_secs": dep,
        "arrival_day_offset": arr // 86400,
        "departure_day_offset": dep // 86400,
        "arrival_clock": format_gtfs_time(arr % 86400),
        "departure_clock": format_gtfs_time(dep % 86400),
    }


# ---------------- 起讫站客流核算 ----------------

def _od_trip_context(conn, vid: int, trip_id: str, date: str,
                     svc_date: date_type):
    """班次存在且当日开行；返回 (trip, route, stops)。"""
    trip = conn.execute(
        "SELECT trip_id, route_id, service_id, trip_headsign, direction_id "
        "FROM trips WHERE version_id = ? AND trip_id = ?",
        (vid, trip_id)).fetchone()
    if trip is None:
        raise HTTPException(404, f"班次 '{trip_id}' 在当前版本 {vid} 中不存在")
    weekday_col = WEEKDAY_COLUMNS[svc_date.weekday()]
    svc = conn.execute(
        f"SELECT service_id FROM calendar WHERE version_id = ? "
        f"AND service_id = ? AND start_date <= ? AND end_date >= ? "
        f"AND {weekday_col} = 1",
        (vid, trip["service_id"], date, date)).fetchone()
    if svc is None:
        raise HTTPException(422, f"班次 '{trip_id}' 在运营日 {date} 不开行")
    route = conn.execute(
        "SELECT route_id, route_short_name, route_long_name, route_type "
        "FROM routes WHERE version_id = ? AND route_id = ?",
        (vid, trip["route_id"])).fetchone()
    stops = conn.execute(
        "SELECT st.stop_sequence, st.stop_id, s.stop_name, "
        "st.arrival_secs, st.departure_secs "
        "FROM stop_times st JOIN stops s "
        "  ON s.version_id = st.version_id AND s.stop_id = st.stop_id "
        "WHERE st.version_id = ? AND st.trip_id = ? "
        "ORDER BY st.stop_sequence",
        (vid, trip_id)).fetchall()
    return trip, route, stops


def _margins_and_fixed(stop_sequences: list[int],
                       totals_csv: str, verified_csv: str | None):
    """把生效底表解析为流模型的边际与固定单元（位置坐标）。"""
    margins, _ = odflow.parse_stop_totals(totals_csv.encode(), stop_sequences)
    pairs = []
    if verified_csv is not None:
        pairs, _ = odflow.parse_verified(verified_csv.encode(), stop_sequences)
    pos = {s: i for i, s in enumerate(stop_sequences)}
    boardings = [b for _, b, _ in margins]
    alightings = [a for _, _, a in margins]
    fixed = {(pos[p], pos[q]): v for p, q, v in pairs}
    return boardings, alightings, fixed


def _state_payload(vid: int, date: str, trip, route, stops, eff,
                   records) -> dict:
    stop_sequences = [s["stop_sequence"] for s in stops]
    totals = None
    if eff["stop_totals"]:
        rows, _ = odflow.parse_stop_totals(
            eff["stop_totals"]["csv_content"].encode(), stop_sequences)
        totals = {
            "table_id": eff["stop_totals"]["id"],
            "uploaded_at": eff["stop_totals"]["created_at"],
            "rows": [{"stop_sequence": s, "boardings": b, "alightings": a}
                     for s, b, a in rows],
        }
    verified = None
    if eff["verified_od"]:
        pairs, _ = odflow.parse_verified(
            eff["verified_od"]["csv_content"].encode(), stop_sequences)
        verified = {
            "table_id": eff["verified_od"]["id"],
            "uploaded_at": eff["verified_od"]["created_at"],
            "rows": [{"board_stop_sequence": p, "alight_stop_sequence": q,
                      "passengers": v} for p, q, v in pairs],
        }
    return {
        "version_id": vid,
        "service_date": date,
        "trip_id": trip["trip_id"],
        "route": dict(route),
        "headsign": trip["trip_headsign"],
        "stops": [_stop_time_dict(s) for s in stops],
        "stop_totals": totals,
        "verified_od": verified,
        "recent_uploads": [
            {"id": r["id"], "kind": r["kind"], "status": r["status"],
             "error_count": r["error_count"], "created_at": r["created_at"],
             "errors": json.loads(r["errors_json"]) if r["errors_json"] else []}
            for r in records],
    }


@app.get("/api/od/trips")
def od_trips(date: str = Query(..., description="运营日，格式 YYYY-MM-DD")):
    """当前正式版在该运营日开行的全部班次（跨线路）。"""
    svc_date = _parse_service_date(date)
    weekday_col = WEEKDAY_COLUMNS[svc_date.weekday()]
    conn = db.connect()
    try:
        vid = _require_current(conn)
        services = conn.execute(
            f"SELECT service_id FROM calendar WHERE version_id = ? "
            f"AND start_date <= ? AND end_date >= ? AND {weekday_col} = 1",
            (vid, date, date)).fetchall()
        service_ids = [r["service_id"] for r in services]
        trips_out = []
        if service_ids:
            placeholders = ",".join("?" for _ in service_ids)
            trips = conn.execute(
                f"SELECT t.trip_id, t.route_id, t.trip_headsign, "
                f"t.direction_id, r.route_short_name, r.route_long_name "
                f"FROM trips t JOIN routes r "
                f"  ON r.version_id = t.version_id AND r.route_id = t.route_id "
                f"WHERE t.version_id = ? AND t.service_id IN ({placeholders})",
                (vid, *service_ids)).fetchall()
            for t in trips:
                first = conn.execute(
                    "SELECT departure_secs FROM stop_times "
                    "WHERE version_id = ? AND trip_id = ? "
                    "ORDER BY stop_sequence LIMIT 1",
                    (vid, t["trip_id"])).fetchone()
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM stop_times "
                    "WHERE version_id = ? AND trip_id = ?",
                    (vid, t["trip_id"])).fetchone()["c"]
                trips_out.append({
                    "trip_id": t["trip_id"],
                    "route_id": t["route_id"],
                    "route_short_name": t["route_short_name"],
                    "route_long_name": t["route_long_name"],
                    "headsign": t["trip_headsign"],
                    "direction_id": t["direction_id"],
                    "first_departure": format_gtfs_time(first["departure_secs"]),
                    "first_departure_secs": first["departure_secs"],
                    "stop_count": count,
                })
            trips_out.sort(key=lambda t: (t["route_id"],
                                          t["first_departure_secs"],
                                          t["trip_id"]))
    finally:
        conn.close()
    return {"version_id": vid, "date": date, "trips": trips_out}


@app.get("/api/od/state")
def od_state(trip_id: str = Query(...),
             date: str = Query(..., description="运营日，格式 YYYY-MM-DD")):
    """该班次当前生效的底表组合、停站序列与最近提交记录。"""
    svc_date = _parse_service_date(date)
    conn = db.connect()
    try:
        vid = _require_current(conn)
        trip, route, stops = _od_trip_context(conn, vid, trip_id, date, svc_date)
        eff = db.effective_od_tables(conn, vid, date, trip_id)
        records = db.list_od_records(conn, vid, date, trip_id)
    finally:
        conn.close()
    return _state_payload(vid, date, trip, route, stops, eff, records)


async def _upload_od(kind: str, trip_id: str, date: str, file: UploadFile):
    """换交一份底表：格式校验 → 组合可行性 → 单事务换交。

    表格有误、无解或保存失败时抛错，原表和原结果继续可查。
    """
    svc_date = _parse_service_date(date)
    content = await file.read()
    conn = db.connect()
    try:
        vid = _require_current(conn)
        trip, route, stops = _od_trip_context(conn, vid, trip_id, date, svc_date)
        stop_sequences = [s["stop_sequence"] for s in stops]

        # 1) 新表格式校验
        if kind == "stop_totals":
            parsed, errors = odflow.parse_stop_totals(content, stop_sequences)
        else:
            parsed, errors = odflow.parse_verified(content, stop_sequences)
        if parsed is None:
            try:
                db.record_rejected_od(conn, vid, date, trip_id, kind, errors)
            except Exception:
                pass  # 拒绝记录本身失败不影响主流程
            raise HTTPException(status_code=422, detail={
                "message": "表格校验失败，换交未生效；原表和原结果继续可查",
                "kind": kind, "errors": errors})

        # 2) 与“实际生效的组合”核验全趟分配可行性，并原子换交
        new_csv = content.decode("utf-8-sig")

        def check(current):
            if kind == "stop_totals":
                totals_csv = new_csv
                verified_csv = (current["verified_od"] or {}).get("csv_content")
            else:
                if current["stop_totals"] is None:
                    raise odflow.OdRejected([{
                        "file": kind, "row": None, "field": None,
                        "message": "尚无生效的逐停站上下车总数表，无法核验"
                                   "起讫站对；请先上传上下车总数表"}])
                totals_csv = current["stop_totals"]["csv_content"]
                verified_csv = new_csv
            margins, _ = odflow.parse_stop_totals(
                totals_csv.encode(), stop_sequences)
            pairs = []
            if verified_csv is not None:
                pairs, _ = odflow.parse_verified(
                    verified_csv.encode(), stop_sequences)
            pos = {s: i for i, s in enumerate(stop_sequences)}
            problems = odflow.combination_problems(
                [b for _, b, _ in margins],
                [a for _, _, a in margins],
                [(pos[p], pos[q], v) for p, q, v in pairs],
                stop_sequences)
            if problems:
                raise odflow.OdRejected([
                    {"file": kind, "row": None, "field": None, "message": m}
                    for m in problems])

        try:
            table_id = db.swap_od_table(
                conn, vid, date, trip_id, kind, new_csv, check)
        except odflow.OdRejected as rej:
            try:
                db.record_rejected_od(conn, vid, date, trip_id, kind, rej.errors)
            except Exception:
                pass
            raise HTTPException(status_code=422, detail={
                "message": "不存在同时满足各停站上下车总数与全部已核实人次的"
                           "全趟分配，换交未生效；原表和原结果继续可查",
                "kind": kind, "errors": rej.errors})
        except Exception:
            raise HTTPException(status_code=500, detail={
                "message": "保存失败，换交未生效；原表和原结果继续可查",
                "kind": kind})

        eff = db.effective_od_tables(conn, vid, date, trip_id)
        records = db.list_od_records(conn, vid, date, trip_id)
        state = _state_payload(vid, date, trip, route, stops, eff, records)
    finally:
        conn.close()
    return {"message": "底表已换交生效", "kind": kind, "table_id": table_id,
            "state": state}


@app.post("/api/od/stop-totals", status_code=201)
async def upload_stop_totals(trip_id: str = Form(...),
                             date: str = Form(...),
                             file: UploadFile = File(...)):
    """换交「逐停站上下车总数」CSV。"""
    return await _upload_od("stop_totals", trip_id, date, file)


@app.post("/api/od/verified", status_code=201)
async def upload_verified(trip_id: str = Form(...),
                          date: str = Form(...),
                          file: UploadFile = File(...)):
    """换交「已核实起讫站对及人次」CSV（核实值允许为 0）。"""
    return await _upload_od("verified_od", trip_id, date, file)


@app.get("/api/od/range")
def od_range(trip_id: str = Query(...),
             date: str = Query(..., description="运营日，格式 YYYY-MM-DD"),
             board_seq: int = Query(..., description="上车站停站序号"),
             alight_seq: int = Query(..., description="下车站停站序号")):
    """任选两个先后停站，从同一时点生效的底表算出该站对在
    所有可行分配中的最少和最多人次。"""
    svc_date = _parse_service_date(date)
    conn = db.connect()
    try:
        vid = _require_current(conn)
        trip, route, stops = _od_trip_context(conn, vid, trip_id, date, svc_date)
        stop_sequences = [s["stop_sequence"] for s in stops]
        eff = db.effective_od_tables(conn, vid, date, trip_id)
    finally:
        conn.close()
    if eff["stop_totals"] is None:
        raise HTTPException(
            409, "该班次尚无生效的逐停站上下车总数表，无法核算站对人次")
    if board_seq not in stop_sequences:
        raise HTTPException(422, f"上车站序 {board_seq} 不属于该班次的停站序列")
    if alight_seq not in stop_sequences:
        raise HTTPException(422, f"下车站序 {alight_seq} 不属于该班次的停站序列")
    if board_seq >= alight_seq:
        raise HTTPException(
            422, f"上车站序 {board_seq} 必须早于下车站序 {alight_seq}")
    boardings, alightings, fixed = _margins_and_fixed(
        stop_sequences,
        eff["stop_totals"]["csv_content"],
        (eff["verified_od"] or {}).get("csv_content"))
    pos = {s: i for i, s in enumerate(stop_sequences)}
    try:
        lo, hi = odflow.pair_range(boardings, alightings, fixed,
                                   pos[board_seq], pos[alight_seq])
    except ValueError:
        raise HTTPException(500, "生效底表组合不可行（数据状态异常）")
    return {
        "version_id": vid,
        "service_date": date,
        "trip_id": trip_id,
        "board_stop_sequence": board_seq,
        "alight_stop_sequence": alight_seq,
        "min_passengers": lo,
        "max_passengers": hi,
        "stop_totals_table_id": eff["stop_totals"]["id"],
        "verified_od_table_id": (eff["verified_od"] or {}).get("id"),
    }


# ---------------- 网页 ----------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
