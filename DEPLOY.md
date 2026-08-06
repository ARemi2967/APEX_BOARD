# 部署指南

## 方式一：本地构建 + 上传镜像（推荐）

### 1. 本地构建镜像

```bash
docker compose build
docker save -o apex-bord.tar apex-bord:latest
# 生成 apex-bord.tar（约 60MB）
```

### 2. 上传到服务器

把 `apex-bord.tar` + `docker-compose.prod.yml` + `.env` 上传到服务器（宝塔文件管理 / scp 均可）。

`.env` 内容：
```
APEX_API_KEY=你的key
DISPLAY_NAME=你的名字
DB_PATH=data/apex.db
SNAPSHOT_INTERVAL_MIN=10
PLATFORM_DEFAULT=PC
```

> API key 必须先在 https://portal.apexlegendsapi.com/discord-auth 绑定 Discord，否则所有请求返回 429。

### 3. 宝塔面板导入镜像

宝塔 → Docker → 镜像 → **导入** → 选择 `apex-bord.tar` → 导入为 `apex-bord:latest`。

如果宝塔不支持 .tar.gz，用终端：
```bash
docker load -i /path/to/apex-bord.tar
```

### 4. 启动容器

宝塔终端（或 SSH）：
```bash
cd /www/wwwroot/apex-bord  # 放 docker-compose.prod.yml 和 .env 的目录
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f  # 等到 scheduler started
```

打开 `http://服务器IP:8000`。

### 更新代码

```bash
# 本地重新构建
docker compose build
docker save -o apex-bord.tar apex-bord:latest
# 上传新 tar → 宝塔重新导入 → 重启
docker compose -f docker-compose.prod.yml up -d
```

> `./data/` 目录（SQLite 数据库）不会被覆盖，历史快照保留。

---

## 方式二：服务器上直接构建

适合服务器能拉到 `python:3.11-slim`（需配置 Docker 镜像加速器）。

```bash
cp .env.example .env
# 编辑 .env

docker compose up -d --build
```

国内服务器需要在 `/etc/docker/daemon.json` 配置阿里云镜像加速器：
```json
{"registry-mirrors": ["https://你的ID.mirror.aliyuncs.com"]}
```

---

## 生产加固

### 反向代理 + HTTPS

用宝塔或 Nginx 反代到 `127.0.0.1:8000`，不要直接暴露 8000 端口。

宝塔：网站 → 添加站点（绑定域名）→ SSL → 反向代理 → 目标 `http://127.0.0.1:8000`。

### 数据备份

定期备份 `./data/apex.db`（全部历史快照）。

### 国内服务器访问 API

`api.mozambiquehe.re` 是境外服务。如果容器报 `transport error` / 502：

`.env` 加代理：
```
HTTPS_PROXY=http://代理地址:端口
HTTP_PROXY=http://代理地址:端口
NO_PROXY=localhost,127.0.0.1
```

或使用**香港/新加坡**地域 ECS，直连无需代理。

---

## 验证

```bash
curl localhost:8000/health          # {"status":"ok"}
curl localhost:8000/api/players     # 你的玩家列表
docker compose logs web | grep "snapshot cycle"  # 定时任务正常运行
```

## 首次使用：数据初始化

API 默认获取不到你的数据，需要先手动解锁：

1. 访问 [apexlegendsstatus.com](https://apexlegendsstatus.com/) 注册账号
2. 绑定你的 Apex 账号（输入游戏 ID + 平台）
3. **打开 Apex 游戏** → 选择一个传奇 → 进入大厅
4. **回到 apexlegendsstatus.com 刷新页面** → 触发数据抓取
5. 现在通过 API 才能拿到战绩

> 只有在游戏内**选中传奇并进入大厅**后，该传奇的数据才会被收录。想看全部 29 个传奇，需要逐个选中 + 回大厅 + 刷新。

## 添加玩家

```bash
curl -X POST localhost:8000/api/players -H 'Content-Type: application/json' \
  -d '{"uid":"你的SteamID64","platform":"PC"}'
```

> UID 查询比名字可靠（名字搜索有时不返回结果）。Steam 玩家用 SteamID64。
