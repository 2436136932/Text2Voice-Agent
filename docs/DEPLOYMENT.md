# 部署指南

本文档覆盖手动部署、进程托管、Nginx 反向代理、生产加固、备份与故障排查。

---

## 目录

- [1. 部署架构](#1-部署架构)
- [2. 快速部署（手动）](#2-快速部署手动)
- [3. 生产部署（Nginx + systemd）](#3-生产部署nginx--systemd)
- [4. Windows 部署](#4-windows-部署)
- [5. 生产加固](#5-生产加固)
- [6. 数据备份与迁移](#6-数据备份与迁移)
- [7. 升级与回滚](#7-升级与回滚)
- [8. 监控与日志](#8-监控与日志)
- [9. 故障排查](#9-故障排查)

---

## 1. 部署架构

前后端分离：后端是 FastAPI 进程（uvicorn），前端是静态文件（Vite 构建产物）。

```
                    ┌────────────────────────────────────────────┐
   浏览器 ─────────►│  Nginx  :80 / :443                          │
                    │    ├─ /            → 前端静态文件 dist/      │
                    │    ├─ /api/        → 反向代理                │
                    │    └─ /docs        → 反向代理                │
                    └────────────────┬───────────────────────────┘
                                     │ http://127.0.0.1:8000
                    ┌────────────────▼───────────────────────────┐
                    │  uvicorn (FastAPI)  :8000                   │
                    │    ├─ SQLite   backend/data/text2voice.db   │
                    │    └─ 音频     backend/data/audio/*         │
                    └────────────────────────────────────────────┘
```

**最小部署**：只跑后端（`python -m app.main`）+ 前端开发服务器（`npm run dev`）即可完整使用。
**生产部署**：前端 `npm run build` 出静态文件，交给 Nginx 托管并反代 `/api`。

| 组件 | 运行形态 | 端口 |
| --- | --- | --- |
| 后端 | Python 进程（uvicorn） | 8000 |
| 前端（生产） | Nginx 静态托管 | 80 / 443 |
| 前端（开发） | Vite dev server | 5173 |

---

## 2. 快速部署（手动）

### 2.1 前置条件

| 组件 | 版本 |
| --- | --- |
| Python | ≥ 3.10（3.13 已验证） |
| Node.js | ≥ 20（仅构建前端需要） |
| 操作系统 | Linux / macOS / Windows |

### 2.2 后端

```bash
cd backend

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate          # Linux / macOS
# source .venv/Scripts/activate    # Windows (Git Bash)

# 安装依赖
pip install -r requirements.txt

# 复制配置模板（可选）
cp ../.env.example .env

# 启动
python -m app.main
```

启动成功后：

```bash
curl -s http://localhost:8000/health
# {"status":"ok","version":"1.0.0","database":"ok","providers":{"llm":9,"tts":7},...}
```

- Swagger 文档：<http://localhost:8000/docs>
- 首次启动会自动创建 `backend/data/text2voice.db` 与 `backend/data/audio/`

**此时就已经可以完整使用了**——打开 `/docs` 在线调试，或用 curl 调用接口。

### 2.3 前端

#### 开发模式（推荐日常使用）

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。Vite 已配置把 `/api` 代理到 `http://localhost:8000`，
后端保持运行即可，无需其他配置。

#### 生产模式（构建静态文件）

```bash
cd frontend
npm ci
npm run build            # 产物输出到 frontend/dist/
```

`dist/` 是纯静态文件，用任意静态服务器托管即可（Nginx / Caddy / Apache）。
注意：**必须配置 `/api` 反向代理**，否则前端无法访问后端——
见下一节的 Nginx 配置。

### 2.4 验证部署

```bash
# 后端健康检查
curl -s http://localhost:8000/health

# 服务商目录
curl -s http://localhost:8000/api/v1/providers | head -c 200

# 端到端（52 项检查，含密钥泄漏检查）
bash scripts/smoke_test.sh http://localhost:8000

# 前后端契约一致性
python scripts/check_contract.py http://localhost:8000
```

---

## 3. 生产部署（Nginx + systemd）

### 3.1 目录规划

```
/opt/text2voice/
├── backend/
│   ├── app/
│   ├── .venv/
│   └── data/                 # 数据库 + 音频（需备份）
│       ├── text2voice.db
│       └── audio/
└── frontend/
    └── dist/                 # npm run build 的产物
```

### 3.2 Nginx 配置

仓库内的 [`frontend/nginx.conf`](../frontend/nginx.conf) 可直接使用，
或复制到 `/etc/nginx/conf.d/text2voice.conf`：

```nginx
server {
    listen 80;
    server_name text2voice.example.com;

    client_max_body_size 16m;
    charset utf-8;

    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;

    # ---------- 前端静态文件 ----------
    root /opt/text2voice/frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;      # SPA 路由回退
    }

    location ~* \.(?:js|css|woff2?|svg|png|jpg|ico)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    # ---------- 后端 API ----------
    location /api/ {
        proxy_pass         http://127.0.0.1:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # LLM + TTS 链路较慢，务必放大超时
        proxy_connect_timeout 30s;
        proxy_send_timeout    300s;
        proxy_read_timeout    300s;
        proxy_buffering       off;
    }

    # ---------- Swagger ----------
    location /docs {
        proxy_pass http://127.0.0.1:8000/docs;
        proxy_set_header Host $host;
    }
    location /openapi.json {
        proxy_pass http://127.0.0.1:8000/openapi.json;
        proxy_set_header Host $host;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**关键点**

- `proxy_read_timeout` 建议 ≥ 300s：LLM 生成长文本可能耗时数十秒，
  默认 60s 会导致网关超时（前端表现为 504）。
- `X-Forwarded-For` 必须传递：后端按它做限流分桶
  （`app/api/middleware.py::client_identity`）。不传则所有请求被算作同一来源。
- `proxy_buffering off`：音频响应更及时。

### 3.3 systemd 托管后端（Linux）

`/etc/systemd/system/text2voice.service`：

```ini
[Unit]
Description=Text2Voice Agent API
After=network.target

[Service]
Type=simple
User=text2voice
Group=text2voice
WorkingDirectory=/opt/text2voice/backend

Environment="T2V_ENVIRONMENT=production"
Environment="T2V_LOG_FORMAT=json"
Environment="T2V_LOG_LEVEL=INFO"
Environment="T2V_HOST=127.0.0.1"
Environment="T2V_PORT=8000"

ExecStart=/opt/text2voice/backend/.venv/bin/python -m app.main

Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# 基础加固
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd --system --home /opt/text2voice --shell /usr/sbin/nologin text2voice
sudo chown -R text2voice:text2voice /opt/text2voice/backend

sudo systemctl daemon-reload
sudo systemctl enable --now text2voice
sudo systemctl status text2voice
sudo journalctl -u text2voice -f
```

> `T2V_HOST=127.0.0.1` 让后端只监听本机，由 Nginx 对外提供服务，
> 避免 8000 端口直接暴露到公网。

### 3.4 HTTPS

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d text2voice.example.com
```

启用 HTTPS 后，记得把 `T2V_CORS_ORIGINS` 更新为 `https://` 前缀。

### 3.5 性能：worker 数量怎么选

```bash
# 开发
python -m app.main

# 生产（Linux，多核）
pip install gunicorn
gunicorn app.api.app:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 1 \
  --bind 127.0.0.1:8000 \
  --timeout 300 \
  --access-logfile -
```

> **为什么是 1 个 worker？** 异步任务管理器（`app/services/tasks.py`）是
> **进程内**的，每个 worker 有独立的任务表。多 worker 时轮询请求可能落到
> 没有该任务的 worker 上，返回 404 `TASK_NOT_FOUND`。
>
> 单机建议：`--workers 1` 配合 `T2V_TASK_WORKERS=8`（靠协程并发，不是进程）。
> 若确实需要多 worker，请只用同步接口 `POST /generate`，
> 或接入外部队列（见 [5.6 节](#56-横向扩展)）。

---

## 4. Windows 部署

### 4.1 后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

### 4.2 前端

```powershell
cd frontend
npm install
npm run build
```

### 4.3 托管为 Windows 服务（可选）

用 [NSSM](https://nssm.cc/) 把后端注册为服务：

```powershell
nssm install Text2Voice "E:\Text2Voice-Agent\backend\.venv\Scripts\python.exe" "-m app.main"
nssm set Text2Voice AppDirectory "E:\Text2Voice-Agent\backend"
nssm set Text2Voice AppEnvironmentExtra "T2V_ENVIRONMENT=production" "T2V_HOST=127.0.0.1"
nssm start Text2Voice
```

前端静态文件可用 IIS 托管，并在 IIS 中配置 `/api` 反向代理
（需安装 URL Rewrite + ARR 模块），规则等价于第 3.2 节的 Nginx 配置。

> **注意**：Windows 下开发时如果用 Git Bash 运行脚本，
> 请确保 `.sh` 文件是 LF 换行（`scripts/smoke_test.sh` 已内置 CRLF 自愈逻辑）。

### 4.4 接入本地模型（Ollama）

后端与 Ollama 同机运行时，`base_url` 直接用 `http://localhost:11434/v1`：

```bash
curl -X POST http://localhost:8000/api/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"text":"你好","llm":{"provider":"ollama","base_url":"http://localhost:11434/v1","model":"qwen2.5:7b"},"tts":{"provider":"edge","voice":"zh-CN-XiaoxiaoNeural"}}'
```

---

## 5. 生产加固

### 5.1 关闭接口文档

```bash
T2V_DISABLE_DOCS=true
```

关闭后 `/docs`、`/redoc`、`/openapi.json` 均返回 404。

### 5.2 收紧 CORS

```bash
T2V_CORS_ORIGINS=https://text2voice.example.com
```

默认值仅包含本地开发地址。**不要**在生产使用 `*`——虽然凭据走请求体
而非 Cookie，但过宽的 CORS 会让任意站点消耗你的上游配额。

### 5.3 调整限流

```bash
T2V_RATE_LIMIT_REQUESTS=60              # 通用接口：每分钟 60 次
T2V_RATE_LIMIT_GENERATE_REQUESTS=5      # 生成接口：每分钟 5 次（花钱的）
T2V_RATE_LIMIT_WINDOW_SECONDS=60
```

限流按客户端 IP 分桶，依赖 `X-Forwarded-For`。若前置了 CDN，
请确认该头由可信代理设置，否则可被伪造绕过限流。

### 5.4 文本与体积上限

```bash
T2V_MAX_TEXT_LENGTH=2000
T2V_MAX_REQUEST_BYTES=1048576
```

### 5.5 日志

```bash
T2V_LOG_FORMAT=json          # 便于 ELK / Loki 采集
T2V_LOG_LEVEL=INFO           # 生产不建议 DEBUG
T2V_REDACT_EXTRA_KEYS=my-internal-token,another-secret
```

日志已内置强制脱敏（`app/core/logging.py`），无需额外处理密钥。
uvicorn 的原始访问日志被主动关闭，改用中间件的脱敏访问日志。

### 5.6 横向扩展

当前异步任务管理器是**进程内**的。要横向扩展：

1. **同步接口** `/generate` 天然支持多实例（无状态），前面挂负载均衡即可；
2. **异步任务**需要外部队列。`app/services/tasks.py` 的
   `submit` / `get` / `result` 就是替换点，换成 Celery / RQ / Arq：
   - `submit()` → 投递到 broker，返回 `task_id`
   - `get()` → 从 Redis 读状态
3. **限流**改分布式：把 `app/core/rate_limit.py` 的
   `InMemoryRateLimitStore` 换成 Redis 实现（`RateLimitStore` 协议已预留）；
4. **数据库**换成 PostgreSQL：安装 `asyncpg` 并设置
   `T2V_DATABASE_URL=postgresql+asyncpg://user:pass@host/db`，无需改代码；
5. **音频存储**换成对象存储：替换 `app/services/audio.py` 的 `AudioStore`。

### 5.7 文件权限

后端进程需要对 `backend/data/` 有读写权限，且该目录**不应**被 Web 服务器直接暴露：

```bash
chmod 750 /opt/text2voice/backend/data
chown -R text2voice:text2voice /opt/text2voice/backend/data
```

音频只应通过 `GET /api/v1/audio/{id}` 访问（带记录校验），
不要给 `data/audio/` 配置静态目录映射。

---

## 6. 数据备份与迁移

所有状态都在 `backend/data/`：
- `text2voice.db` — SQLite 数据库（历史、任务、设置）
- `audio/*.wav|mp3` — 生成的音频文件

### 6.1 备份

```bash
cd /opt/text2voice/backend

# 推荐：用 SQLite 在线备份（不中断服务，快照一致）
sqlite3 data/text2voice.db ".backup '/backup/text2voice-$(date +%F).db'"

# 音频文件
tar czf /backup/text2voice-audio-$(date +%F).tar.gz -C data audio/
```

> **不要**在服务运行时直接 `cp data/text2voice.db`——可能复制到写入中的页面，
> 得到不一致的快照。用 `.backup` 或先停服。

一键备份脚本：

```bash
cat > /usr/local/bin/t2v-backup.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
BACKUP_DIR=/backup/text2voice
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%F-%H%M)
cd /opt/text2voice/backend
sqlite3 data/text2voice.db ".backup '$BACKUP_DIR/db-$STAMP.sqlite'"
tar czf "$BACKUP_DIR/audio-$STAMP.tar.gz" -C data audio
# 只保留最近 14 份
ls -1t "$BACKUP_DIR"/db-*.sqlite | tail -n +15 | xargs -r rm -f
ls -1t "$BACKUP_DIR"/audio-*.tar.gz | tail -n +15 | xargs -r rm -f
echo "backup done: $STAMP"
EOF
chmod +x /usr/local/bin/t2v-backup.sh

# 每天凌晨 3 点备份
echo "0 3 * * * root /usr/local/bin/t2v-backup.sh >> /var/log/t2v-backup.log 2>&1" \
  | sudo tee /etc/cron.d/t2v-backup
```

### 6.2 恢复

```bash
sudo systemctl stop text2voice
cd /opt/text2voice/backend

cp data/text2voice.db data/text2voice.db.bak      # 保险
cp /backup/text2voice/db-2026-10-12-0300.sqlite data/text2voice.db
tar xzf /backup/text2voice/audio-2026-10-12-0300.tar.gz -C data

sudo chown -R text2voice:text2voice data
sudo systemctl start text2voice
curl -s http://127.0.0.1:8000/health
```

### 6.3 清理旧数据

平台自带保留策略（默认 30 天，每天执行一次）：

```bash
T2V_AUDIO_RETENTION_DAYS=7      # 0 表示不自动清理
```

也可通过界面「历史记录」页手动删除，或调用 API：

```bash
curl -X DELETE http://localhost:8000/api/v1/history \
  -H 'Content-Type: application/json' -d '{"all":true}'
```

---

## 7. 升级与回滚

```bash
cd /opt/text2voice

# 备份
/usr/local/bin/t2v-backup.sh

# 更新后端
cd backend
git pull                      # 或解压新版本
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart text2voice

# 更新前端
cd ../frontend
npm ci
npm run build                 # 直接覆盖 dist/

# 验证
curl -s http://127.0.0.1:8000/health
bash ../scripts/smoke_test.sh http://127.0.0.1:8000
```

数据库表结构由 SQLAlchemy 自动创建；**新增列不会自动迁移到已有库**。
若版本涉及 schema 变更，请先备份再手动 `ALTER TABLE`，
或删除 `text2voice.db` 重新开始（会丢失历史）。

回滚：

```bash
sudo systemctl stop text2voice
cp /backup/text2voice/db-<旧版本>.sqlite backend/data/text2voice.db
tar xzf /backup/text2voice/audio-<旧版本>.tar.gz -C backend/data
git checkout <旧版本 tag>
sudo systemctl start text2voice
```

---

## 8. 监控与日志

### 8.1 健康检查

```bash
curl -s http://localhost:8000/health | python -m json.tool
```

```json
{
  "status": "ok",
  "version": "1.0.0",
  "uptime_seconds": 1234.5,
  "environment": "production",
  "database": "ok",
  "providers": { "llm": 9, "tts": 7 }
}
```

`status` 为 `degraded` 时说明数据库探测失败，`database` 字段会给出原因。

### 8.2 运行指标

```bash
curl -s http://localhost:8000/api/v1/meta/metrics | python -m json.tool
```

包含请求总数、生成成功/失败/降级数、活跃任务数、各服务商调用次数。

### 8.3 关键日志行

```bash
sudo journalctl -u text2voice -f
# 或
sudo journalctl -u text2voice | grep -E 'generation (started|finished)|task (submitted|failed)|rate limit hit'
```

| 日志 | 含义 |
| --- | --- |
| `generation started` | 流水线开始（含服务商、模型、文本长度、request_id） |
| `generation finished` | 完成（含耗时、音频大小、时长、是否降级） |
| `llm degraded` / `tts degraded` | 触发了降级兜底 |
| `provider X short-circuited` | 连续失败触发熔断 |
| `rate limit hit` | 触发限流 |
| `AppError <CODE>` | 业务错误（含错误码与 detail） |

每条日志都带 `request_id`，可与前端错误提示中的 `request_id` 对应排查。

---

## 9. 故障排查

### 后端启动失败

```bash
sudo journalctl -u text2voice -n 100 --no-pager
```

常见原因：
- 端口被占用 → 改 `T2V_PORT`
- `data/` 无写权限 → `chown -R text2voice:text2voice backend/data`
- 依赖未装全 → `pip install -r requirements.txt`

### 前端页面能打开但接口全 502

反代配置问题。检查：

```bash
curl -s http://127.0.0.1:8000/health          # 后端本身是否正常
sudo nginx -t                                  # nginx 配置语法
sudo tail -f /var/log/nginx/error.log
```

确认 `nginx.conf` 里 `proxy_pass` 指向 `http://127.0.0.1:8000`（本机后端），
若后端在别的主机，改成对应 IP。

### 前端页面能打开但接口 404

- `proxy_pass` 结尾的 `/api/` 不能漏，且要带尾斜杠：

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8000/api/;   # ✅ 尾斜杠必须与 location 一致
}
```

### Swagger 打开是空白 / 404

- 确认未设置 `T2V_DISABLE_DOCS=true`
- Nginx 已代理 `/docs` 与 `/openapi.json`（见 3.2 节）

### 页面样式丢失 / 刷新 404

SPA 路由需要回退到 `index.html`：

```nginx
location / {
    try_files $uri $uri/ /index.html;
}
```

缺少 `try_files` 时，直接访问 `/history` 会 404。

### 生成报 `LLM_AUTH_FAILED` / `TTS_AUTH_FAILED`

密钥错误、未开通对应服务、或账号欠费。用「测试连接」按钮单独验证：

```bash
curl -X POST http://localhost:8000/api/v1/providers/validate \
  -H 'Content-Type: application/json' \
  -d '{"kind":"llm","provider":"openai","api_key":"sk-...","model":"gpt-4o-mini"}'
```

返回的 `message` 会给出上游的具体原因。

### 生成报 `LLM_UPSTREAM_ERROR` 且提示无法连接

- 自建/代理场景：检查 `base_url` 是否正确（注意 `/v1` 后缀）
- 本地模型（Ollama）：确认 `ollama serve` 已运行，
  且 `base_url` 为 `http://localhost:11434/v1`

### 返回 `degraded: true`

这是**预期行为**而非故障：上游调用失败时平台返回了降级结果
（模板回复 / 静音音频）以保证可用性。`degraded_reason` 字段会说明具体原因。
若希望失败时直接报错，请求时传：

```json
{ "options": { "fallback_enabled": false } }
```

### 报 `RATE_LIMITED`

触发限流。响应 `detail.retry_after` 给出需等待的秒数。
调大 `T2V_RATE_LIMIT_REQUESTS` / `T2V_RATE_LIMIT_GENERATE_REQUESTS`，
或确认 Nginx 是否正确传递 `X-Forwarded-For`。

### 报 `TTS_TEXT_TOO_LONG`

回复文本超过了该服务商的单次合成上限（各服务商不同，见 `GET /providers`
的 `max_chars`）。可缩短输入、换用上限更高的服务商，或降低
`max_tokens` 让回复更短。

### 报 `TASK_NOT_FOUND`（任务刚提交就查不到）

多 worker 部署导致（见 [3.5 节](#35-性能worker-数量怎么选)）。
改为单 worker `--workers 1`，或改用同步接口 `/generate`。

### 请求很慢 / 网关 504

- Nginx 的 `proxy_read_timeout` 需 ≥ 300s（见 3.2 节）
- 调大 `T2V_LLM_TIMEOUT_SECONDS`（默认 60）与 `T2V_TTS_TIMEOUT_SECONDS`（默认 90）
- 检查 `GET /api/v1/meta/metrics` 中各服务商的错误计数

### 音频无法播放

```bash
ls -la backend/data/audio/
head -c 4 backend/data/audio/<file>
```

WAV 应以 `RIFF` 开头，MP3 应以 `ID3` 或 `0xFFFB` 开头。
若文件为空，检查对应服务商是否返回了空响应（日志中有 `tts synthesis ok ... bytes=0`）。

### 音频播放器无法拖动进度条

`GET /audio/{id}` 支持 Range 请求。若经过自定义反向代理，
请确认未剥离 `Range` 头（Nginx 默认透传）。

### 前端修改后没生效

```bash
cd frontend && npm run build      # 重新构建
```

浏览器强刷（Ctrl+Shift+R）。静态资源带哈希文件名，`index.html` 已配置不缓存。

---

## 附：部署速查

```bash
# 后端
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.main                       # http://localhost:8000/docs

# 前端（开发）
cd frontend && npm install && npm run dev  # http://localhost:5173

# 前端（生产）
npm ci && npm run build                    # 产出 dist/，交给 Nginx
sudo nginx -t && sudo systemctl reload nginx

# 验证
curl -s http://localhost:8000/health
bash scripts/smoke_test.sh http://localhost:8000       # 52 项
python scripts/check_contract.py http://localhost:8000 # 契约一致性
```