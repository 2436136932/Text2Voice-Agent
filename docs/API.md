# API 手册

- **交互式文档（推荐）**：<http://localhost:8000/docs> —— 可直接在线调试
- **ReDoc**：<http://localhost:8000/redoc>
- **OpenAPI JSON**：<http://localhost:8000/openapi.json>

Base URL：`http://localhost:8000/api/v1`

---

## 目录

- [通用约定](#通用约定)
- [服务商](#服务商)
- [生成（同步）](#生成同步)
- [生成（异步任务）](#生成异步任务)
- [历史记录](#历史记录)
- [音频](#音频)
- [系统设置](#系统设置)
- [媒体与声音克隆](#媒体与声音克隆)
- [系统与元信息](#系统与元信息)
- [完整调用示例](#完整调用示例)

---

## 通用约定

### 请求

- `Content-Type: application/json`
- 可选请求头 `X-Request-ID`（自定义链路 ID，需匹配 `^[A-Za-z0-9_\-.:]{1,128}$`）

### 响应头

| 头 | 说明 |
| --- | --- |
| `X-Request-ID` | 本次请求的链路 ID（未提供则自动生成） |
| `X-Response-Time` | 服务端处理耗时 |

### 错误格式

所有非 2xx 响应统一为：

```json
{
  "code": "TTS_AUTH_FAILED",
  "message": "openai 鉴权失败，请检查 API 密钥是否正确、是否已开通对应服务。",
  "detail": { "provider": "openai", "kind": "tts", "upstream_status": 401 },
  "request_id": "8f14e45fceea167a5a36dedd4bea2543"
}
```

完整错误码清单：`GET /api/v1/meta/error-codes`。

### 关于密钥

所有 `api_key` 字段：
- 仅在本次请求内转发给上游服务商；
- **不写入数据库、不写入磁盘、不进入日志**；
- 数据库中只保存不可逆的 SHA-256 截断指纹。

---

## 服务商

### GET /providers

列出全部支持的 LLM 与 TTS 服务商及其能力。前端表单由此渲染。

```bash
curl http://localhost:8000/api/v1/providers
```

```json
{
  "llm": [
    {
      "id": "openai",
      "name": "OpenAI",
      "kind": "llm",
      "requires_api_key": true,
      "requires_base_url": false,
      "supports_base_url": true,
      "api_key_hint": "sk-...",
      "docs_url": "https://platform.openai.com/docs/api-reference/chat",
      "notes": "支持 GPT-4o / GPT-4o-mini / GPT-4.1 等…",
      "default_model": "gpt-4o-mini",
      "models": [{ "id": "gpt-4o-mini", "name": "gpt-4o-mini (快速、便宜)" }]
    }
  ],
  "tts": [
    {
      "id": "openai",
      "name": "OpenAI TTS",
      "kind": "tts",
      "requires_api_key": true,
      "default_voice": "alloy",
      "voices": [{ "id": "alloy", "name": "Alloy", "gender": "neutral", "language": "multi" }],
      "supports_speed": true,
      "speed_min": 0.25,
      "speed_max": 4.0,
      "formats": ["mp3", "opus", "aac", "flac", "wav", "pcm"],
      "max_chars": 4096,
      "extra_fields": []
    }
  ]
}
```

**已内置服务商**

| 类型 | id | 名称 | 是否需要密钥 |
| --- | --- | --- | --- |
| LLM | `openai` | OpenAI | 是 |
| LLM | `anthropic` | Anthropic Claude | 是 |
| LLM | `qwen` | 通义千问 (DashScope) | 是 |
| LLM | `deepseek` | DeepSeek | 是 |
| LLM | `gemini` | Google Gemini | 是 |
| LLM | `zhipu` | 智谱 GLM | 是 |
| LLM | `moonshot` | Moonshot (Kimi) | 是 |
| LLM | `ollama` | Ollama / 本地模型 | **否** |
| LLM | `mock` | 本地模拟 LLM | **否** |
| TTS | `openai` | OpenAI TTS | 是 |
| TTS | `elevenlabs` | ElevenLabs | 是 |
| TTS | `azure` | Azure Speech | 是（另需 region） |
| TTS | `volcengine` | 火山引擎 TTS | 是（另需 app_id） |
| TTS | `minimax` | MiniMax 语音 | 是（另需 group_id） |
| TTS | `edge` | Edge 免费 TTS | **否** |
| TTS | `mock` | 本地模拟 TTS | **否** |

> `extra_fields` 声明了服务商特有的额外凭据字段（如 Azure 的 `region`），
> 通过请求的 `extra` 字典传入。

### POST /providers/validate

用提交的密钥做一次最小开销探测。

```bash
curl -X POST http://localhost:8000/api/v1/providers/validate \
  -H 'Content-Type: application/json' \
  -d '{
    "kind": "llm",
    "provider": "openai",
    "api_key": "sk-...",
    "model": "gpt-4o-mini"
  }'
```

```json
{
  "valid": true,
  "message": "连接成功，模型 gpt-4o-mini 可用。",
  "latency_ms": 412,
  "provider": "openai",
  "kind": "llm",
  "detail": { "model": "gpt-4o-mini" }
}
```

> **校验失败仍返回 HTTP 200**，`valid: false`，`message` 说明原因。
> 这样前端可以统一展示"测试连接"的结果，而不用区分错误处理路径。

TTS 校验会真的合成一小段音频：

```bash
curl -X POST http://localhost:8000/api/v1/providers/validate \
  -H 'Content-Type: application/json' \
  -d '{"kind":"tts","provider":"azure","api_key":"...","voice":"zh-CN-XiaoxiaoNeural","extra":{"region":"eastasia"}}'
```

---

## 生成（同步）

### POST /generate

一次调用完成「LLM → TTS」并返回结果。适合脚本与后端服务调用。

**请求体**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string | ✓ | 输入文本，1–5000 字符 |
| `llm.provider` | string | ✓ | 服务商 id |
| `llm.api_key` | string | 视服务商 | API 密钥 |
| `llm.model` | string | | 模型名，缺省用服务商默认值 |
| `llm.base_url` | string | | 自定义端点（代理/Azure/自建） |
| `llm.temperature` | number | | 0–2，默认 0.7 |
| `llm.max_tokens` | integer | | 1–32768，默认 1024 |
| `llm.system_prompt` | string | | 系统提示词 |
| `llm.extra` | object | | 服务商特有参数 |
| `tts.provider` | string | ✓ | 服务商 id |
| `tts.api_key` | string | 视服务商 | API 密钥 |
| `tts.voice` | string | | 音色 id，缺省用服务商默认值 |
| `tts.speed` | number | | 0.1–4.0，默认 1.0（会按服务商范围夹紧） |
| `tts.format` | string | | 音频格式，默认 `mp3` |
| `tts.language` | string | | 语言，默认 `zh-CN` |
| `tts.extra` | object | | 服务商特有参数（region / app_id / group_id 等） |
| `options.auto_save` | boolean | | 是否写入历史，默认 `true` |
| `options.include_audio_base64` | boolean | | 响应中是否内联音频 base64，默认 `false` |
| `options.fallback_enabled` | boolean | | 是否启用降级兜底，默认 `true` |
| `options.reuse_reply_text` | string | | **仅重新合成语音**：跳过 LLM，直接合成此文本 |

**响应 200**

```json
{
  "record_id": 1,
  "reply_text": "杭州西湖位于浙江省杭州市西部，是中国著名的淡水湖……",
  "input_text": "请用三句话介绍一下杭州西湖。",
  "audio_url": "/api/v1/audio/1",
  "audio_format": "mp3",
  "audio_bytes": 48210,
  "audio_base64": null,
  "duration_ms": 4210,
  "degraded": false,
  "degraded_reason": null,
  "llm": {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "latency_ms": 812,
    "usage": { "prompt_tokens": 20, "completion_tokens": 90, "total_tokens": 110 },
    "finish_reason": "stop"
  },
  "tts": { "provider": "openai", "voice": "nova", "latency_ms": 1203, "format": "mp3", "bytes": 48210 },
  "total_latency_ms": 2100,
  "created_at": "2026-10-12T15:30:46+08:00"
}
```

**降级说明**：当上游调用失败且 `fallback_enabled=true` 时，接口仍返回 200，
但 `degraded: true` 且 `degraded_reason` 说明原因（LLM 用模板回复、TTS 用静音占位）。
若希望失败时直接报错，传 `"fallback_enabled": false`。

**参数类错误不会被降级掩盖**：密钥缺失、服务商不存在、格式不支持等会如实返回 4xx。

---

## 生成（异步任务）

适合 Web 界面，可展示分阶段进度。

### POST /tasks

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H 'Content-Type: application/json' \
  -d @request.json
```

**响应 202**

```json
{ "task_id": "3f2b1a4c9d8e4f7ab6c51e2d3f4a5b6c", "status": "pending", "stage": "queued", "progress": 0, "message": "任务已提交" }
```

队列满时返回 503 `TASK_QUEUE_FULL`。

### GET /tasks/{task_id}

```json
{
  "task_id": "3f2b1a4c9d8e4f7ab6c51e2d3f4a5b6c",
  "status": "running",
  "stage": "tts",
  "progress": 60,
  "message": "正在调用 OpenAI TTS 合成语音…",
  "record_id": null,
  "created_at": "2026-10-12T15:30:46+08:00",
  "started_at": "2026-10-12T15:30:46+08:00",
  "finished_at": null,
  "elapsed_ms": 1240,
  "error": null
}
```

| 字段 | 取值 |
| --- | --- |
| `status` | `pending` / `running` / `succeeded` / `failed` / `canceled` |
| `stage` | `queued` → `llm` → `tts` → `persist` → `done` |
| `progress` | 0–100，单调递增 |
| `message` | 可直接展示的中文进度提示 |

失败时 `error` 为完整错误信封：

```json
{
  "status": "failed",
  "stage": "done",
  "error": {
    "code": "LLM_AUTH_FAILED",
    "message": "openai 鉴权失败，请检查 API 密钥是否正确…",
    "detail": { "provider": "openai", "kind": "llm", "upstream_status": 401 },
    "request_id": "8f14e45fceea167a5a36dedd4bea2543"
  }
}
```

### GET /tasks/{task_id}/result

任务完成后返回与 `POST /generate` 相同的结构。

| 情况 | 响应 |
| --- | --- |
| 未完成 | 409 `TASK_NOT_FINISHED` |
| 执行失败 | 500 + 原始错误码 |
| 已取消 | 409 `TASK_CANCELED` |
| 不存在/已过期 | 404 `TASK_NOT_FOUND` |

### DELETE /tasks/{task_id}

取消 `pending` / `running` 任务。已结束的任务返回 409 `TASK_ALREADY_FINISHED`。

### GET /tasks

列出当前进程内的任务（调试用）。

---

## 历史记录

### GET /history

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | 1 | 页码 |
| `page_size` | integer | 20 | 每页条数，最大 100 |
| `q` | string | | 搜索关键字（匹配输入与回复文本） |
| `status` | string | | `succeeded` / `failed` 等 |
| `provider` | string | | 按服务商过滤（LLM 或 TTS） |

```json
{
  "items": [
    {
      "id": 1,
      "input_text": "请用三句话介绍一下杭州西湖。",
      "reply_text": "杭州西湖位于……",
      "llm_provider": "openai",
      "llm_model": "gpt-4o-mini",
      "tts_provider": "openai",
      "tts_voice": "nova",
      "tts_model": "",
      "audio_format": "mp3",
      "audio_bytes": 48210,
      "duration_ms": 4210,
      "llm_latency_ms": 812,
      "tts_latency_ms": 1203,
      "total_latency_ms": 2100,
      "usage": { "prompt_tokens": 20, "completion_tokens": 90, "total_tokens": 110 },
      "status": "succeeded",
      "degraded": false,
      "degraded_reason": null,
      "error_code": null,
      "error_message": null,
      "created_at": "2026-10-12T15:30:46+08:00",
      "audio_url": "/api/v1/audio/1"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20,
  "pages": 1
}
```

### GET /history/{id}

单条详情，字段同上。

### DELETE /history/{id}

删除单条记录（同时删除音频文件）。返回 `204 No Content`。

### DELETE /history

```bash
# 批量删除
curl -X DELETE http://localhost:8000/api/v1/history \
  -H 'Content-Type: application/json' -d '{"ids":[1,2,3]}'

# 清空全部
curl -X DELETE http://localhost:8000/api/v1/history \
  -H 'Content-Type: application/json' -d '{"all":true}'
```

```json
{ "deleted": 3 }
```

### GET /history/stats

```json
{
  "total": 12, "succeeded": 11, "failed": 1,
  "total_audio_ms": 48200, "audio_files": 11, "audio_bytes": 512000
}
```

### GET /history/export

导出 CSV（`text/csv`，带 UTF-8 BOM，Excel 可直接打开）。

---

## 音频

### GET /audio/{record_id}

返回原始音频字节流，可直接用作 `<audio src>`。

| 参数 | 说明 |
| --- | --- |
| `download` | `1` / `true` 时附带 `Content-Disposition: attachment` 触发下载 |

**支持 HTTP Range 请求**（可拖动进度条）：

```bash
curl -H 'Range: bytes=0-99' http://localhost:8000/api/v1/audio/1 -o part.bin
# -> 206 Partial Content, Content-Range: bytes 0-99/48210
```

| 情况 | 响应 |
| --- | --- |
| 记录不存在或音频已删除 | 404 `AUDIO_NOT_FOUND` |
| Range 非法 | 416 |

---

## 系统设置

用户偏好设置。**不接受任何 API 密钥字段**（传入会返回 422）。

### GET /settings

```json
{
  "default_llm_provider": "openai",
  "default_llm_model": "gpt-4o-mini",
  "default_tts_provider": "openai",
  "default_tts_voice": "alloy",
  "default_speed": 1.0,
  "default_system_prompt": "你是一个乐于助人的中文助手，回答简洁、口语化，适合朗读。",
  "auto_save_history": true,
  "auto_save_credentials": false,
  "request_timeout_seconds": 120,
  "temperature": 0.7,
  "max_tokens": 1024,
  "updated_at": "2026-10-12T15:30:46+08:00"
}
```

### PUT /settings

局部更新，只传要改的字段。

```bash
curl -X PUT http://localhost:8000/api/v1/settings \
  -H 'Content-Type: application/json' \
  -d '{"default_tts_voice":"nova","default_speed":1.25,"auto_save_history":false}'
```

```bash
# 尝试持久化密钥 -> 422 VALIDATION_ERROR
curl -X PUT http://localhost:8000/api/v1/settings \
  -H 'Content-Type: application/json' -d '{"api_key":"sk-..."}'
```

---

## 媒体与声音克隆

用于「声音克隆」模式：上传参考音频/视频 → （可选）裁剪 → 创建音色 → 用该音色朗读文本。

### POST /media/inspect

上传参考文件并读取元数据（multipart，字段名 `file`）。

```bash
curl -X POST http://localhost:8000/api/v1/media/inspect \
  -F "file=@/path/to/reference.wav;type=audio/wav"
```

```json
{
  "media_id": "3f2b1a4c9d8e4f7ab6c51e2d3f4a5b6c",
  "filename": "reference.wav",
  "content_type": "audio/wav",
  "kind": "audio",
  "size_bytes": 96044,
  "duration_ms": 3000,
  "sample_rate": 16000,
  "channels": 1,
  "format": "wav",
  "trimmable": true,
  "preview_url": "/api/v1/media/3f2b1a4c…",
  "warnings": []
}
```

| 错误码 | 状态 | 说明 |
| --- | --- | --- |
| `MEDIA_UNSUPPORTED_FORMAT` | 400 | 扩展名不在白名单内 |
| `MEDIA_TOO_LARGE` | 413 | 超过 `max_upload_mb`（默认 50） |

两个错误都带 `detail.suggestion`，直接展示给用户即可。

### POST /media/trim

从已上传的文件中裁出 `[start_ms, end_ms)`，输出 WAV。

```bash
curl -X POST http://localhost:8000/api/v1/media/trim \
  -H 'Content-Type: application/json' \
  -d '{"media_id":"3f2b…","start_ms":500,"end_ms":1500,"output_format":"wav"}'
```

```json
{
  "media_id": "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d",
  "preview_url": "/api/v1/media/9a8b…",
  "duration_ms": 1000,
  "size_bytes": 32044,
  "format": "wav"
}
```

* **WAV** 用 Python 标准库裁剪，精确且无依赖。
* **其他格式**需要 ffmpeg；未安装时返回 `MEDIA_TRIM_UNSUPPORTED`(422)，带
  `detail.install_hint` 与 `detail.suggestion`（含安装命令）。

### GET /media/{media_id}

流式返回上传/裁剪后的文件，供 `<audio>` / `<video>` 在线试听试看。
支持 HTTP Range（拖动进度条）。不存在时返回 404 `MEDIA_NOT_FOUND`。

### DELETE /media/{media_id}

删除上传文件（同时删除元数据 sidecar）。返回 `204`。

### POST /voices/clone

用参考片段创建一个音色。**API Key 仅用于本次调用，不落库。**

```bash
curl -X POST http://localhost:8000/api/v1/voices/clone \
  -H 'Content-Type: application/json' \
  -d '{
    "provider": "elevenlabs",
    "api_key": "...",
    "name": "我的声音",
    "media_id": "3f2b…",
    "model": "eleven_multilingual_v2"
  }'
```

```json
{
  "id": 1,
  "voice_id": "21m00Tcm4TlvDq8ikWAM",
  "provider": "elevenlabs",
  "name": "我的声音",
  "model": "eleven_multilingual_v2",
  "duration_ms": 3000,
  "created_at": "2026-10-12T23:00:00+08:00"
}
```

服务商不支持克隆时返回 `VOICE_CLONE_UNSUPPORTED`(400)，其 `detail.suggestion`
会明确告知可以改用哪些服务商。

创建后的音色会出现在 `GET /providers` 对应 TTS 条目的 `voices[]` 中，并标记
`"cloned": true`，可直接用于生成。

### GET /voices · DELETE /voices/{id}

```json
{ "items": [ { "id": 1, "voice_id": "...", "provider": "...", "name": "...", "model": "", "duration_ms": 3000, "created_at": "..." } ] }
```

`DELETE /voices/{id}` → `204`。

### 参考音频直传（无需克隆接口的服务商）

对声明了 `supports_reference_audio` 的服务商（如 `custom`），可以在生成请求里
直接带参考片段：

```json
{
  "text": "用这个声音朗读这句话",
  "llm": { "provider": "mock" },
  "tts": { "provider": "custom", "api_key": "...", "model": "mimo-v2.5-tts", "base_url": "https://api.example.com/v1" },
  "options": { "reference_media_id": "3f2b…" }
}
```

若所选服务商既无克隆接口也不支持参考音频，返回 `TTS_REFERENCE_UNSUPPORTED`(400)
并给出可操作建议。

---

## 系统与元信息

### GET /health

```json
{
  "status": "ok",
  "version": "1.0.0",
  "uptime_seconds": 1234.5,
  "environment": "production",
  "database": "ok",
  "providers": { "llm": 9, "tts": 7 },
  "timestamp": "2026-10-12T15:30:46+00:00"
}
```

`status` 为 `degraded` 时 `database` 字段给出失败原因。根路径 `/health` 同样可用（进程守护探针）。

### GET /capabilities

运行时能力探测。前端据此决定显示哪些功能、给出哪种提示（例如没装 ffmpeg 时提示只能裁剪 WAV）。

```json
{
  "ffmpeg": {
    "available": false,
    "path": null,
    "version": null,
    "message": "未检测到 ffmpeg，仅支持 WAV 格式的裁剪与视频转音频。安装后自动启用：winget install Gyan.FFmpeg"
  },
  "voice_clone": {
    "supported_providers": ["elevenlabs", "minimax"],
    "reference_audio_providers": ["custom"],
    "note": "支持克隆的服务商：ElevenLabs、MiniMax、以及任何实现了克隆接口的自定义端点"
  },
  "upload": {
    "accept": ".aac,.avi,.flac,.m4a,.mkv,.mov,.mp3,.mp4,.ogg,.opus,.wav,.webm",
    "max_size_mb": 50,
    "trimmable_formats": ["wav"],
    "video_extraction_available": false
  }
}
```

### GET /meta/error-codes

```json
{
  "total": 42,
  "codes": [
    { "code": "AUDIO_NOT_FOUND", "http_status": 404, "description": "音频文件不存在" },
    { "code": "LLM_AUTH_FAILED", "http_status": 401, "description": "LLM 鉴权失败（密钥错误或未开通）" }
  ]
}
```

### GET /meta/metrics

```json
{
  "uptime_seconds": 1234.5,
  "requests_total": 152,
  "generations_total": 12,
  "generations_succeeded": 11,
  "generations_failed": 1,
  "generations_degraded": 2,
  "active_tasks": 1,
  "provider_calls": { "llm:openai:ok": 10, "tts:openai:ok": 9 }
}
```

### GET /meta/config

非敏感运行时配置（文本长度上限、超时、限流阈值等），供前端展示与调优。

---

## 完整调用示例

### 异步生成 + 轮询（推荐给 Web）

```bash
#!/usr/bin/env bash
set -euo pipefail
API=http://localhost:8000/api/v1

cat > /tmp/req.json <<'JSON'
{
  "text": "请用三句话介绍一下杭州西湖。",
  "llm": {
    "provider": "openai",
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "system_prompt": "你是一个乐于助人的中文助手，回答口语化，适合朗读。"
  },
  "tts": { "provider": "openai", "api_key": "sk-...", "voice": "nova", "speed": 1.0, "format": "mp3" },
  "options": { "auto_save": true, "fallback_enabled": true }
}
JSON

TASK_ID=$(curl -sS -X POST "$API/tasks" -H 'Content-Type: application/json' \
  --data-binary @/tmp/req.json | python -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')
echo "task: $TASK_ID"

while :; do
  POLL=$(curl -sS "$API/tasks/$TASK_ID")
  STATUS=$(echo "$POLL" | python -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  echo "$POLL" | python -c 'import json,sys; d=json.load(sys.stdin); print(f"  {d[\"stage\"]:8s} {d[\"progress\"]:3d}%  {d[\"message\"]}")'
  [ "$STATUS" = "succeeded" ] && break
  [ "$STATUS" = "failed" ] && { echo "FAILED"; echo "$POLL"; exit 1; }
  sleep 1
done

# 取结果并下载音频
curl -sS "$API/tasks/$TASK_ID/result" -o /tmp/result.json
python -c 'import json;d=json.load(open("/tmp/result.json"));print("\n回复:",d["reply_text"][:120]);print("音频:",d["audio_url"])'
curl -sS "$API/audio/$(python -c 'import json;print(json.load(open("/tmp/result.json"))["record_id"])')" -o /tmp/reply.mp3
echo "已保存到 /tmp/reply.mp3"
```

### Python 客户端

```python
import time
import httpx

API = "http://localhost:8000/api/v1"
HEADERS = {"Content-Type": "application/json"}

payload = {
    "text": "请用三句话介绍一下杭州西湖。",
    "llm": {"provider": "openai", "api_key": "sk-...", "model": "gpt-4o-mini"},
    "tts": {"provider": "openai", "api_key": "sk-...", "voice": "nova", "format": "mp3"},
    "options": {"auto_save": True},
}

with httpx.Client(timeout=300) as client:
    # 1) 提交
    task = client.post(f"{API}/tasks", json=payload, headers=HEADERS).json()
    task_id = task["task_id"]
    print(f"task_id={task_id}")

    # 2) 轮询
    while True:
        status = client.get(f"{API}/tasks/{task_id}").json()
        print(f"  {status['stage']:8s} {status['progress']:3d}%  {status['message']}")
        if status["status"] == "succeeded":
            break
        if status["status"] == "failed":
            raise RuntimeError(status["error"])
        time.sleep(1)

    # 3) 取结果 + 下载音频
    result = client.get(f"{API}/tasks/{task_id}/result").json()
    print("\n回复:", result["reply_text"])
    audio = client.get(f"http://localhost:8000{result['audio_url']}").content
    with open("reply.mp3", "wb") as fh:
        fh.write(audio)
    print(f"音频已保存 ({len(audio)} 字节, {result['duration_ms']} ms)")
```

### 仅重新合成语音（不消耗 token）

```bash
curl -X POST http://localhost:8000/api/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "占位文本，会被 reuse_reply_text 覆盖",
    "llm": { "provider": "openai", "api_key": "sk-...", "model": "gpt-4o-mini" },
    "tts": { "provider": "elevenlabs", "api_key": "...", "voice": "21m00Tcm4TlvDq8ikWAM" },
    "options": { "reuse_reply_text": "这段文本将被逐字合成为语音，不经过大模型。" }
  }'
```

响应中 `llm.finish_reason` 为 `"reused"`，`llm.latency_ms` 为 0。

### 零密钥体验（内置 mock 服务商）

```bash
curl -X POST http://localhost:8000/api/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "你好，这是一次离线演示。",
    "llm": { "provider": "mock" },
    "tts": { "provider": "mock", "voice": "mock-neutral", "format": "wav" }
  }'
```

无需任何密钥即可得到真实可播放的 WAV 音频，用于验证整条链路。