# APEX BOARD 🎮

Apex Legends 个人战绩分析仪表盘。实时获取聚合战绩、定时快照攒时序、趋势可视化。

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.141+-green) ![Docker](https://img.shields.io/badge/Docker-ready-blue) ![License](https://img.shields.io/badge/License-MIT-yellow)

<!-- 截图占位：替换为你的仪表盘截图 -->
<!-- ![dashboard](screenshots/dashboard.png) -->

---

## ✨ 功能

| 模块 | 说明 |
|---|---|
| **实时 KPI** | 段位 / 等级 / 击杀 / 伤害（各传奇求和 = 真实账号总量）|
| **今日战报** | 今日击杀 / 伤害 / RP 变化（按赛季自动排除重置和定级分）|
| **7 日趋势** | 伤害/击杀增量折线、段位分累计折线、段位变化每日柱状图 |
| **各传奇战绩** | 29 个传奇的击杀/伤害（追踪器独立，已全部汉化）|
| **主打传奇** | 自动识别主玩传奇，立绘背景 + 流动灯带 + 近 7 天增量 |
| **武器精通** | 账号级每把武器的击杀 + 伤害（跨快照合并，稳定完整）|
| **每日记录** | 各传奇 × 7 天的击杀/伤害表格 |
| **EA 数据对账** | 上传 EA 账户数据导出（近一年对局真值），自动找出陈旧追踪器漏掉的数据并校准显示（`/admin` 后台）|
| **段位徽章** | 按段位配色（青铜棕→猎杀红）+ 脉冲发光，随赛季自动切换 |
| **实时状态** | 在线 / 大厅 / 游戏中 · 当前传奇 |
| **全中文** | 传奇名/武器名/UI 全汉化（官方简中译名）|

## 🛠 技术栈

- **后端**: Python 3.10+ · FastAPI · httpx · APScheduler · SQLAlchemy 2.0 (async) · SQLite (WAL)
- **前端**: 原生 HTML + ECharts + vanilla JS（零构建）
- **部署**: 单 Docker 容器

## 🚀 快速开始

### 方式一：Docker（推荐）

```bash
# 1. 获取 API key
#    访问 https://apexlegendsapi.com/ 注册免费 key
#    然后 https://portal.apexlegendsapi.com/discord-auth 绑定 Discord（必须）

# 2. 配置
cp .env.example .env
# 编辑 .env，填入你的 APEX_API_KEY 和 DISPLAY_NAME

# 3. 构建并启动（首次自动建表）
docker compose up -d --build

# 4. 打开 http://localhost:8000
```

### 方式二：本地开发

```bash
pip install -e ".[dev]"

# 配置 .env（同上）
cp .env.example .env

# 初始化数据库
python scripts/init_db.py

# 运行测试
pytest

# 启动服务器
uvicorn backend.main:app --reload
```

## 🔑 首次使用前必须完成

API 不会自动获取你的数据，需要先手动初始化：

1. **注册** [apexlegendsstatus.com](https://apexlegendsstatus.com/) 账号
2. **绑定**你的 Apex 账号（输入游戏 ID + 平台）
3. **打开 Apex 游戏**，选择一个传奇，进入大厅
4. **回到 apexlegendsstatus.com 手动刷新页面** — 触发数据抓取并解锁初始信息
5. 现在通过 API 才能获取到你的战绩数据

> ⚠️ 只有你**在游戏内选中传奇并进入大厅**后，该传奇的数据才会被 apexlegendsapi.com 收录。想看全部 29 个传奇的数据，需要逐个在游戏中选中并回大厅，然后回 apexlegendsstatus.com 刷新。这是 EA Stryder 后端的限制，不是本项目的问题。

## ⚙️ 配置

所有配置通过 `.env` 文件管理（参考 `.env.example`）：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `APEX_API_KEY` | apexlegendsapi.com 的 API key（**必填**）| — |
| `DISPLAY_NAME` | banner 显示的名字（上游名字为空时用这个）| YourName |
| `DB_PATH` | SQLite 文件路径 | data/apex.db |
| `SNAPSHOT_INTERVAL_MIN` | 快照间隔（分钟）| 10 |
| `PLATFORM_DEFAULT` | 默认平台 | PC |
| `ADMIN_TOKEN` | `/admin` 后台令牌（EA 导出上传/对账）；留空则整个后台关闭 | 空 |

## 🧭 EA 数据对账（校准陈旧追踪器）

API 的各传奇数据来自**当前装备的追踪器**，很少玩的传奇读数会滞后数月，导致「各传奇战绩」与 KPI 合计偏低。EA 账户的「下载您的EA数据」导出包含**官方每传奇终身生涯数据**（与服务端追踪器同源），可用来直接对账修正：

1. EA 账户 → 隐私设置 → 申请「下载您的EA数据」（等待数天至数周，邮件通知后下载 zip）
2. `.env` 里配置 `ADMIN_TOKEN`，打开 `/admin`，输入令牌
3. 选择玩家 → 上传 zip。重复上传同一文件自动跳过；建议每隔几个月（EA 允许时）重新下载导入，刷新 EA 基线

公式（按传奇、按指标）：`缺失 = max(0, EA官方值 − 追踪器值)`，`校准值 = max(追踪器值, EA官方值)`。健康追踪器与 EA 完全一致（缺失为 0，零误伤）；陈旧追踪器补到官方值；导出日期之后的对局只有追踪器能看到，取较大者保证不会把新鲜数据改小。主面板自动生效：被校准的传奇带「校准」角标（悬浮看 原始值→校准值），KPI 合计同步补上缺失量。

注意：EA 会匿名化部分传奇 ID（显示为 unknown），这些计数无法归属，只在后台报告中单独展示、不计入缺失合计。

## 📡 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/players` | 列出追踪的玩家 |
| POST | `/api/players` | 添加玩家（uid + platform），触发首次快照 |
| DELETE | `/api/players/{id}` | 取消追踪（保留历史）|
| PATCH | `/api/players/{id}` | 修改显示名等 |
| GET | `/api/players/{id}/current` | 实时战绩（30 秒缓存）|
| GET | `/api/players/{id}/history?metric=&days=7` | 时序数据 |
| GET | `/api/players/{id}/deltas?days=1` | 今日变化（赛季感知）|
| GET | `/api/players/{id}/breakdown` | 武器精通 + 各传奇数据 |
| GET | `/api/players/{id}/legend-activity?days=1` | 今日各传奇增量 |
| GET | `/api/players/{id}/legend-daily?days=7` | 每日各传奇记录 |
| GET | `/api/players/{id}/legends` | 各传奇追踪器原始数据 + EA 校准覆盖层 |
| GET | `/api/img?url=` | 图片代理（避免浏览器直连）|
| POST | `/api/admin/ea-import?player_id=` | 上传 EA 导出 zip（body 为文件，需 Bearer ADMIN_TOKEN）|
| GET | `/api/admin/ea-imports` | 导入历史（需鉴权）|
| GET | `/api/admin/ea-report/{id}` | 对账报告：EA 真值 vs 追踪器（需鉴权）|

交互文档：`/docs`

## 🔌 工作原理

```
apexlegendsapi.com  ←(每10分钟)→  ApexClient (httpx, 限速3.3req/s)
        ↓                                    ↓
   /bridge JSON                        capture_snapshot()
        ↓                                    ↓
   parse_bridge()                    SQLite (WAL)
   (各传奇求和)                         ↓
        ↓                          FastAPI + Scheduler
   snapshots 表                     ↓
                                    前端 (ECharts)
```

- **快照机制**：每 N 分钟抓一次 `/bridge`，完整 raw_json + 解析字段存入 SQLite。趋势/记录/增量全从快照算。
- **各传奇求和**：总击杀/伤害 = 所有传奇的追踪器值之和（API 的 total 字段只是单个传奇的值）。
- **赛季感知**：快照记录 `rankedSeason`，RP 增量只算当前赛季内（自动排除赛季重置和定级分）。
- **容错平滑**：终身计数器（击杀/伤害）单调递增，上游返回残值时沿用上一个已知好值。

## ⚠️ 数据源说明

本项目使用 [apexlegendsapi.com](https://apexlegendsapi.com/) 的免费 API。

| 能拿到 ✅ | 拿不到 ❌ |
|---|---|
| 当前聚合战绩（段位/击杀/伤害/等级）| 逐局比赛历史（需白名单/付费）|
| 武器精通（部分武器）| 排行榜（403 Forbidden）|
| 各传奇追踪器（已装备的）| 未装备追踪器的传奇无数据 |
| 实时在线状态 | 过往赛季段位（API 不返回）|

**重要**：
- 免费 key 必须先在 [portal.apexlegendsapi.com/discord-auth](https://portal.apexlegendsapi.com/discord-auth) 绑定 Discord，否则所有请求返回 429。
- 玩家需要**在游戏内选中传奇并进入大厅**，该传奇的数据才会出现在 API 返回中。
- 武器精通数据有**波动**（同一把武器不同请求返回不同值），本项目通过跨快照合并缓解。
- 中国大陆服务器访问 `api.mozambiquehe.re` 需要代理或使用香港/海外地域 ECS。

## 📁 项目结构

```
APEX_BOARD/
├── backend/
│   ├── main.py              # FastAPI 应用工厂 + lifespan
│   ├── config.py            # .env 配置加载
│   ├── api/
│   │   ├── routes.py        # 所有 HTTP 路由
│   │   ├── schemas.py       # Pydantic 模型
│   │   └── errors.py        # 统一错误处理
│   ├── db/
│   │   ├── models.py        # Player + Snapshot ORM
│   │   ├── session.py       # 异步引擎 + WAL
│   │   └── base.py          # DeclarativeBase
│   ├── integrations/
│   │   ├── apex_api.py      # httpx 客户端（限速 + 重试 + 429 分流）
│   │   └── exceptions.py    # 异常层级
│   ├── services/
│   │   └── snapshot_service.py  # 解析 + 平滑 + 快照
│   ├── scheduler/
│   │   └── jobs.py          # APScheduler 定时任务
│   └── tests/               # 54+ 测试
├── frontend/
│   ├── index.html
│   └── assets/
│       ├── css/style.css
│       └── js/{api,charts,app}.js
├── scripts/
│   ├── init_db.py           # 建表
│   ├── backfill_snapshots.py # 手动触发快照
│   └── smoke_test_apex.py   # API 连通测试
├── Dockerfile
├── docker-compose.yml       # 开发（bind-mount frontend）
├── docker-compose.prod.yml  # 生产（纯镜像）
├── pyproject.toml
├── .env.example
└── DEPLOY.md                # 阿里云部署指南
```

## 🤝 贡献

欢迎提 Issue / PR。

## 📄 License

[MIT](LICENSE)
