# 架构说明

本文档记录关键设计决策、数据模型与扩展点。
交互式接口文档见运行时的 `/docs`；接口速查见 [API.md](API.md)。

---

## 1. 分层架构

```
┌───────────────────────────────────────────────────────────────┐
│ 路由层  app/api/routes/*.py                                    │
│   · HTTP 编解码、参数校验（Pydantic）、依赖注入、限流            │
│   · 不含业务逻辑；不 import 任何具体适配器                       │
└──────────────────────────┬────────────────────────────────────┘
                           │ SessionDep / RateLimitDep
┌──────────────────────────▼────────────────────────────────────┐
│ 服务层  app/services/*.py                                      │
│   · 业务编排：生成流水线、任务管理、历史、设置、音频、指标        │
│   · 只依赖适配层抽象接口（通过 registry 查表）                   │
└──────────────────────────┬────────────────────────────────────┘
                           │ registry.llm(id) / registry.tts(id)
┌──────────────────────────▼────────────────────────────────────┐
│ 适配层  app/adapters/*.py                                      │
│   · 统一接口 → 各家协议转换；错误归一化为 AppError               │
│   · base.py 提供共享 HTTP 客户端、重试钩子、错误映射             │
└──────────────────────────┬────────────────────────────────────┘
                           │
┌──────────────────────────▼────────────────────────────────────┐
│ 数据层  app/db/*.py   +   横切  app/core/*.py                   │
│   SQLAlchemy 2.0 async    配置 / 错误码 / 日志脱敏 / 限流 / 重试  │
└───────────────────────────────────────────────────────────────┘
```

### 关键决策：注册表模式

**问题**：如果服务层直接 `if provider == "openai": ... elif provider == "elevenlabs": ...`，
每加一个服务商就要改业务代码，且容易漏改。

**方案**：`app/adapters/registry.py` 是**唯一**枚举服务商的地方。所有其他代码
只通过字符串 id 查表：

```python
adapter = registry.tts(request.tts.provider)
result = await adapter.synthesize(tts_request)
```

**收益**：
- 新增服务商 = 新增 1 个文件 + 1 行注册，路由/Schema/服务/前端全部零改动；
- 前端表单由 `GET /providers` 的元数据驱动，服务商自描述能力（音色、语速范围、格式）；
- 适配器可独立单测，不需要启动应用。

---

## 2. 生成流水线

`app/services/generation.py::GenerationService.generate()`

```
                    ┌─────────────────────────────────────────┐
  GenerateRequest ─►│ 阶段 1  LLM                             │
                    │  registry.llm(provider)                 │
                    │  call_with_retry(complete)              │
                    │  ─ 成功 ──► reply_text + usage + latency │
                    │  ─ 失败 ──► 可降级? 模板回复 : 抛出       │
                    └────────────────┬────────────────────────┘
                                     │ reply_text
                    ┌────────────────▼────────────────────────┐
                    │ 阶段 2  TTS                             │
                    │  registry.tts(provider)                 │
                    │  call_with_retry(synthesize)            │
                    │  ─ 成功 ──► audio bytes + latency        │
                    │  ─ 失败 ──► 可降级? 静音 WAV : 抛出       │
                    └────────────────┬────────────────────────┘
                                     │ audio
                    ┌────────────────▼────────────────────────┐
                    │ 阶段 3  persist                         │
                    │  解析时长（自研帧头解析器）              │
                    │  写音频文件 → 插入历史记录 → 返回 DTO     │
                    └─────────────────────────────────────────┘
```

### 降级策略

| 阶段 | 失败原因 | 行为 |
| --- | --- | --- |
| LLM | 超时 / 429 / 5xx / 网络 | 用 `fallback_reply_template` 生成降级回复，继续 TTS，`degraded=true` |
| LLM | 密钥缺失 / 服务商不存在 / 参数错 | **直接报错**（参数问题不该被掩盖） |
| TTS | 超时 / 429 / 5xx / 网络 | 生成静音 WAV 占位，`degraded=true` |
| TTS | 格式不支持 / 音色无效 / 文本超长 | **直接报错** |

判定逻辑集中在 `NON_DEGRADABLE_CODES` 集合（`generation.py`），
并有测试覆盖（`test_missing_credential_is_400` 等）。

### 进度上报

流水线通过回调上报进度，同步与异步模式共用同一实现：

```python
on_progress(stage: str, percent: int, message: str) -> None
```

- 同步接口：忽略回调（不产生额外开销）
- 异步任务：更新内存状态，仅在**阶段切换**时落库（避免轮询打库）

---

## 3. 异步任务模型

`app/services/tasks.py::TaskManager`

```
POST /tasks
   │
   ├─► 有界 asyncio.Queue(maxsize=T2V_TASK_QUEUE_SIZE)
   │     队列满 → 503 TASK_QUEUE_FULL（背压信号）
   │
   ├─► tasks 表插入 pending 行（先持久化，崩溃也不丢）
   │
   └─► 请求对象留在内存 _pending_payloads（含密钥，绝不落库）

        N 个 worker 协程（T2V_TASK_WORKERS）
            │
            ├─ 取任务 → 标记 running → 调用生成流水线
            ├─ 进度回调更新内存状态
            ├─ 完成后写 result（JSON）到 tasks 表
            └─ 立即从 _pending_payloads 移除（密钥随之释放）
```

**双写设计**：内存快照服务轮询热路径（不打库），数据库保证可审计与重启恢复。

**已知限制**：任务表是进程内的。多 worker 部署需换成外部队列
（见 [DEPLOYMENT.md 第 5.6 节](DEPLOYMENT.md#56-横向扩展)）。

---

## 4. 数据模型

### generation_records

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK | |
| `input_text` | TEXT | 用户输入 |
| `reply_text` | TEXT | 大模型回复 |
| `llm_provider` / `llm_model` | VARCHAR | LLM 来源 |
| `tts_provider` / `tts_voice` / `tts_model` | VARCHAR | TTS 来源 |
| `key_fingerprint` | VARCHAR(64) | **SHA-256 截断指纹**，不可逆，仅供审计 |
| `audio_format` / `audio_bytes` / `audio_path` | | 音频元数据（存相对路径） |
| `duration_ms` | INTEGER | 音频时长（帧头解析） |
| `llm_latency_ms` / `tts_latency_ms` / `total_latency_ms` | INTEGER | 耗时 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | INTEGER | token 用量 |
| `status` / `degraded` / `degraded_reason` | | 结果状态 |
| `error_code` / `error_message` | | 失败信息 |
| `request_id` | VARCHAR(64) | 链路追踪 |
| `created_at` | DATETIME | 索引 |

> **表中没有任何密钥列**。这是设计约束，不是疏漏。

### tasks

`task_id`(PK) / `status` / `stage` / `progress` / `message` / `record_id` /
`result`(JSON) / `error`(JSON) / `request_payload`(JSON，已剔除密钥) /
`request_id` / `created_at` / `started_at` / `finished_at` / `elapsed_ms`

### settings

单行 JSON 文档（`id=1`），读取即整取，新增字段自动回填默认值。
**不接受任何凭据字段**（Schema 为 `extra="forbid"`）。

---

## 5. 错误处理体系

### 单一信封

所有非 2xx 响应（包括框架抛出的 404/405/422）：

```json
{ "code": "...", "message": "...", "detail": {...}, "request_id": "..." }
```

由 `app/api/errors.py` 注册的四个处理器统一保证：
`AppError` / `RequestValidationError` / `HTTPException` / `Exception`。

### 错误码分段

| 区段 | 归属 |
| --- | --- |
| `1xxx` | 通用、参数校验、凭据 |
| `2xxx` | LLM 适配层 |
| `3xxx` | TTS 适配层 |
| `4xxx` | 异步任务 |
| `5xxx` | 平台（限流、存储、内部） |

`DEFAULT_STATUS` 映射表给出每个码的默认 HTTP 状态，路由可覆盖
（例如 `TASK_NOT_FINISHED` → 409）。

### 重试与熔断

`app/core/retry.py`

- 仅重试 `retryable=True` 的错误（超时 / 429 / 5xx）
- 指数退避 + 全抖动：`min(cap, base * 2^(n-1))` 内的随机值
- `RetryBudget` 熔断：同一服务商连续失败 5 次后短路 30 秒，
  避免拖慢每一个请求

---

## 6. 安全设计

见 [README 第 5 节](../README.md#5-密钥安全机制) 的完整表格。核心机制：

1. **类型层**：`SecretStr` 让密钥在 `repr()`、日志、OpenAPI 中天然不可见
2. **序列化层**：`safe_dump()` 显式剔除凭据，用于所有持久化路径
3. **存储层**：表结构无密钥列；只存 SHA-256 截断指纹
4. **日志层**：`RedactionFilter` 作用于 message / args / extra / traceback，
   覆盖 `sk-*`、`AIza*`、`hf_*`、32 位 hex、`Bearer`、`?api_key=`、UUID、长 base64
5. **接口层**：`PUT /settings` 用 `extra="forbid"` 拒收密钥字段
6. **前端层**：密钥默认仅在 Pinia 内存；localStorage 持久化需用户显式开启并确认风险
7. **验证**：`tests/test_security.py`（40 项）+ 冒烟脚本 canary 检查

---

## 7. 扩展点

| 要扩展什么 | 改哪里 | 文档 |
| --- | --- | --- |
| **新增 TTS 服务商** | `app/adapters/tts/<vendor>.py` + registry 注册 | [EXTENDING_TTS.md](EXTENDING_TTS.md) |
| **新增 LLM 服务商** | `app/adapters/llm/<vendor>.py` + registry 注册 | [EXTENDING_TTS.md 末尾](EXTENDING_TTS.md#参考实现) |
| 换数据库 | 设置 `T2V_DATABASE_URL`，装驱动 | [DEPLOYMENT.md](DEPLOYMENT.md#56-横向扩展) |
| 换外部队列 | 实现 `TaskManager` 的 submit/get/result | [DEPLOYMENT.md](DEPLOYMENT.md#56-横向扩展) |
| 换分布式限流 | 实现 `RateLimitStore` 协议（已预留） | `app/core/rate_limit.py` |
| 换音频存储 | 替换 `AudioStore` | `app/services/audio.py` |
| 加指标后端 | 替换 `MetricsCollector` 为 prometheus_client | `app/services/metrics.py` |
| 加用户体系 | 在 `app/api/deps.py` 加鉴权依赖 | — |

---

## 8. 为什么这样设计

| 决策 | 原因 |
| --- | --- |
| **同步 + 异步双接口** | 脚本调用要简单（`/generate`），Web UI 要进度反馈（`/tasks`）。两者复用同一条流水线，不重复实现 |
| **音频时长自研解析** | 多数服务商不上报时长；自己解析 WAV/MP3/OGG/FLAC 帧头可保证历史列表数字一致且正确 |
| **SQLite 默认** | 单机零配置、单文件易备份；`DATABASE_URL` 可换 PG，无需改代码 |
| **进程内限流** | 单进程部署不需要 Redis；`RateLimitStore` 协议预留了替换点 |
| **降级而非直接失败** | 用户输入了一段文字，即使上游抖动也应尽量拿到可听的结果；同时用 `degraded` 标记保证透明 |
| **参数错误不降级** | 密钥错误被降级成"成功"会掩盖真实问题，让用户以为服务正常 |
| **统一错误信封** | 前端只需一个 `ApiError` 解析逻辑；`request_id` 让用户报错可被精确定位 |
| **前端能力由后端驱动** | 服务商能力（音色/格式/语速范围）变化时前端零改动 |

---

## 9. 代码规模

| 模块 | 文件 | 说明 |
| --- | --- | --- |
| `app/core` | 6 | 配置、错误、日志、安全、限流、重试 |
| `app/adapters` | 18 | 基类 + 注册表 + 9 LLM + 7 TTS |
| `app/db` | 2 | engine/session + ORM 模型 |
| `app/schemas` | 2 | 全部 Pydantic 契约 |
| `app/services` | 7 | 流水线、任务、历史、设置、服务商、音频、指标 |
| `app/api` | 11 | 应用工厂、路由、依赖、中间件、错误处理 |
| `tests` | 5 | 174 个测试 |
| `frontend/src` | 26 | 3 视图 + 8 组件 + 3 store + API 层 |