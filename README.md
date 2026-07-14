# grokcli-2api

把 **Grok OIDC 登录态** 转成 **OpenAI / Anthropic 兼容 API**，并附带 Web 管理台：多 API Key、多账号轮询、设备码 / SSO / JSON 导入导出、协议注册。

**当前版本：v1.9.49（注册任务日志 · 进度硬刷新恢复）**

[![GHCR](https://img.shields.io/badge/ghcr.io-hm2899%2Fgrokcli--2api-blue)](https://github.com/users/HM2899/packages/container/package/grokcli-2api)

- **独立运行**：不依赖本地 Grok CLI / 浏览器 OAuth
- **Hybrid 存储（默认强制）**：PostgreSQL 持久 + Redis 热状态 + 多 Worker
- **协议注册**：内置 `grok-build-auth`（纯 HTTP，无需 Chromium）
- **中继友好**：兼容 new-api / sub2api / Claude Code 工具流
- **大账号池**：Token 自动续期、模型健康探测、冷却状态落库
- **任务可观测**：后台任务写入「任务日志」；SSO / JSON 导入导出带实时进度

---

## 架构

```
客户端 (OpenAI / Anthropic SDK · new-api · Claude Code / sub2api)
        │  /v1/chat/completions  ·  /v1/responses  ·  /v1/messages
        ▼
  grokcli-2api  (FastAPI · multi-worker)
        │  管理台 /admin
        │  账号轮询 · 失败切换 · 对话粘性
        │  任务日志（注册 / SSO / JSON / 测活 / 续期）
        │  PostgreSQL（账号 / Key / 设置 / 冷却 / 任务日志）—— 容器内网
        │  Redis（粘性 / 计数 / 锁 / 会话 / 任务进度）—— 容器内网
        ▼
  cli-chat-proxy.grok.com
```

> `data/*.json` **仅作旧版迁移源或可选镜像**，运行时权威数据在 PostgreSQL。

---

## 功能一览

| 功能 | 说明 |
|------|------|
| OpenAI 兼容 | `/v1/models` · `/v1/chat/completions` · `/v1/responses` · SSE |
| Anthropic 兼容 | `/v1/messages` · tools / tool_use · `count_tokens` |
| 管理台 | 账号、Key、协议注册、测活、续期、**任务日志**、用量、设置 |
| 多账号轮询 | `round_robin` / `least_used` / `random` |
| 冷却状态 | free-usage 等写入 DB；测活成功恢复为「冷却中」→ 正常 |
| Token 续期 | 后台 leader 维护；支持单选/多选立即续期 |
| 模型探测 | 单账号 / 多选批量 / 全量；状态实时回填 |
| 协议注册 | MoeMail / YYDS / GPTMail + 本地内联过盾 / YesCaptcha，多线程批量；入池后延迟测活 |
| SSO 导入 | Cookie → Device Flow，**后台任务 + 实时进度条** |
| JSON 导入/导出 | 多文件导入 / 全部·选中导出，**后台任务 + 进度条 + 完成后下载** |
| 任务日志 | 注册、SSO、JSON、测活、续期等结果落 PG，可按类型/状态/关键词查询 |
| 用量统计 | 代理侧 token / 请求：今日·近 N 天·累计；按 Key / 账号 / 模型；请求明细 |

---

## 快速开始

### 方式 A：Docker Compose（推荐）

```bash
git clone https://github.com/HM2899/grokcli-2api.git
cd grokcli-2api
cp .env.example .env
# 编辑 .env：至少改 GROK2API_ADMIN_PASSWORD；生产请改 Postgres 密码

docker compose up -d --build
curl -fsS http://127.0.0.1:3000/health
```

浏览器打开：`http://127.0.0.1:3000/admin`

#### 启动时指定打码线程数

主容器内联过盾线程数由 `TURNSTILE_THREAD` 控制（默认与注册并发一致，当前默认 **3**）：

```bash
# compose 启动时直接传参
TURNSTILE_THREAD=3 GROK2API_REG_CONCURRENCY=3 docker compose up -d --build

# 或写入 .env
# GROK2API_CAPTCHA_PROVIDER=local
# GROK2API_INLINE_SOLVER=1
# GROK2API_REG_CONCURRENCY=3
# TURNSTILE_THREAD=3
```

| 变量 | 默认 | 说明 |
|------|------|------|
| `GROK2API_CAPTCHA_PROVIDER` | `local` | `local`（容器内联）/ `yescaptcha` |
| `GROK2API_INLINE_SOLVER` | `1` | `1` 时入口脚本在主容器内启动过盾 |
| `GROK2API_REG_CONCURRENCY` | `3` | 协议注册默认并发 |
| `TURNSTILE_THREAD` | `= REG_CONCURRENCY` | 本地过盾浏览器线程数 |
| `TURNSTILE_BROWSER_TYPE` | `camoufox` | 过盾浏览器类型 |
| `TURNSTILE_PORT` | `5072` | 内联过盾监听端口（容器内 loopback） |

> 2 核小机器建议 `TURNSTILE_THREAD=1~2`；`3` 已较重，`5` 容易把 CPU/内存打满。

**默认只映射应用端口 `3000`（内联部署）。**  
栈内 **PostgreSQL / Redis / 本地过盾** 都不绑定宿主机端口：

| 服务 | 容器内地址 | 是否映射到宿主机 |
|------|------------|------------------|
| app | `0.0.0.0:3000` | 是 → `127.0.0.1:3000` |
| postgres | `postgres:5432` | **否**（compose 内网） |
| redis | `redis:6379` | **否**（compose 内网） |
| 本地过盾 | `127.0.0.1:5072` | **否**（主容器 loopback 内联） |

因此 compose 里应用环境变量应使用服务名，而不是 `127.0.0.1`：

```env
REDIS_URL=redis://redis:6379/0
DATABASE_URL=postgresql://grok2api:grok2api@postgres:5432/grok2api
```

> `.env.example` 中的 `127.0.0.1` 仅适用于「本机直接跑 Python、自己起 DB」的场景。  
> `docker compose` 启动时会用 `docker-compose.yml` 中的服务名覆盖，无需改成宿主机端口。

若你**确实**需要从宿主机连库调试，可在本地 `docker-compose.override.yml` 临时加 `ports`（该文件已 gitignore，勿提交）。

### 方式 B：GHCR 镜像（注意小写）

Docker / GHCR **镜像名必须全小写**。仓库 owner 可能是 `HM2899`，但拉取时要用：

```text
ghcr.io/hm2899/grokcli-2api
```

**错误示例（会拉失败）：** `ghcr.io/HM2899/grokcli-2api`  
**正确示例：**

```bash
docker pull ghcr.io/hm2899/grokcli-2api:1.9.49
# 或
docker pull ghcr.io/hm2899/grokcli-2api:latest
```

最小 compose 示例（内联 redis + postgres，**不要**给 DB 映射宿主机端口）：

```yaml
services:
  redis:
    image: redis:7-alpine
    # 不要 ports —— 仅容器网络内访问
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: grok2api
      POSTGRES_PASSWORD: change-me
      POSTGRES_DB: grok2api
    volumes:
      - grok2api_pg:/var/lib/postgresql/data
    # 不要 ports —— 仅容器网络内访问
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U grok2api -d grok2api"]
      interval: 5s
      timeout: 5s
      retries: 10

  grokcli-2api:
    image: ghcr.io/hm2899/grokcli-2api:1.9.49
    ports:
      # 只映射应用；不要给 postgres/redis 加 ports
      - "3000:3000"
    environment:
      GROK2API_HOST: "0.0.0.0"
      GROK2API_PORT: "3000"
      GROK2API_ADMIN_PASSWORD: "change-me"
      GROK2API_STORE_BACKEND: "hybrid"
      GROK2API_REQUIRE_SHARED_STORES: "1"
      GROK2API_WORKERS: "4"
      # 内联本地过盾（主容器 loopback，无需对外端口）
      GROK2API_CAPTCHA_PROVIDER: "local"
      GROK2API_INLINE_SOLVER: "1"
      REDIS_URL: "redis://redis:6379/0"
      DATABASE_URL: "postgresql://grok2api:change-me@postgres:5432/grok2api"
    volumes:
      - ./data:/app/data
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy

volumes:
  grok2api_pg:
```

若包为 private，需先登录：

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

### 必要环境变量

| 变量 | 说明 |
|------|------|
| `GROK2API_ADMIN_PASSWORD` | 管理台密码**首次种子**（无库内哈希时导入；之后以数据库为准） |
| `GROK2API_STORE_BACKEND=hybrid` | 生产模式 |
| `GROK2API_REQUIRE_SHARED_STORES=1` | Redis/PG 不可用则拒绝启动 |
| `REDIS_URL` | Compose 内：`redis://redis:6379/0` |
| `DATABASE_URL` | Compose 内：`postgresql://…@postgres:5432/…` |
| `GROK2API_WORKERS` | 建议 ≥2（按 CPU） |
| `GROK2API_RELOAD` | 开发热更新：`1` 开启（强制单 worker）；生产保持 `0` |

完整模板见 [`.env.example`](./.env.example)。**生产请修改默认数据库密码。**

### 本地开发热更新

生产默认 `reload=False` + 多 worker。改代码后要自动重启：

```bash
# 仅起 Redis/Postgres（若尚未运行）
docker compose up -d postgres redis

# 宿主机 Python 热更新（监听 .py / static/js / static/admin）
./dev.sh
# 或
GROK2API_RELOAD=1 GROK2API_WORKERS=1 python app.py
```

说明：
- `GROK2API_RELOAD=1` 时强制 **1 worker**（uvicorn 限制）
- 默认忽略 `data/`、`static/dist/`、`__pycache__/`，避免写库/打包触发无意义重启
- 管理台 `static/js` 源文件变更会触发进程重启；带 hash 的 `static/dist` 仍建议跑 `python scripts/build_admin_assets.py`
- Docker 镜像内一般不挂源码，热更新请用宿主机 `./dev.sh`，或 bind-mount 代码后再设 `GROK2API_RELOAD=1`

---

## 从旧版（JSON 文件）升级

详见 **[docs/UPGRADE.md](./docs/UPGRADE.md)**。

```bash
# 备份 data/ 后
chmod +x scripts/upgrade_from_file_backend.sh
./scripts/upgrade_from_file_backend.sh --data-dir ./data

# 或
docker compose up -d redis postgres
docker compose run --rm \
  -e DATABASE_URL=postgresql://grok2api:grok2api@postgres:5432/grok2api \
  grokcli-2api \
  python migrate_json_to_pg.py --data-dir /app/data --merge-pool
```

迁移内容：`auth.json` / `keys.json` / `settings.json`（含账号池状态）→ PostgreSQL。  
不迁移：Redis 热状态、管理台登录会话。

已是 hybrid 时，拉新镜像即可；表结构由 `store/pg.py` 启动时幂等升级。

---

## 客户端接入

### OpenAI 兼容

```bash
export OPENAI_BASE_URL=http://127.0.0.1:3000/v1
export OPENAI_API_KEY=你的管理台API_Key

curl "$OPENAI_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"hi"}]}'
```

### Anthropic 兼容

```bash
curl http://127.0.0.1:3000/v1/messages \
  -H "x-api-key: 你的管理台API_Key" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.5","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}'
```

Claude Code / Cursor / Cherry Studio：Base URL 填服务地址（通常带 `/v1`），Key 用管理台创建的 API Key。

---

## 管理台

| 页面 | 用途 |
|------|------|
| 概览 | 池规模、续期/探测状态、今日用量 |
| 账号 / 轮询 | 设备码、**SSO 导入（进度）**、**JSON 导入/导出（进度）**、协议注册、测活、续期 |
| API Keys | 客户端密钥 |
| 用量 | Token / 请求：今日·近 N 天·累计；Key / 账号 / 模型；请求明细 |
| 任务日志 | 协议注册、SSO、JSON 导入导出、测活、Token 续期等后台任务结果 |
| 设置 | 轮询与冷却策略、协议注册默认项等 |

### 账号导入 / 导出

| 方式 | 说明 |
|------|------|
| SSO Cookie | 粘贴或上传；后台 Device Flow 换 token，页面显示进度条与明细 |
| JSON 文件 | 支持多文件合并导入；解析 → 入库全程进度 |
| 导出全部 / 选中 | 后台打包，完成后自动下载；大池不阻塞页面 |

导入导出、测活、续期等完成后，可在 **任务日志** 按类型 / 状态 / 关键词查询历史结果。

### 协议注册

依赖 **临时邮箱** + **过盾**（环境变量或管理台配置，存 PG）：  
- 邮箱：`MoeMail` / **YYDS Mail**（[vip.215.im](https://vip.215.im/docs)）/ **GPTMail**（[mail.chatgpt.org.uk](https://mail.chatgpt.org.uk/zh/api/)）  
- 过盾：本地内联 Turnstile Solver 或 YesCaptcha  

本地过盾默认与主容器同进程（`127.0.0.1:5072`），**无需填写 URL**；选 YesCaptcha 时仅用云端 Key。  
邮箱有效期：MoeMail 支持 1 小时 / 1 天 / 3 天 / 永久；YYDS / GPTMail 临时邮箱约 24 小时。  
新注册账号入池后默认 **延迟 30s** 再自动测活；可在管理台「测活等待秒」调整，或用环境变量 `GROK2API_REG_PROBE_DELAY_SEC`（`0`=立即测活）。

---

## 运维

```bash
curl -fsS http://127.0.0.1:3000/health
curl -fsS http://127.0.0.1:3000/metrics | head
docker compose logs -f grokcli-2api
```

- 仅 **leader** worker 跑 Token 续期与模型健康任务（Redis 选主）
- 备份重点：**PostgreSQL 卷**（`grok2api_pg`）；Redis 可丢
- 本地低停机重建：`./docker-rebuild.sh`
- Postgres / Redis **默认不暴露宿主机端口**，降低误扫与误连风险
- 任务日志表 `task_logs` 在 hybrid 启动时幂等创建

### 发布镜像（GHCR）

```bash
# app.py 中 APP_VERSION 必须与 tag 一致（且镜像路径全小写）
git tag v1.9.49
git push origin v1.9.49
```

成功后可拉取（**必须小写**）：

- `ghcr.io/hm2899/grokcli-2api:1.9.49`
- `ghcr.io/hm2899/grokcli-2api:latest`（打 `v*` tag 时）
- `ghcr.io/hm2899/grokcli-2api:edge`（`main` 分支）

CI 会把 `github.repository` 强制转成小写后再推送，避免 `HM2899` 大小写导致 `docker pull` 失败。

---

## 目录提示

```
app.py / admin_routes.py              # API 与管理路由（含异步导入导出任务）
task_log.py / store/task_logs_pg.py   # 任务日志写入与查询
store/                                # Redis + PostgreSQL 后端
migrate_json_to_pg.py                 # JSON → PG
scripts/upgrade_from_file_backend.sh  # 旧版升级包装
scripts/build_admin_assets.py         # 管理台静态资源打包
docs/UPGRADE.md                       # 升级说明
static/                               # 管理台前端
grok-build-auth/                      # 协议注册引擎（vendored）
turnstile-solver/                     # 本地过盾（默认内联进主容器；懒加载 + 空闲回收）
docker-compose.yml                    # redis + postgres（内网）+ app（内联过盾）
.github/workflows/docker-publish.yml  # GHCR 多架构构建（小写镜像名）
```

---

## 安全与免责

- 勿将 `.env`、`data/`、真实 Token 提交到 Git
- 生产务必修改 Postgres 密码与管理员密码
- 默认不映射 DB/Redis 端口；需要调试时用本地 override，勿对公网暴露
- 导出 JSON 含完整 token，请妥善保管下载文件
- 协议注册与账号自动化请遵守 xAI 服务条款与当地法律；本项目仅供自用/研究集成

---

## 版本

- **v1.9.49**（当前）：
  - **注册任务日志**：单次/批量协议注册启动与结束都写入「任务日志」(kind=`register`)
  - **注册进度硬刷新恢复**：batch/session id 写入 sessionStorage，刷新后按 id 精确回拉进度框
  - 任务日志类型筛选项预置 `register` 等常见 kind
- **v1.9.48**：
  - **注册进度恢复**：刷新 / soft-nav 后自动从后端恢复进行中的协议注册，进度日志不再空白
  - **任务日志详情**：刷新后保留选中行详情（不再依赖脆弱 HTML 属性）
  - **空闲回收加固**：Turnstile 浏览器关闭更彻底；`in_flight` 泄漏可强制回收；新增 `/health` 与 `/reclaim`
  - 管理台模型列表走专用 `/models` 目录加载，避免大池 `/dashboard` 导致空白
  - API 转发 / Token 续期 / 模型测活路径不受影响
- **v1.9.47**：
  - **空闲降内存**：内联 Turnstile 浏览器池默认懒加载（`TURNSTILE_LAZY=1`），首次过盾再 warm
  - **空闲回收**：`TURNSTILE_IDLE_SEC`（默认 180s）无验证码活动后关闭 Camoufox，避免空转占 1G+
  - 默认 `GROK2API_WORKERS` 调为 **2**（可用环境变量覆盖）；显式允许 `1`
  - API 转发 / Token 续期 / 模型测活不受影响；注册首次过盾可能多几秒冷启动
- **v1.9.46**：
  - 协议注册新增 **Cloudflare Temp Email**（[dreamhunter2333/cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email)）
  - 邮箱服务 Key / 域名 / Base URL **按服务独立保存**，切换不串值
  - Token 续期：默认软禁用永久失效号（不再误硬删）；multi-worker 自动重选 leader
  - 流式兼容：空 200 / Update 工具参数 / 心跳与超时加固；兼容前提下降低首包延迟
- **v1.9.45**：
  - **YYDS 邮箱域名留空**时从公开目录 **自动随机获取**（不再误用 MoeMail 默认域名）
  - 修复 hybrid 多 worker 下协议注册配置保存后读旧缓存（PG 优先）
- **v1.9.44**：
  - 管理台「日志」改为 **任务日志**（注册 / SSO / JSON 导入导出 / 测活 / Token 续期）
  - **JSON 导入 / 导出** 后台任务 + 实时进度条（全部导出、选中导出）
  - **SSO 导入** 异步任务与进度轮询（多线程转换 + 批量入库）
  - 协议注册入池后可配置 **测活等待秒**；邮箱域名删空后不再被回填
  - SSO 导入并发与轮询参数可配置（见 `.env.example`）
- **v1.9.39**：修复 YYDS/GPTMail 域名删空后被自动回填
- **v1.9.38**：
  - 协议注册邮箱：**MoeMail / YYDS / GPTMail** 可选；Key 与域名按服务独立落库
  - Docker **内联** hybrid：仅暴露应用端口；Postgres / Redis / 本地过盾均不映射宿主机
  - 兼容前提下首字优化（粘性快路径、选号与 body 并行、连接池预热等）
- **v1.9.25–1.9.37**：见提交历史（注册停止/进度、内联过盾、Responses 工具流等）
- **v1.9.22**：hybrid 下 live 账号读 PG；compose 取消 Postgres/Redis 宿主机端口；GHCR 小写镜像名
- **v1.9.21**：OpenAI Responses API + 用量统计
- **v1.9.19**：高并发 hybrid 默认（PostgreSQL + Redis + multi-worker）；GHCR 多架构
- 更早变更见 [GitHub Releases](https://github.com/HM2899/grokcli-2api/releases)

> 镜像 tag 与 `app.py` 中 `APP_VERSION` 一致（当前 **1.9.49**）。推 `main` 会打 `edge` 与版本号；打 `v1.9.49` tag 会额外发布 `latest`。  
> 拉取路径固定为 **`ghcr.io/hm2899/grokcli-2api`**（全小写）。

## License

见 [LICENSE](./LICENSE)。
