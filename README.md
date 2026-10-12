# Text2Voice Agent

**把大模型的回复，变成可以直接播放的语音。**

一个前后端分离的工程化语音 Agent Web 平台：在浏览器里配置 LLM 与 TTS 服务商的 API 密钥，
输入一段文本，即可得到「大模型回复文本 + 该回复的语音文件」，并自动沉淀为可检索、可回放、
可下载的历史记录。

```
用户文本 ──► LLM 适配层 ──► 回复文本 ──► TTS 适配层 ──► 音频文件 ──► 历史记录
             (9 家服务商)                (7 家服务商)
```

| | |
| --- | --- |
| **后端** | Python 3.10+ · FastAPI · SQLAlchemy 2.0 (async) · SQLite · httpx |
| **前端** | Vue 3 · Vite 5 · TypeScript · Element Plus · Pinia · vue-router |
| **适配层** | LLM 9 家 · TTS 7 家，统一接口，新增服务商 = 新增一个文件 |
| **部署** | 手动部署（uvicorn / gunicorn）+ systemd 托管 + Nginx 反向代理，附完整指南 |
| **测试** | 174 个后端单测/集成测 + 52 项端到端冒烟检查，全部通过 |
| **密钥** | 仅单次转发，不落库、不落盘、不进日志（有测试兜底） |

---

## 目录

- [1. 快速开始](#1-快速开始)
- [2. 核心能力](#2-核心能力)
- [3. 项目结构](#3-项目结构)
- [4. 架构设计](#4-架构设计)
- [5. 密钥安全机制](#5-密钥安全机制)
- [6. API 一览](#6-api-一览)
- [7. 错误码](#7-错误码)
- [8. 配置项](#8-配置项)
- [9. 测试](#9-测试)
- [10. 文档索引](#10-文档索引)

---

## 1. 快速开始

### 1.1 最快路径：零密钥体验

不需要任何 API 密钥即可跑通完整链路——平台内置 `mock` 服务商，在本地生成真实可播放的 WAV 音频。

```bash
# 终端 1：后端
cd backend && python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements-dev.txt
python -m app.main                    # -> http://localhost:8000/docs

# 终端 2：前端
cd frontend && npm install && npm run dev   # -> http://localhost:5173
```

打开 <http://localhost:5173>：

1. 进入 **语音生成**；
2. LLM 选 `本地模拟 LLM (无需密钥)`，TTS 选 `本地模拟 TTS (无需密钥)`；
3. 输入任意文本 → 点击 **生成**；
4. 得到回复文本 + 可播放音频 + 下载按钮；历史记录页也能看到这一条。

想接真实服务商，只需在同一个页面把服务商切到 OpenAI / Anthropic / 通义千问 等，填入密钥即可。

### 1.2 本地开发（前后端分离）

**前置条件**：Python ≥ 3.10（3.13 已验证）、Node.js ≥ 20。

#### 后端

```bash
cd backend

# 1) 创建虚拟环境
python -m venv .venv
source .venv/Scripts/activate      # Windows (Git Bash)
# source .venv/bin/activate        # macOS / Linux

# 2) 安装依赖
pip install -r requirements-dev.txt

# 3) 可选：复制配置模板
cp ../.env.example .env

# 4) 启动（开发模式，代码热重载）
python -m app.main
```

- API 根路径：<http://localhost:8000>
- **Swagger 文档：<http://localhost:8000/docs>**（自动生成，可直接在线调试）
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

首次启动会自动创建 `backend/data/text2voice.db` 与 `backend/data/audio/`。

#### 前端

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。Vite 已把 `/api` 代理到 `http://localhost:8000`，无需额外配置。

#### 一键验证

后端起来之后，用冒烟脚本跑一遍完整链路（52 项检查，含密钥泄漏检查）：

```bash
bash scripts/smoke_test.sh http://localhost:8000
```

---

## 2. 核心能力

### 2.1 前端界面

| 页面 | 能力 |
| --- | --- |
| **语音生成** | 只读「当前配置」条 + 跳转设置；三种模式：**文字转语音**（大模型写回复再朗读）、**声音克隆**（上传音频/视频作为音色参考）、**直接配音**（跳过 LLM 直接朗读输入）；生成参数（语速/格式/是否存历史）；分阶段进度与计时；结果区（回复文本、音频播放器、**另存为**、重新生成、仅重新合成语音） |
| **历史记录** | 分页列表、关键字搜索（防抖 400ms）、状态过滤、批量删除、详情抽屉、统一播放器（同时只播一条）、下载、时长与耗时展示、CSV 导出 |
| **系统设置** | **全部服务商配置集中于此**：LLM/TTS 服务商、API Key、模型（含「获取模型」自动拉取）、音色、Base URL、温度/最大长度/系统提示词；**声音克隆管理器**（上传参考片段 → 创建音色 → 选用/删除）；默认参数；请求超时、自动保存历史/凭据；本机凭据查看与清除 |

> **配置不再重复**：服务商、密钥、模型、音色只在「系统设置」配置一次，生成页只读展示。


### 2.2 后端能力

- **LLM 适配层**：`openai`、`anthropic`、`qwen`（通义千问/DashScope）、`deepseek`、`gemini`、`zhipu`（智谱 GLM）、`moonshot`（Kimi）、`ollama`（本地模型）、`custom`（自定义 OpenAI 兼容端点）、`mock`
- **TTS 适配层**：`openai`、`elevenlabs`、`azure`（Azure Speech，SSML 控制语速/风格）、`volcengine`（火山引擎）、`minimax`、`edge`（免费，无需密钥）、`custom`（自定义 OpenAI 兼容端点）、`mock`
- **自定义服务商**：填 Base URL + API Key 即可接入任意 OpenAI 兼容接口；点「获取模型」自动拉取模型列表，其中**支持语音合成的模型会带 `TTS` 标记**（如 `mimo-v2.5-tts`）
- **声音克隆**：上传参考音频/视频（支持 WAV 原生裁剪；MP3/MP4 需 ffmpeg，缺失时给出明确安装提示），调用服务商克隆接口生成音色并本地留存；ElevenLabs / MiniMax 走各自克隆 API，`custom` 走参考音频透传
- **媒体处理**：`POST /media/inspect` 探测时长/采样率/声道，`POST /media/trim` 裁剪片段，`GET /media/{id}` 支持 Range 流式预览（音频/视频在线试听试看）
- **友好报错**：所有新增错误码都带 `detail.suggestion` 中文可执行建议（例如「请在系统设置中把 TTS 模型切换为支持语音合成的模型」）
- **异步任务**：`POST /tasks` 提交 → `GET /tasks/{id}` 轮询进度 → `GET /tasks/{id}/result` 取结果；有界队列 + 多工作协程 + 队列满背压 + 状态持久化（重启后可恢复）
- **同步接口**：`POST /generate` 一次拿到完整结果，适合脚本调用
- **限流**：进程内令牌桶，按客户端 IP 分桶；生成类接口使用更严格的独立配额
- **重试与熔断**：仅对可重试错误（超时 / 429 / 5xx）做指数退避 + 全抖动重试；连续失败触发 30 秒短路
- **降级兜底**：LLM 失败 → 模板化降级回复（仍可合成语音）；TTS 失败 → 静音占位 WAV；两者都会在响应中标记 `degraded=true` 并给出原因。**参数类错误不会被降级掩盖**
- **音频处理**：Range 请求支持（可拖动进度条）、`?download=1` 触发下载、自行解析 WAV/MP3/OGG/FLAC 帧头计算时长
- **可观测性**：请求 ID 贯穿全链路、`/capabilities` 运行时能力探测、`/meta/metrics` 运行指标、`/meta/error-codes` 错误码字典

### 2.3 工程化

- 分层架构：路由层 → 服务层 → 适配层，依赖单向
- 自动生成 Swagger（含统一错误响应示例与错误码扩展字段）
- 完整错误码体系（40+ 码，覆盖 1xxx/2xxx/3xxx/4xxx/5xxx 分段）
- 环境变量集中管理（`T2V_` 前缀 + pydantic-settings 校验）
- 生产部署指南：systemd 托管、Nginx 反向代理与 SPA 回退、备份脚本

---

## 3. 项目结构

```
Text2Voice-Agent/
├── backend/                          # FastAPI 后端
│   ├── app/
│   │   ├── main.py                   # uvicorn 入口（python -m app.main）
│   │   ├── core/                     # 横切关注点
│   │   │   ├── config.py             # 环境变量配置（T2V_* 前缀）
│   │   │   ├── errors.py             # 错误码枚举 + AppError + 默认 HTTP 状态映射
│   │   │   ├── logging.py            # 结构化日志 + 强制脱敏过滤器
│   │   │   ├── security.py           # 凭据校验、指纹、文件名净化、请求 ID
│   │   │   ├── rate_limit.py         # 令牌桶限流（可插拔 store）
│   │   │   └── retry.py              # 重试策略 + 熔断预算
│   │   ├── adapters/                 # ★ 适配层
│   │   │   ├── base.py               # 抽象接口 + 值对象 + HTTP 客户端 + 错误映射
│   │   │   ├── registry.py           # 服务商注册表（唯一枚举处）
│   │   │   ├── llm/                  # 每服务商一个模块
│   │   │   │   ├── openai_compatible.py  # OpenAI 兼容基类（复用 6 家）
│   │   │   │   ├── openai.py  anthropic.py  qwen.py  deepseek.py
│   │   │   │   ├── gemini.py  zhipu.py  moonshot.py  ollama.py
│   │   │   └── tts/
│   │   │       ├── openai.py  elevenlabs.py  azure.py
│   │   │       ├── volcengine.py  minimax.py  edge.py  mock.py
│   │   ├── db/                       # 数据访问层
│   │   │   ├── base.py               # async engine / session / 建表
│   │   │   └── models.py             # GenerationRecord / TaskModel / SettingModel
│   │   ├── schemas/
│   │   │   └── generation.py         # 全部 Pydantic 契约（OpenAPI 来源）
│   │   ├── services/                 # ★ 服务层（业务逻辑）
│   │   │   ├── generation.py         # 生成流水线（LLM→TTS→持久化，含降级）
│   │   │   ├── tasks.py              # 异步任务管理器
│   │   │   ├── history.py            # 历史记录 CRUD / 统计 / 导出
│   │   │   ├── providers.py          # 服务商目录与校验
│   │   │   ├── settings.py           # 用户偏好
│   │   │   ├── audio.py              # 音频存储 + 时长解析
│   │   │   └── metrics.py            # 进程指标
│   │   └── api/                      # ★ 路由层
│   │       ├── app.py                # 应用工厂 + lifespan + 自定义 OpenAPI
│   │       ├── router.py             # /api/v1 聚合
│   │       ├── deps.py               # 依赖注入（会话、限流、分页）
│   │       ├── middleware.py         # 请求 ID / 脱敏访问日志 / 体积限制
│   │       ├── errors.py             # 统一异常处理器
│   │       └── routes/
│   │           ├── providers.py  generate.py  tasks.py
│   │           ├── history.py  audio.py  settings.py  health.py
│   ├── tests/                        # 174 个测试
│   │   ├── conftest.py  test_api.py  test_adapters.py
│   │   ├── test_pipeline.py  test_security.py
│   ├── requirements.txt  requirements-dev.txt  pytest.ini
│
├── frontend/                         # Vue 3 前端
│   ├── src/
│   │   ├── main.ts  App.vue  env.d.ts
│   │   ├── api/                      # http.ts(ApiError) / index.ts / types.ts
│   │   ├── stores/                   # Pinia: config / settings / history
│   │   ├── router/index.ts
│   │   ├── components/
│   │   │   ├── layout/AppSidebar.vue  AppHeader.vue
│   │   │   ├── ProviderConfigCard.vue  VoiceParamsCard.vue
│   │   │   ├── TextInputCard.vue  ResultCard.vue
│   │   │   ├── AudioPlayer.vue  ErrorAlert.vue
│   │   ├── views/                    # GenerateView / HistoryView / SettingsView
│   │   ├── styles/main.css  utils/format.ts
│   ├── vite.config.ts  tsconfig.json  package.json
│   ├── nginx.conf                    # 生产环境参考配置（静态托管 + 反代 /api）
│   ├── .env.development  .env.production
│
├── scripts/
│   ├── smoke_test.sh                 # 52 项端到端冒烟检查
│   └── check_contract.py             # 前后端接口契约一致性校验
├── docs/
│   ├── DEPLOYMENT.md                 # 部署指南（systemd / Nginx / 备份）
│   ├── EXTENDING_TTS.md              # ★ 扩展新 TTS 服务商开发说明
│   ├── API.md                        # 接口手册
│   └── ARCHITECTURE.md               # 架构与数据流
├── .env.example
├── .gitignore
└── README.md
```

---

## 4. 架构设计

### 4.1 分层与依赖方向

```
        ┌──────────────────────────────────────────────┐
        │  路由层  app/api/routes/*                    │  只做 HTTP 编解码
        │  参数校验、限流、错误映射                      │  不含业务逻辑
        └───────────────────┬──────────────────────────┘
                            │ 依赖注入
        ┌───────────────────▼──────────────────────────┐
        │  服务层  app/services/*                      │  业务编排
        │  流水线、任务、历史、设置、音频                │  只依赖抽象接口
        └───────────────────┬──────────────────────────┘
                            │ registry 查表
        ┌───────────────────▼──────────────────────────┐
        │  适配层  app/adapters/*                      │  协议转换
        │  统一接口 → 各家 SDK/HTTP 协议                │  错误归一化
        └───────────────────┬──────────────────────────┘
                            │
                   外部服务商 API
```

**关键约束**：路由层与服务层**从不 import 具体适配器类**，只通过
`registry.llm(id)` / `registry.tts(id)` 按 id 取用。因此接入新服务商不需要改动
任何路由、Schema 或服务代码。

### 4.2 生成流水线

`app/services/generation.py` 的三个阶段，同步与异步模式复用同一份实现：

```
阶段 1  LLM       registry.llm(provider) → call_with_retry → 回复文本
                  └ 失败且可降级 → 模板回复（标记 degraded）
阶段 2  TTS       registry.tts(provider) → call_with_retry → 音频字节
                  └ 失败且可降级 → 静音 WAV（标记 degraded）
阶段 3  persist   写音频文件 → 插入历史记录 → 返回 DTO
```

进度通过 `on_progress(stage, percent, message)` 回调上报，
同步接口忽略它，异步任务用它更新任务状态。

### 4.3 异步任务模型

```
POST /tasks ──► 有界 asyncio.Queue ──► N 个 worker 协程 ──► 生成流水线
     │                (满则 503 TASK_QUEUE_FULL)
     │
     └─► tasks 表（持久化，重启可恢复）
         request_payload 已剔除密钥
```

`GET /tasks/{id}` 走内存快照（轮询不打库），阶段切换时才落库。

---

## 5. 密钥安全机制

这是本项目的核心设计约束，**每一项都有测试兜底**（见 `backend/tests/test_security.py`）。

| 机制 | 实现 | 验证 |
| --- | --- | --- |
| 类型层面不可泄漏 | `api_key` 使用 pydantic `SecretStr`，`repr()`/`str()`/OpenAPI 均不显示 | `test_repr_does_not_leak_secret` |
| 只取一次 | 仅在构造适配器请求时显式 `get_secret_value()` | `test_secret_is_retrievable_only_explicitly` |
| 不落库 | `GenerateRequest.safe_dump()` 直接剔除 `api_key`；数据库表**没有密钥列** | `test_safe_dump_contains_no_credential`、`test_no_credential_is_written_to_the_row` |
| 不可逆审计 | 仅存 SHA-256 截断指纹 `key_fingerprint`（`sha256:xxxxxxxxxxxxxxxx`） | `test_fingerprint_is_stable_and_irreversible` |
| 任务快照同样干净 | 任务表的 `request_payload` 使用 `safe_dump()` | `test_task_payload_snapshot_has_no_key` |
| 日志强制脱敏 | `RedactionFilter` 作用于 message、args、`extra` 字段、异常堆栈 | `test_message_is_redacted`、`test_traceback_is_redacted` |
| 覆盖主流密钥形态 | `sk-*`、`sk-ant-*`、`AIza*`、`hf_*`、32 位十六进制、`Bearer/Basic`、`?api_key=`、UUID、长 base64 | `test_openai_key_is_masked` 等 8 项 |
| 请求 ID 安全 | `X-Request-ID` 经白名单正则校验后才接受，且关联 ID 本身不可形似密钥 | `sanitize_request_id` |
| 设置接口拒收密钥 | `PUT /settings` 的 Schema 为 `extra="forbid"`，传 `api_key` 直接 422 | `test_submitting_a_key_is_rejected` |
| 前端不落盘 | 密钥默认仅存 Pinia 内存；仅当用户显式开启「自动保存配置」才存 localStorage，并有醒目风险提示 | 前端 `stores/config.ts` |
| 端到端复核 | 冒烟脚本注入 canary 密钥，检查响应、数据库文件、服务端日志三处均无泄漏 | `smoke_test.sh` 第 10 节 |

日志脱敏示例：

```
原始：  upstream call provider=openai endpoint=... payload={"api_key": "sk-proj-AbCd...", "model": "gpt-4o-mini"}
日志：  upstream call provider=openai endpoint=... payload={"api_key": "***REDACTED***", "model": "gpt-4o-mini"}

原始：  Authorization: Bearer abcdef1234567890
日志：  Authorization: Bearer ***REDACTED***
```

---

## 6. API 一览

完整交互式文档：**<http://localhost:8000/docs>**。以下为速查表。

### 服务商

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/providers` | 全部服务商能力（模型、音色、语速范围、格式、额外字段） |
| `POST` | `/api/v1/providers/validate` | 校验密钥是否可用（失败也返回 200 + `valid:false`） |

### 生成

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/generate` | **同步**生成，直接返回结果 |
| `POST` | `/api/v1/tasks` | **异步**提交，返回 202 + `task_id` |
| `GET` | `/api/v1/tasks/{id}` | 查询状态与进度（`stage`/`progress`/`message`） |
| `GET` | `/api/v1/tasks/{id}/result` | 获取结果（未完成返回 409 `TASK_NOT_FINISHED`） |
| `DELETE` | `/api/v1/tasks/{id}` | 取消任务 |

### 数据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/history` | 分页 + 搜索 + 状态过滤 |
| `GET` | `/api/v1/history/{id}` | 单条详情 |
| `DELETE` | `/api/v1/history/{id}` | 删除单条（204） |
| `DELETE` | `/api/v1/history` | 批量删除（`{"ids":[...]}` 或 `{"all":true}`） |
| `GET` | `/api/v1/history/stats` | 统计概览 |
| `GET` | `/api/v1/history/export` | 导出 CSV |
| `GET` | `/api/v1/audio/{id}` | 音频流（支持 Range；`?download=1` 下载） |
| `GET` / `PUT` | `/api/v1/settings` | 用户偏好（**不接受密钥字段**） |

### 系统与元信息

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查（含数据库状态、服务商数量） |
| `GET` | `/api/v1/meta/error-codes` | 错误码字典 |
| `GET` | `/api/v1/meta/metrics` | 运行指标 |
| `GET` | `/api/v1/meta/config` | 非敏感运行时配置 |

### 请求示例

```bash
# 异步生成（推荐给 Web 前端）
curl -X POST http://localhost:8000/api/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "请用三句话介绍一下杭州西湖。",
    "llm": {
      "provider": "openai",
      "api_key": "sk-...",
      "model": "gpt-4o-mini",
      "temperature": 0.7,
      "max_tokens": 512,
      "system_prompt": "你是一个乐于助人的中文助手，回答口语化，适合朗读。"
    },
    "tts": {
      "provider": "openai",
      "api_key": "sk-...",
      "voice": "nova",
      "speed": 1.0,
      "format": "mp3"
    },
    "options": { "auto_save": true, "fallback_enabled": true }
  }'
# -> 202 {"task_id":"...","status":"pending","stage":"queued","progress":0}

# 轮询
curl http://localhost:8000/api/v1/tasks/<task_id>
# -> {"status":"running","stage":"tts","progress":60,"message":"正在调用 OpenAI TTS 合成语音…"}

# 取结果
curl http://localhost:8000/api/v1/tasks/<task_id>/result
# -> {"record_id":1,"reply_text":"...","audio_url":"/api/v1/audio/1","duration_ms":4210, ...}
```

> **仅重新合成语音**：在 `options` 中传 `reuse_reply_text`，后端会完全跳过 LLM 调用，
> 只对给定文本重新合成音频（不消耗 token，回复文本逐字保留）。

---

## 7. 错误码

所有非 2xx 响应统一为：

```json
{
  "code": "TTS_AUTH_FAILED",
  "message": "openai 鉴权失败，请检查 API 密钥是否正确、是否已开通对应服务。",
  "detail": { "provider": "openai", "kind": "tts", "upstream_status": 401 },
  "request_id": "8f14e45fceea167a5a36dedd4bea2543"
}
```

分段规则：

| 区段 | 含义 | 示例 |
| --- | --- | --- |
| `1xxx` | 通用 / 参数校验 | `VALIDATION_ERROR`、`MISSING_CREDENTIAL`、`TEXT_TOO_LONG` |
| `2xxx` | LLM 适配层 | `LLM_AUTH_FAILED`、`LLM_TIMEOUT`、`LLM_RATE_LIMITED` |
| `3xxx` | TTS 适配层 | `TTS_AUTH_FAILED`、`TTS_INVALID_VOICE`、`TTS_TEXT_TOO_LONG` |
| `4xxx` | 异步任务 | `TASK_NOT_FOUND`、`TASK_NOT_FINISHED`、`TASK_QUEUE_FULL` |
| `5xxx` | 平台 | `RATE_LIMITED`、`INTERNAL_ERROR`、`AUDIO_NOT_FOUND` |

机器可读的完整清单（含 HTTP 状态码与中文说明）：`GET /api/v1/meta/error-codes`。

---

## 8. 配置项

全部通过 `T2V_` 前缀的环境变量或 `backend/.env` 配置，详见 [`.env.example`](.env.example)。
常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `T2V_PORT` | `8000` | 监听端口 |
| `T2V_ENVIRONMENT` | `development` | `development` / `staging` / `production` |
| `T2V_DATABASE_URL` | `sqlite+aiosqlite:///./data/text2voice.db` | 换 PostgreSQL 只需装 `asyncpg` |
| `T2V_AUDIO_DIR` | `./data/audio` | 音频目录 |
| `T2V_MAX_TEXT_LENGTH` | `5000` | 输入文本上限 |
| `T2V_RATE_LIMIT_REQUESTS` | `30` / 60s | 通用接口配额 |
| `T2V_RATE_LIMIT_GENERATE_REQUESTS` | `10` / 60s | 生成接口配额（更严格） |
| `T2V_UPSTREAM_MAX_RETRIES` | `2` | 可重试错误的额外重试次数 |
| `T2V_TASK_WORKERS` | `4` | 异步任务并发数 |
| `T2V_FALLBACK_ENABLED` | `true` | 是否启用降级兜底 |
| `T2V_AUDIO_RETENTION_DAYS` | `30` | 历史与音频保留天数（0 关闭清理） |
| `T2V_DISABLE_DOCS` | `false` | 生产环境可关闭 `/docs` |

**注意**：`.env` 中不应出现任何 API 密钥——密钥由用户在页面输入，按请求转发。

---

## 9. 测试

```bash
cd backend

# 全部测试（174 个，无需网络与密钥）
pytest

# 分类运行
pytest tests/test_security.py      # 密钥安全与日志脱敏（最关键的 40 项）
pytest tests/test_adapters.py      # 适配层：注册表、能力、错误映射、各家报文形状
pytest tests/test_api.py           # API 契约：每个路由与错误码
pytest tests/test_pipeline.py      # 流水线：重试、降级、持久化、并发

# 覆盖率
pytest --cov=app --cov-report=term-missing
```

端到端（需先启动后端）：

```bash
bash scripts/smoke_test.sh http://localhost:8000
```

前端：

```bash
cd frontend
npm run typecheck     # vue-tsc 类型检查
npm run build         # 类型检查 + 生产构建
```

### 测试覆盖要点

- **安全**：密钥不进响应/数据库/日志；脱敏规则覆盖 8 类密钥形态；traceback 也脱敏
- **适配层**：用 monkeypatch 拦截 HTTP 传输，验证各家**报文形状**（Anthropic 的 `x-api-key` + `system` 字段、Gemini 的路径拼接、火山引擎的 `Bearer;` 与 base64 解码、Azure 的 SSML）而无需真实调用
- **降级**：LLM/TTS 分别失败时仍返回可用结果并正确标记 `degraded`
- **契约**：每个错误码的 HTTP 状态、统一错误信封、OpenAPI 完整性
- **并发**：4 路并发生成互不干扰

---

## 10. 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | 手动部署 / systemd 托管 / Nginx 反向代理 / 备份 / 排错 |
| [docs/EXTENDING_TTS.md](docs/EXTENDING_TTS.md) | **扩展新 TTS 服务商的完整开发说明**（含可复制的模板） |
| [docs/API.md](docs/API.md) | 接口手册与调用示例 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构决策、数据模型、扩展点 |
| `/docs` | 运行时自动生成的 Swagger UI |

---

## License

MIT