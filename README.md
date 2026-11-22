# 公交 GTFS 时刻表发布系统

FastAPI + SQLite + 本地文件的公交时刻表发布与查询网页。数据工程师一次性上传同一版本的
5 个 CSV（stops / routes / trips / stop_times / calendar），系统整版校验通过后才发布；
公众按「线路 + 运营日」查询有序停站时刻。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `gtfs-data` 中，容器重启数据不丢。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### 运行测试

```bash
pip install pytest httpx
pytest tests/ -q
```

## 网页功能

- **时刻查询**：选择运营日和线路，展示当日各班次的停站序列与到发时刻；
  跨午夜时刻显示为 `24:20:00` 并标注「+1天」。
- **客流核算**：选择正式版运行图、运营日和当天开行的一趟班次后——
  - 上传**逐停站上下车总数** CSV（`stop_sequence,boardings,alightings`，
    必须恰好覆盖班次每个停站序号一次）；
  - 可另交**已核实起讫站对及人次** CSV
    （`board_stop_sequence,alight_stop_sequence,passengers`，核实值允许为 0，
    表示该站对核实无人）。两份表都用停站序号区分重复停靠；
  - 任选两个先后停站，按同一时点生效的底表组合，算出该站对在
    **所有可行分配中的最少和最多人次**。
- **发布新版本**：一次上传 5 个 CSV + 版本名称。通过则立即生效；
  失败则列出全部错误行（文件 / 行号 / 字段 / 原因），当前正式版不受影响。
- **版本记录**：查看所有版本（含失败版本）及其错误明细。

也可以用 `sample_data/valid/`（可通过）和 `sample_data/invalid/`（含典型错误）
两组样例数据在网页上直接试；`sample_data/od/` 是匹配 valid 版 T1 班次的
客流底表样例。

## 校验规则

任一规则不满足，整版拒绝发布：

1. **结构**：必备列齐全；字段类型合法（非负整数序号、YYYYMMDD 日期、0/1 星期标志、
   HH:MM:SS 时刻）。
2. **表内唯一**：`stop_id` / `route_id` / `service_id` / `trip_id` 各自唯一；
   `stop_times` 内 `(trip_id, stop_sequence)` 唯一。
3. **跨表引用**：`trips.route_id → routes`、`trips.service_id → calendar`、
   `stop_times.trip_id → trips`、`stop_times.stop_id → stops`；
   每个 trip 至少有一条 stop_times。
4. **时刻递增**：同一 trip 按 `stop_sequence` 排序后，每行 `departure ≥ arrival`，
   且下一站到站时刻严格大于上一站发车时刻。
5. **跨午夜**：时刻允许 `HH ≥ 24`（如 `25:10:00`），内部统一换算为
  「自运营日 00:00 起的秒数」比较与存储；`24:10` 之后出现 `23:59` 视为倒退报错。

## 发布与并发语义

- **整版原子发布**：所有校验在内存中完成；通过后在**单个 SQLite 事务**内写入
  整版数据并拨动 `meta.current_version_id` 指针。查询方要么看到旧完整版本、
  要么看到新完整版本，绝不出现半新半旧。
- **失败保旧**：校验失败的版本只记录 `versions(status='failed')` 和错误明细，
  不写入任何时刻数据、不触碰指针。
- **版本不可变**：已发布版本的数据永不修改或删除；请求开始时读一次指针，
  之后该请求内所有查询都固定读取同一版本，换版期间查询结果始终完整一致。
- SQLite 开启 **WAL**，发布写入不阻塞并发查询。

## 客流核算模型与换交语义

把一趟班次的停站依次记为 1..n，设 `x[i][j]`（i<j）为在 i 上车、j 下车的人数。
可行分配须同时满足：每个停站的上/下车总数（行和、列和），以及每条已核实
站对 `x[p][q] = 核实人次`（0 也是硬约束）。这是运输问题，约束矩阵全单位模，
因此整数边际下用最大流即可判定可行性；站对最少人次 = 总人次 − 去掉该边后的
最大流，最多人次由单调二分求得。

- **作用域**：底表挂在（运行图版本, 运营日, 班次）上；换版本后旧底表不影响
  新版本下的同名班次。
- **先总数后核实**：已核实站对表只有在存在生效的上下车总数表时才可提交。
- **分别换交、组合核验**：两份表可分别换交；每次提交都在 `BEGIN IMMEDIATE`
  事务内重新读取**实际生效的组合**（新表 + 另一份生效表）做全趟分配可行性
  核验，交错提交也不会漏验。只有存在同时满足全部约束的分配，换交才生效。
- **失败保旧**：表格有误、组合无解或保存失败时整体回滚，原表和原结果继续
  可查；被拒的提交留痕（含错误明细）可在网页上查看。
- **同一时点底表**：站对区间查询用单条 JOIN 读取当前生效的两份底表，
  保证 min/max 来自同一时点生效的组合。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/versions` | multipart 上传 `label` + 5 个 CSV；成功 201，失败 422 并返回错误行 |
| GET | `/api/versions` | 版本列表（含当前正式版指针） |
| GET | `/api/versions/current` | 当前正式版 |
| GET | `/api/versions/{id}` | 版本详情与错误明细 |
| GET | `/api/routes` | 当前版本的线路列表 |
| GET | `/api/routes/{route_id}/schedule?date=YYYY-MM-DD` | 该运营日的班次与有序停站时刻 |
| GET | `/api/od/trips?date=YYYY-MM-DD` | 当前正式版在该运营日开行的全部班次 |
| GET | `/api/od/state?trip_id=&date=` | 该班次生效底表组合、停站序列与最近提交记录 |
| POST | `/api/od/stop-totals` | multipart 换交上下车总数表（`trip_id`+`date`+`file`）；成功 201，失败 422/500 且原表不动 |
| POST | `/api/od/verified` | multipart 换交已核实站对表；同上 |
| GET | `/api/od/range?trip_id=&date=&board_seq=&alight_seq=` | 站对在所有可行分配中的最少/最多人次 |

### 命令行示例

```bash
# 发布
curl -F "label=2026冬季运行图" \
     -F "stops=@sample_data/valid/stops.csv" \
     -F "routes=@sample_data/valid/routes.csv" \
     -F "trips=@sample_data/valid/trips.csv" \
     -F "stop_times=@sample_data/valid/stop_times.csv" \
     -F "calendar=@sample_data/valid/calendar.csv" \
     http://localhost:8000/api/versions

# 查询夜班线路（含 24:00 之后的跨午夜班次）
curl "http://localhost:8000/api/routes/R2/schedule?date=2026-11-23"

# 客流核算：给 T1 上传两份底表（2026-11-23 周一开行）
curl -F "trip_id=T1" -F "date=2026-11-23" \
     -F "file=@sample_data/od/stop_totals.csv" \
     http://localhost:8000/api/od/stop-totals
curl -F "trip_id=T1" -F "date=2026-11-23" \
     -F "file=@sample_data/od/verified_od.csv" \
     http://localhost:8000/api/od/verified

# 站对 1→4 在所有可行分配中的最少/最多人次
curl "http://localhost:8000/api/od/range?trip_id=T1&date=2026-11-23&board_seq=1&alight_seq=4"
```

## 目录结构

```
app/
  main.py        FastAPI 端点（发布 / 查询 / 版本 / 客流核算）
  gtfs.py        GTFS CSV 解析与整版校验（纯标准库）
  odflow.py      客流底表解析校验 + 全趟分配可行性 + 站对上下界（纯标准库）
  db.py          SQLite schema、原子发布、版本指针、底表原子换交
  static/index.html  网页（查询 / 客流核算 / 发布 / 版本记录）
tests/           pytest 单元与集成测试
sample_data/     可通过与含错误的样例 CSV（od/ 为客流底表样例）
Dockerfile / docker-compose.yml
```
