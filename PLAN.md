# Apex Legends 战绩分析仪表盘 — 项目方案

## 目标

单人 / 单设备部署的 Apex Legends 个人战绩追踪仪表盘。通过定时轮询第三方 API（apexlegendsapi.com `/bridge`）攒时序快照，提供实时聚合数据 + 历史趋势可视化。

## 范围划定

### MVP（4 天内完成，仅用 bridge 聚合数据）
- 数据源：apexlegendsapi.com `/bridge` 端点（当前段位、累计击杀/伤害/胜场、传奇数据）
- 定时快照：每 10–15 min 拉一次，存 SQLite
- 后端 API：FastAPI 提供实时代理 + 时序查询
- 前端：静态 HTML + ECharts，KPI 卡 + 折线趋势 + 饼图
- 部署：单 Docker 容器

### 二期（match history 白名单批下来后再做）
- 比赛级数据：placement、character、武器、队友
- 段位按局变化曲线
- 最近 N 场列表 + 详情
- Discord webhook 通知（升级、新成就）
- 多账号聚合视图
- 白名单通过后的历史回填

---

## 阶段拆分

### Phase 0 — 项目脚手架（Day 1 / ~1h）
**任务**
- 创建目录结构与占位空文件
- 写 `pyproject.toml`（依赖：fastapi、uvicorn、httpx、sqlalchemy、aiosqlite、apscheduler、tenacity、pydantic-settings、pytest）
- 写 `.env.example`、`.gitignore`、`README.md` 占位

**验收标准**
- [ ] 目录树与方案一致
- [ ] `pip install -e .` 成功
- [ ] `.env.example` 列出所有必需变量（APEX_API_KEY、DB_PATH、SNAPSHOT_INTERVAL_MIN、PLATFORM_DEFAULT）

**涉及文件**
- `pyproject.toml`, `.env.example`, `.gitignore`, `README.md`

---

### Phase 1 — 数据抓取层（Day 1 / ~3h）
**任务**
- 实现 `apex_api.py`：httpx async 客户端，封装 `/bridge?player=X&platform=Y`
- 限速：asyncio.Semaphore 限制并发 ≤ 4（留 1 req/s 余量给前端实时拉取）
- 错误分类：401/403（key 问题）、404（player 不存在）、429（限速）、5xx（上游故障）
- 重试：tenacity 指数退避（仅 429 / 5xx，最多 3 次）
- 日志：每次请求记录 player、platform、耗时、状态码

**验收标准**
- [ ] 给定真实 player + platform，能返回 bridge JSON
- [ ] mock 429 → 退避后重试成功
- [ ] mock 404 → 抛 `PlayerNotFoundError`，不被重试
- [ ] 单元测试覆盖正常路径 + 至少 2 个错误路径

**涉及文件**
- `backend/integrations/apex_api.py`
- `backend/integrations/exceptions.py`
- `backend/tests/test_apex_api.py`

---

### Phase 2 — 存储层（Day 2 上午 / ~3h）
**任务**
- 设计 schema：
  - `players`: id, username, platform, uid（Apex 内部 ID）, tracked (bool), created_at, updated_at
  - `snapshots`: id, player_id (FK), captured_at, raw_json (TEXT), bp_level, rank_tier, rank_score, kills, damage, wins, matches_played
  - 索引：`(player_id, captured_at DESC)`、`players.uid` 唯一
- SQLAlchemy 2.0 ORM models
- engine/session：SQLite + WAL 模式 + `PRAGMA foreign_keys=ON`
- `init_db.py`：建表脚本

**验收标准**
- [ ] `python scripts/init_db.py` 后 `data/apex.db` 生成
- [ ] `sqlite3 .schema` 输出包含两张表 + 索引
- [ ] 能插入 player + 关联 snapshot，外键约束生效（删 player 级联删 snapshot）
- [ ] `PRAGMA journal_mode` 返回 `wal`

**涉及文件**
- `backend/db/base.py`, `session.py`, `models.py`
- `scripts/init_db.py`

---

### Phase 3 — 定时快照任务（Day 2 下午 / ~3h）
**任务**
- `snapshot_service.py`：取一个 player → 调 `apex_api` → 解析关键字段 → 写 snapshot（含 raw_json）
- APScheduler（AsyncIOScheduler）：默认每 10 min 跑一次（环境变量可调）
- 失败隔离：单个 player 抓取失败不影响其他
- 启动时自动加载 `tracked=True` 的 player 列表
- `backfill_snapshots.py`：手动触发单 player / 全量 snapshot

**验收标准**
- [ ] 启动 scheduler 后，DB 每 10 min 多一条 snapshot
- [ ] 故意断网时 scheduler 不挂，恢复后下次正常抓取
- [ ] `python scripts/backfill_snapshots.py --player <id>` 能手动拉一次
- [ ] 日志可见每次 snapshot 的成功/失败/耗时

**涉及文件**
- `backend/services/snapshot_service.py`
- `backend/scheduler/jobs.py`
- `scripts/backfill_snapshots.py`

---

### Phase 4 — API 层（Day 3 / ~3h）
**任务**
- FastAPI 应用 + uvicorn 入口（与 scheduler 同进程）
- 路由：
  - `GET /api/players` — 列出跟踪玩家
  - `POST /api/players` — 添加（body: username, platform）→ 触发首次 snapshot
  - `DELETE /api/players/{id}` — 取消跟踪（不删数据）
  - `GET /api/players/{id}/current` — 实时代理 bridge（30s 内存缓存）
  - `GET /api/players/{id}/history?metric=&days=7` — 时序点
  - `GET /api/players/{id}/legends` — 当前传奇数据（从最近 snapshot 解析）
  - `GET /health` — 健康检查
- Pydantic schemas
- CORS：允许前端域
- 统一错误响应 `{error: {code, message}}`

**验收标准**
- [ ] curl 每个端点 200 + 正确 schema
- [ ] POST player 后立刻触发一次 snapshot
- [ ] `/current` 30s 内重复请求只打一次上游
- [ ] `/history?metric=damage&days=7` 返回 7 天的时序点
- [ ] player not found → 404 + 统一错误格式

**涉及文件**
- `backend/main.py`
- `backend/api/routes.py`, `schemas.py`, `errors.py`
- `backend/config.py`

---

### Phase 5 — 前端可视化（Day 3 晚 + Day 4 上午 / ~5h）
**任务**
- 用 `dashboard-creator` skill 产出骨架，`data-visualization` skill 辅助图表选型
- 布局：
  - 顶部：player 选择器 + 上次更新时间 + 手动刷新按钮
  - KPI 卡：当前段位 / BP 等级 / 累计击杀 / 累计伤害 / 胜场 / 匹配数
  - 主图：7 天指标折线（默认 damage，可切换 kills / wins）
  - 副图：传奇使用饼图 + 段位阶梯图
- 配色：Apex 暗主题（深背景 + 红橙强调色 #DA292A / #FFA552）
- ECharts dark theme
- JS 模块化：`api.js`（fetch 后端）/ `charts.js`（ECharts 实例）/ `app.js`（状态 + 事件）
- 响应式（移动端可读）

**验收标准**
- [ ] 浏览器打开能看到自己实时数据
- [ ] 切 player / metric 图正常刷新
- [ ] 即使只有几小时数据，趋势图也能渲染（不空白）
- [ ] 浏览器 console 无 error
- [ ] 移动端宽度 375px 可读

**涉及文件**
- `frontend/index.html`
- `frontend/assets/css/style.css`
- `frontend/assets/js/api.js`, `charts.js`, `app.js`

---

### Phase 6 — 部署与收尾（Day 4 下午 / ~2h）
**任务**
- `Dockerfile`：Python slim + 拷贝代码 + uvicorn 启动（含 scheduler）
- `docker-compose.yml`：volume 挂 `data/`、env 注入 API key、healthcheck
- `README.md`：使用说明（申请 key、配置、启动）
- 健康检查端点 + 自动重启

**验收标准**
- [ ] `docker compose up` 一键起服务
- [ ] 容器重启后 SQLite 数据不丢
- [ ] `docker compose down` 干净停止

**涉及文件**
- `Dockerfile`, `docker-compose.yml`, `README.md`

---

### Phase 7 — 二期：match history 集成（白名单批下来后）
**前置条件**：apexlegendsapi.com Discord 白名单申请通过

**任务（待白名单后细化）**
- 抓取 `/v3/matches` 端点
- 新增 `matches` 表：match_id, player_id, legend, placement, kills, damage, started_at, duration_s, teammates_json
- 比赛级视图：placement 分布、按传奇筛选
- 最近 N 场列表 + 详情卡
- Discord webhook：段位升级 / 单场 ≥10 kills 推送
- 多账号聚合 dashboard
- 历史回填脚本（首次拉取过去 N 场）

---

## 关键技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| HTTP 客户端 | httpx (async) | 与 FastAPI 异步栈一致 |
| ORM | SQLAlchemy 2.0 + aiosqlite | 异步友好 |
| 调度器 | APScheduler AsyncIO | 单进程内嵌，无需 Celery |
| 缓存 | 内存 LRU（30s） | 简单够用，不引 Redis |
| 时序存储 | 单表 + captured_at 索引 | 数据量小，不需要专门 TS DB |
| 前端框架 | 无框架，原生 JS + ECharts | 静态托管、零构建 |
| 部署 | 单 Docker 容器 | 一台机器/NAS/小 VPS 都能跑 |

---

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| API key 限速（5 req/s） | 信号量限并发 ≤ 4 + 30s 缓存 + 失败退避 |
| 上游字段变更 | snapshot 存原始 raw_json，解析层独立 |
| 上游宕机 | scheduler 单次失败不影响整体，下次自动重试 |
| SQLite 写锁竞争 | WAL 模式 + 写操作短事务 |
| Player 改名 | 按 uid 关联而非 username |
| 历史 snapshot 缺失（首次部署） | 前端容差：数据不足时显示"数据积累中" |

---

## 目录结构（占位）

```
APEX_bord/
├── PLAN.md
├── README.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── backend/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── apex_api.py
│   │   └── exceptions.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── session.py
│   │   └── models.py
│   ├── services/
│   │   ├── __init__.py
│   │   └── snapshot_service.py
│   ├── scheduler/
│   │   ├── __init__.py
│   │   └── jobs.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py
│   │   ├── schemas.py
│   │   └── errors.py
│   └── tests/
│       ├── __init__.py
│       ├── test_apex_api.py
│       ├── test_snapshot_service.py
│       └── test_api.py
├── frontend/
│   ├── index.html
│   └── assets/
│       ├── css/style.css
│       └── js/{api,charts,app}.js
├── scripts/
│   ├── init_db.py
│   └── backfill_snapshots.py
└── data/
    └── .gitkeep
```

---

## MVP 完工定义（Definition of Done）

单条 `docker compose up` 起来后：

1. 通过 web UI 输入自己的 player + platform 添加跟踪
2. 立刻看到当前段位 / 击杀 / 伤害等 KPI
3. 等 10–15 min 后再访问，趋势图开始有数据点
4. 第二天访问，7 天折线图清晰可见
5. 上游 API 故障时，前端展示"上次成功时间"，不白屏
