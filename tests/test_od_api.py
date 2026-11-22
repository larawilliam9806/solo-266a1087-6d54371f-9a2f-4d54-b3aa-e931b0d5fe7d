"""起讫站客流核算 API 集成测试。

覆盖：上传生效、格式错误/无解/保存失败时原表原结果继续可查、
两份表分别换交与交错提交的组合核验、站对上下界查询、
版本/运营日/班次作用域。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import upload, valid_files

DATE = "2026-11-23"  # 周一，WKD 与 ALL 都开行

# T1 停站：1:S1 2:S2 3:S3 4:S4
TOTALS = ("stop_sequence,boardings,alightings\n"
          "1,15,0\n2,10,3\n3,5,6\n4,0,21\n")
VERIFIED_ZERO = ("board_stop_sequence,alight_stop_sequence,passengers\n"
                 "1,3,0\n")


def publish(client):
    r = upload(client, valid_files(), label="v1")
    assert r.status_code == 201


def post_od(client, kind, content, trip="T1", date=DATE):
    return client.post(
        f"/api/od/{kind}",
        data={"trip_id": trip, "date": date},
        files={"file": ("table.csv", content.encode(), "text/csv")},
    )


def state(client, trip="T1", date=DATE):
    return client.get(f"/api/od/state?trip_id={trip}&date={date}")


def od_range(client, board, alight, trip="T1", date=DATE):
    return client.get(f"/api/od/range?trip_id={trip}&date={date}"
                      f"&board_seq={board}&alight_seq={alight}")


# ---------------- 基本流程 ----------------

def test_full_flow(client):
    publish(client)
    trips = client.get(f"/api/od/trips?date={DATE}").json()
    assert {t["trip_id"] for t in trips["trips"]} == {"T1", "T2", "N1"}
    t1 = [t for t in trips["trips"] if t["trip_id"] == "T1"][0]
    assert t1["stop_count"] == 4 and t1["first_departure"] == "08:00:00"

    # 上传上下车总数表
    r = post_od(client, "stop-totals", TOTALS)
    assert r.status_code == 201, r.text
    st = state(client).json()
    assert st["stop_totals"]["rows"][0] == {
        "stop_sequence": 1, "boardings": 15, "alightings": 0}
    assert st["verified_od"] is None
    totals_id = st["stop_totals"]["table_id"]

    # 上传已核实站对表（含核实值 0）
    r = post_od(client, "verified", VERIFIED_ZERO)
    assert r.status_code == 201, r.text
    st = state(client).json()
    assert st["verified_od"]["rows"] == [
        {"board_stop_sequence": 1, "alight_stop_sequence": 3, "passengers": 0}]

    # 站对区间：x13 被钉 0 后全部强制
    r = od_range(client, 1, 4)
    assert r.status_code == 200
    body = r.json()
    assert (body["min_passengers"], body["max_passengers"]) == (12, 12)
    assert body["stop_totals_table_id"] == totals_id
    assert body["verified_od_table_id"] == st["verified_od"]["table_id"]
    assert (od_range(client, 1, 3).json()["min_passengers"] ==
            od_range(client, 1, 3).json()["max_passengers"] == 0)
    assert (od_range(client, 2, 3).json()["min_passengers"] ==
            od_range(client, 2, 3).json()["max_passengers"] == 6)


def test_range_without_verified(client):
    publish(client)
    post_od(client, "stop-totals", TOTALS)
    # 无核实约束：x14 ∈ [6,12]，x13 ∈ [0,6]
    body = od_range(client, 1, 4).json()
    assert (body["min_passengers"], body["max_passengers"]) == (6, 12)
    assert body["verified_od_table_id"] is None
    body = od_range(client, 1, 3).json()
    assert (body["min_passengers"], body["max_passengers"]) == (0, 6)


# ---------------- 格式错误：拒绝且原表不变 ----------------

def test_bad_csv_rejected_old_kept(client):
    publish(client)
    post_od(client, "stop-totals", TOTALS)
    good_id = state(client).json()["stop_totals"]["table_id"]

    bad = ("stop_sequence,boardings,alightings\n"
           "1,15,0\n2,10,3\n3,五,6\n9,0,21\n")  # 非整数 + 未知序号 + 缺行
    r = post_od(client, "stop-totals", bad)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["errors"]
    assert any(e["field"] == "boardings" for e in detail["errors"])
    assert any("不属于该班次" in e["message"] for e in detail["errors"])
    assert any("缺少停站序号" in e["message"] for e in detail["errors"])

    # 原表和原结果继续可查
    st = state(client).json()
    assert st["stop_totals"]["table_id"] == good_id
    assert od_range(client, 1, 4).status_code == 200
    # 拒绝记录留痕
    rejected = [r for r in st["recent_uploads"] if r["status"] == "rejected"]
    assert rejected and rejected[0]["kind"] == "stop_totals"
    assert rejected[0]["errors"]


def test_verified_format_rules(client):
    publish(client)
    post_od(client, "stop-totals", TOTALS)
    # 上车站序须早于下车站序
    r = post_od(client, "verified",
                "board_stop_sequence,alight_stop_sequence,passengers\n3,1,2\n")
    assert r.status_code == 422
    assert "必须早于" in r.json()["detail"]["errors"][0]["message"]
    # 人次必须非负整数
    r = post_od(client, "verified",
                "board_stop_sequence,alight_stop_sequence,passengers\n1,3,-2\n")
    assert r.status_code == 422
    # 重复站对
    r = post_od(client, "verified",
                "board_stop_sequence,alight_stop_sequence,passengers\n"
                "1,3,2\n1,3,5\n")
    assert r.status_code == 422
    assert any("重复" in e["message"] for e in r.json()["detail"]["errors"])
    # 均未生效
    assert state(client).json()["verified_od"] is None


# ---------------- 无解组合：换交不生效 ----------------

def test_unbalanced_totals_rejected(client):
    publish(client)
    r = post_od(client, "stop-totals",
                "stop_sequence,boardings,alightings\n"
                "1,15,0\n2,10,3\n3,5,6\n4,0,20\n")  # 上 30 ≠ 下 29
    assert r.status_code == 422
    assert any("配平" in e["message"] for e in r.json()["detail"]["errors"])
    assert state(client).json()["stop_totals"] is None
    assert od_range(client, 1, 4).status_code == 409


def test_interleaved_swap_validated_against_effective_combination(client):
    """交错提交按实际生效的组合核验；无解换交不生效，原组合继续可查。"""
    publish(client)
    post_od(client, "stop-totals", TOTALS)
    post_od(client, "verified", VERIFIED_ZERO)  # 钉死 x13=0
    good_totals = state(client).json()["stop_totals"]["table_id"]

    # 新总数表自身可行，但与已生效的 x13=0 冲突（第 3 站下车 10 人
    # 只能来自第 1、2 站，而第 2 站上车 0 人）→ 拒绝
    clash = ("stop_sequence,boardings,alightings\n"
             "1,15,0\n2,0,3\n3,0,10\n4,0,2\n")
    r = post_od(client, "stop-totals", clash)
    assert r.status_code == 422
    assert any("全趟分配" in e["message"] for e in r.json()["detail"]["errors"])
    st = state(client).json()
    assert st["stop_totals"]["table_id"] == good_totals  # 原表不动
    assert (od_range(client, 1, 4).json()["min_passengers"] == 12)  # 原结果可查

    # 与生效组合相容的新总数表 → 生效
    totals2 = ("stop_sequence,boardings,alightings\n"
               "1,12,0\n2,8,3\n3,5,6\n4,0,16\n")
    r = post_od(client, "stop-totals", totals2)
    assert r.status_code == 201, r.text
    assert (od_range(client, 1, 4).json()["max_passengers"] == 9)

    # 新核实表与新总数冲突（核实人次超过下车站总数）→ 拒绝，原核实表不动
    r = post_od(client, "verified",
                "board_stop_sequence,alight_stop_sequence,passengers\n2,3,7\n")
    assert r.status_code == 422
    assert any("超过下车站" in e["message"] for e in r.json()["detail"]["errors"])
    st = state(client).json()
    assert st["verified_od"]["rows"] == [
        {"board_stop_sequence": 1, "alight_stop_sequence": 3, "passengers": 0}]

    # 相容的新核实表 → 生效
    r = post_od(client, "verified",
                "board_stop_sequence,alight_stop_sequence,passengers\n1,4,9\n")
    assert r.status_code == 201
    body = od_range(client, 1, 4).json()
    assert (body["min_passengers"], body["max_passengers"]) == (9, 9)


def test_verified_requires_totals_first(client):
    publish(client)
    r = post_od(client, "verified", VERIFIED_ZERO)
    assert r.status_code == 422
    assert any("请先上传" in e["message"] for e in r.json()["detail"]["errors"])
    assert state(client).json()["verified_od"] is None


def test_verified_zero_is_real_constraint(client):
    """核实值 0 不是「忽略」，而是把该站对钉为 0。"""
    publish(client)
    post_od(client, "stop-totals", TOTALS)
    # 钉 x12=0：第 2 站下车 3 人只能来自第 1 站 → 无解
    r = post_od(client, "verified",
                "board_stop_sequence,alight_stop_sequence,passengers\n1,2,0\n")
    assert r.status_code == 422
    # 空核实表（仅表头）= 清除核实约束 → 生效
    r = post_od(client, "verified",
                "board_stop_sequence,alight_stop_sequence,passengers\n")
    assert r.status_code == 201
    assert state(client).json()["verified_od"]["rows"] == []
    assert od_range(client, 1, 3).json()["max_passengers"] == 6


# ---------------- 作用域与参数校验 ----------------

def test_scope_version_date_trip(client):
    publish(client)
    post_od(client, "stop-totals", TOTALS)
    # 另一班次（T2）：无生效底表
    assert od_range(client, 1, 3, trip="T2").status_code == 409
    # 另一运营日（T1 周六不开行）
    assert state(client, date="2026-11-21").status_code == 422
    # 未知班次
    assert state(client, trip="T9").status_code == 404
    # 换版本后：原底表属于旧运行图作用域，新版本下不可见
    upload(client, valid_files(), label="v2")
    assert state(client).json()["stop_totals"] is None
    assert od_range(client, 1, 4).status_code == 409


def test_range_param_validation(client):
    publish(client)
    post_od(client, "stop-totals", TOTALS)
    assert od_range(client, 3, 1).status_code == 422   # 上车须早于下车
    assert od_range(client, 2, 2).status_code == 422
    assert od_range(client, 1, 9).status_code == 422   # 序号不存在
    assert od_range(client, 1, 4, date="bad").status_code == 422


def test_od_requires_published_version(client):
    assert client.get(f"/api/od/trips?date={DATE}").status_code == 404
    assert state(client).status_code == 404


def test_second_trip_independent(client):
    """T2（3 站）独立核算，与 T1 互不影响。"""
    publish(client)
    post_od(client, "stop-totals", TOTALS)
    t2_totals = "stop_sequence,boardings,alightings\n1,5,0\n2,3,2\n3,0,6\n"
    assert post_od(client, "stop-totals", t2_totals, trip="T2").status_code == 201
    r = post_od(client, "verified",
                "board_stop_sequence,alight_stop_sequence,passengers\n1,3,3\n",
                trip="T2")
    assert r.status_code == 201
    body = od_range(client, 1, 3, trip="T2").json()
    assert (body["min_passengers"], body["max_passengers"]) == (3, 3)
    # T1 仍无核实表
    assert state(client).json()["verified_od"] is None


def test_repeated_stop_distinguished_by_sequence(client):
    """环线同一站点多次停靠：两份表用停站序号区分，互不影响。"""
    files = valid_files(
        trips="route_id,service_id,trip_id,trip_headsign,direction_id\n"
              "R2,ALL,L1,环线,0\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "L1,20:00:00,20:00:00,S1,1\n"
                   "L1,20:10:00,20:12:00,S2,2\n"
                   "L1,20:20:00,20:20:00,S1,3\n"   # S1 第二次停靠
                   "L1,20:30:00,20:30:00,S4,4\n")
    assert upload(client, files, label="loop").status_code == 201
    totals = ("stop_sequence,boardings,alightings\n"
              "1,10,0\n2,5,4\n3,2,6\n4,0,7\n")
    assert post_od(client, "stop-totals", totals, trip="L1").status_code == 201
    # 序号 1 的 S1 与序号 3 的 S1 是不同的上车站
    assert post_od(client, "verified",
                   "board_stop_sequence,alight_stop_sequence,passengers\n3,4,2\n",
                   trip="L1").status_code == 201
    body = od_range(client, 3, 4, trip="L1").json()
    assert (body["min_passengers"], body["max_passengers"]) == (2, 2)
    # 序号 1→4 不受序号 3 的核实约束影响
    body = od_range(client, 1, 4, trip="L1").json()
    assert body["max_passengers"] == 5  # 总下车 7，序号3→4 钉走 2
