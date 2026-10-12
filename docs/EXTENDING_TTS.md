# 扩展新的 TTS 服务商

本文档说明如何为 Text2Voice Agent 接入一个新的语音合成服务商。
**整个过程只需新增 1 个文件 + 修改 1 行注册代码**，不需要改动路由层、Schema、
服务层或前端任何代码——前端的能力表单是由后端 `GET /providers` 的元数据驱动的。

---

## 目录

- [1. 为什么这么简单：注册表模式](#1-为什么这么简单注册表模式)
- [2. 五步接入法](#2-五步接入法)
- [3. 完整示例：接入 Fish Audio](#3-完整示例接入-fish-audio)
- [4. TTSAdapter 接口参考](#4-ttsadapter-接口参考)
- [5. 必须遵守的约定](#5-必须遵守的约定)
- [6. 进阶：额外凭据字段](#6-进阶额外凭据字段)
- [7. 进阶：SSML 类服务商](#7-进阶ssml-类服务商)
- [8. 进阶：WebSocket / 非 HTTP 服务商](#8-进阶websocket--非-http-服务商)
- [9. 测试你的适配器](#9-测试你的适配器)
- [10. 检查清单](#10-检查清单)

---

## 1. 为什么这么简单：注册表模式

```
app/api/routes/*.py        ← 从不 import 具体适配器
app/services/*.py          ← 从不 import 具体适配器
        │
        │  只通过 id 查表
        ▼
app/adapters/registry.py   ← 唯一枚举服务商的地方
        │
        ▼
app/adapters/tts/<vendor>.py  ← 你只需要新增这个文件
```

服务层调用 `registry.tts("your_vendor")` 拿到一个 `TTSAdapter` 实例，
再调用它的 `synthesize()`。只要你的类满足这个接口，它就能立刻工作：

- `GET /providers` 自动列出它，前端自动渲染出音色下拉框、语速滑块、格式单选；
- `POST /providers/validate` 自动获得「测试连接」能力；
- 同步 `/generate`、异步 `/tasks`、历史记录、音频播放/下载全部自动打通；
- 错误统一走 `AppError`，自动获得正确的 HTTP 状态码与中文提示。

---

## 2. 五步接入法

### 第 1 步：创建适配器文件

新建 `backend/app/adapters/tts/<vendor>.py`。最简版本（约 60 行）：

```python
from __future__ import annotations

import time

from app.adapters.base import (
    AdapterInfo, TTSAdapter, TTSRequest, TTSResult, VoiceInfo, request_bytes,
)
from app.core.config import settings
from app.core.security import assert_credential


class YourVendorTTSAdapter(TTSAdapter):
    id = "yourvendor"                 # 唯一 slug，出现在 API 与数据库中
    name = "Your Vendor TTS"          # 前端显示名
    kind = "tts"
    default_voice = "voice-a"
    default_base_url = "https://api.yourvendor.com/v1"
    api_key_hint = "在控制台 → API Keys 获取"
    docs_url = "https://docs.yourvendor.com/tts"

    # 能力声明 —— 前端表单直接由这些值驱动
    supports_speed = True
    speed_min = 0.5
    speed_max = 2.0
    formats = ("mp3", "wav")
    max_chars = 3000

    voices = (
        VoiceInfo(id="voice-a", name="女声 A", gender="female", language="zh-CN"),
        VoiceInfo(id="voice-b", name="男声 B", gender="male", language="zh-CN"),
    )

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id, name=self.name, kind="tts",
            api_key_hint=self.api_key_hint, docs_url=self.docs_url,
        )

    def resolve_base_url(self, base_url: str | None) -> str:
        return (base_url or self.default_base_url).rstrip("/")

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        api_key = assert_credential(request.api_key, kind="tts", provider=self.id)
        self.check_length(request.text)
        audio_format = self.normalize_format(request.audio_format)
        voice = request.voice or self.default_voice

        started = time.perf_counter()
        content, content_type, _ = await request_bytes(
            method="POST",
            url=f"{self.resolve_base_url(request.base_url)}/tts",
            kind="tts",                       # 决定错误码前缀 TTS_*
            provider=self.id,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_body={
                "text": request.text,
                "voice": voice,
                "speed": self.clamp_speed(request.speed),
                "format": audio_format,
            },
            timeout=settings.tts_timeout_seconds,
        )
        return TTSResult(
            audio=content,
            provider=self.id,
            voice=voice,
            audio_format=audio_format,
            latency_ms=int((time.perf_counter() - started) * 1000),
            content_type=content_type or self.content_type_for(audio_format),
        )
```

### 第 2 步：注册到 registry

编辑 `backend/app/adapters/registry.py`，改两处：

```python
# 1) 导入
from app.adapters.tts import (
    AzureSpeechAdapter, EdgeTTSAdapter, ElevenLabsAdapter, MiniMaxTTSAdapter,
    MockLLMAdapter, MockTTSAdapter, OpenAITTSAdapter, VolcengineTTSAdapter,
    YourVendorTTSAdapter,                                    # ← 新增
)

# 2) 加进元组
_TTS_ADAPTERS: tuple[TTSAdapter, ...] = (
    OpenAITTSAdapter(),
    ElevenLabsAdapter(),
    AzureSpeechAdapter(),
    VolcengineTTSAdapter(),
    MiniMaxTTSAdapter(),
    EdgeTTSAdapter(),
    YourVendorTTSAdapter(),                                  # ← 新增
    MockTTSAdapter(),
)
```

同时把类导出加到 `backend/app/adapters/tts/__init__.py`：

```python
from app.adapters.tts.yourvendor import YourVendorTTSAdapter

__all__ = [..., "YourVendorTTSAdapter"]
```

### 第 3 步：重启服务，确认自动出现

```bash
cd backend && python -m app.main
curl -s http://localhost:8000/api/v1/providers | python -m json.tool | grep -A2 yourvendor
```

打开 <http://localhost:5173>，TTS 服务商下拉框里就会出现它，音色、语速范围、
格式选项都已自动渲染。

### 第 4 步：写测试

在 `backend/tests/test_adapters.py` 中加一个报文形状测试（见
[第 9 节](#9-测试你的适配器)）。用 monkeypatch 拦截 HTTP 传输，无需真实密钥。

### 第 5 步：跑测试

```bash
cd backend && pytest
```

`test_pipeline.py::TestAdapterContractCompliance` 会自动验证你的适配器满足
抽象契约（有音色、有格式、语速范围合法等），无需额外编写。

---

## 3. 完整示例：接入 Fish Audio

一个真实风格的完整实现，展示了常见需求：**JSON 返回 base64 音频**、
**服务商自定义错误码**、**多语言音色**。

```python
"""Fish Audio TTS adapter.

``POST /v1/tts`` 返回 JSON，音频以 base64 放在 ``data`` 字段中。
"""

from __future__ import annotations

import base64
import binascii
import time
from typing import Any

from app.adapters.base import (
    AdapterInfo,
    TTSAdapter,
    TTSRequest,
    TTSResult,
    VoiceInfo,
    request_json,
)
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.security import assert_credential

logger = get_logger(__name__)


class FishAudioTTSAdapter(TTSAdapter):
    id = "fishaudio"
    name = "Fish Audio"
    kind = "tts"
    default_voice = "e58b0d7efca34eb38d5c49837aa2b2c0"
    default_base_url = "https://api.fish.audio/v1"
    api_key_hint = "在 Fish Audio 控制台 → API Keys 获取"
    docs_url = "https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech"
    notes = "支持音色克隆，中英文表现自然；参考音色 ID 可在控制台复制。"

    supports_speed = True
    speed_min = 0.5
    speed_max = 2.0
    formats = ("mp3", "wav", "pcm")
    max_chars = 2000

    voices = (
        VoiceInfo(id="e58b0d7efca34eb38d5c49837aa2b2c0", name="默认女声", gender="female", language="zh-CN"),
        VoiceInfo(id="7f92f8afb8ec43bf81429cc1c9199cb1", name="沉稳男声", gender="male", language="zh-CN"),
        VoiceInfo(id="d75d5a3f9c5a4b5c9c1e2f3a4b5c6d7e", name="English Female", gender="female", language="en-US"),
    )

    #: Fish Audio 的格式名与我们的通用名不同，需要映射。
    FORMAT_MAP = {"mp3": "mp3", "wav": "wav", "pcm": "pcm"}

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id,
            name=self.name,
            kind="tts",
            api_key_hint=self.api_key_hint,
            docs_url=self.docs_url,
            notes=self.notes,
        )

    def resolve_base_url(self, base_url: str | None) -> str:
        return (base_url or self.default_base_url).rstrip("/")

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        api_key = assert_credential(request.api_key, kind="tts", provider=self.id)
        self.check_length(request.text)
        audio_format = self.normalize_format(request.audio_format)
        voice = request.voice or self.default_voice

        url = f"{self.resolve_base_url(request.base_url)}/tts"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "text": request.text,
            "reference_id": voice,
            "format": self.FORMAT_MAP[audio_format],
            "speed": round(self.clamp_speed(request.speed), 2),
        }

        started = time.perf_counter()
        body, _ = await request_json(
            method="POST", url=url, kind="tts", provider=self.id,
            headers=headers, json_body=payload,
            timeout=settings.tts_timeout_seconds,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        # --- 归一化服务商特有的错误码 -------------------------------------
        if not isinstance(body, dict):
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "Fish Audio 返回了非预期的响应格式。",
                detail={"provider": self.id}, retryable=True,
            )

        if body.get("status") == "error" or "data" not in body:
            message = str(body.get("message") or "unknown error")
            code = str(body.get("code") or "")
            # 凭据类问题 -> TTS_AUTH_FAILED（不可重试，前端提示检查密钥）
            if code in {"401", "403", "unauthorized"}:
                raise AppError(
                    ErrorCode.TTS_AUTH_FAILED,
                    f"Fish Audio 鉴权失败：{message}",
                    detail={"provider": self.id, "upstream_code": code},
                )
            # 其余 -> 上游异常（可重试）
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                f"Fish Audio 合成失败：{message}",
                detail={"provider": self.id, "upstream_code": code},
                retryable=True,
            )

        # --- 解码 base64 音频 ---------------------------------------------
        try:
            audio = base64.b64decode(body["data"])
        except (binascii.Error, ValueError, TypeError) as exc:
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "Fish Audio 返回的音频数据无法解码。",
                detail={"provider": self.id}, retryable=True,
            ) from exc

        logger.info(
            "tts synthesis ok provider=%s voice=%s bytes=%s latency_ms=%s",
            self.id, voice, len(audio), latency_ms,
        )
        return TTSResult(
            audio=audio,
            provider=self.id,
            voice=voice,
            audio_format=audio_format,
            latency_ms=latency_ms,
            content_type=self.content_type_for(audio_format),
        )
```

---

## 4. TTSAdapter 接口参考

### 4.1 必须实现

| 成员 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `str` | 唯一 slug，小写；会出现在 API、数据库与前端 |
| `name` | `str` | 前端显示名 |
| `info()` | `AdapterInfo` | 静态元数据 |
| `resolve_base_url(base_url)` | `str` | 返回规范化后的 base URL（无尾斜杠） |
| `synthesize(request)` | `TTSResult` | **核心方法**，失败时抛 `AppError` |

### 4.2 能力声明（前端表单由此驱动）

| 属性 | 默认 | 作用 |
| --- | --- | --- |
| `default_voice` | `""` | 默认音色 id |
| `voices` | `()` | `VoiceInfo` 元组，渲染音色下拉框 |
| `supports_speed` | `True` | 是否显示语速滑块 |
| `speed_min` / `speed_max` | `0.5` / `2.0` | 滑块范围 |
| `formats` | `("mp3",)` | 可选音频格式 |
| `max_chars` | `4000` | 单次合成字符上限（超出抛 `TTS_TEXT_TOO_LONG`） |
| `default_base_url` | — | 未填 base_url 时使用 |
| `api_key_hint` | `""` | 密钥输入框占位提示 |
| `docs_url` | `""` | 文档链接 |
| `notes` | `""` | 前端展示的说明文字 |
| `requires_api_key` | `True` | 设为 `False` 则隐藏密钥输入框 |
| `requires_extra` | `None` | 可选依赖包名（未安装则自动隐藏该服务商） |

### 4.3 基类提供的工具方法

```python
self.normalize_format(fmt)      # 校验格式，不支持则抛 TTS_UNSUPPORTED_FORMAT
self.clamp_speed(speed)         # 按 speed_min/max 夹紧
self.check_length(text)         # 超长抛 TTS_TEXT_TOO_LONG
self.content_type_for(fmt)      # "mp3" -> "audio/mpeg"
await self.validate(request)    # 默认实现：合成 "测试" 两个字
```

### 4.4 HTTP 辅助函数

```python
from app.adapters.base import request_json, request_bytes

# 返回 JSON 的接口
body, status = await request_json(
    method="POST", url=url, kind="tts", provider=self.id,
    headers=headers, json_body=payload, timeout=settings.tts_timeout_seconds,
)

# 直接返回音频字节的接口
content, content_type, status = await request_bytes(
    method="POST", url=url, kind="tts", provider=self.id,
    headers=headers, json_body=payload,
)

# 表单 / 文件上传 / 原始 body
await request_bytes(..., data={"text": "..."})
await request_bytes(..., data=ssml_string.encode("utf-8"))
await request_bytes(..., files={"file": ("audio.wav", b"...")})
```

这两个函数已经处理了：

- **超时 / 连接失败** → `TTS_TIMEOUT` / `TTS_UPSTREAM_ERROR`（标记可重试）
- **401 / 403** → `TTS_AUTH_FAILED`（不可重试）
- **429** → `TTS_RATE_LIMITED`（可重试）
- **5xx** → `TTS_UPSTREAM_ERROR`（可重试）
- **空响应体** → `TTS_UPSTREAM_ERROR`
- **日志脱敏**（请求体经 `RedactionFilter` 处理，密钥不会进日志）

### 4.5 TTSRequest / TTSResult

```python
@dataclass
class TTSRequest:
    text: str                      # 要合成的文本
    api_key: str                   # 明文密钥（仅本次调用有效）
    voice: str = ""                # 音色 id
    model: str = ""                # 模型名（可选）
    base_url: str | None = None    # 自定义端点
    speed: float = 1.0             # 语速
    audio_format: str = "mp3"      # 音频格式
    language: str = "zh-CN"        # 语言
    extra: dict[str, Any] = {}     # 服务商特有参数（如 region / app_id）

@dataclass
class TTSResult:
    audio: bytes                   # 音频字节（必须非空）
    provider: str                  # 填 self.id
    voice: str                     # 实际使用的音色
    audio_format: str              # 实际格式
    latency_ms: int                # 耗时
    content_type: str = "audio/mpeg"
    duration_ms: int | None = None # 服务商上报的时长（可选）
```

> `duration_ms` 可以留空：平台会自己解析 WAV/MP3/OGG/FLAC 帧头计算时长。
> 若服务商上报了准确值（如 MiniMax 的 `audio_length`），填上更精确。

---

## 5. 必须遵守的约定

### 5.1 绝不持久化密钥

```python
# ✅ 正确：按请求传入，用完即弃
async def synthesize(self, request: TTSRequest) -> TTSResult:
    api_key = assert_credential(request.api_key, kind="tts", provider=self.id)

# ❌ 错误：不要把密钥存到实例属性上
def __init__(self, api_key):
    self.api_key = api_key        # 适配器是单例！会串号且常驻内存
```

适配器在 registry 中是**进程级单例**，绝不能持有凭据状态。

### 5.2 失败必须抛 AppError

```python
# ✅ 正确
raise AppError(ErrorCode.TTS_AUTH_FAILED, "密钥无效", detail={"provider": self.id})

# ❌ 错误：裸异常会变成 500 INTERNAL_ERROR，前端拿不到有用信息
raise ValueError("bad key")
```

用 `request_json` / `request_bytes` 时，HTTP 层的异常已被自动转换。

### 5.3 区分「可重试」与「不可重试」

| 情况 | 错误码 | `retryable` |
| --- | --- | --- |
| 超时、429、5xx | `TTS_TIMEOUT` / `TTS_RATE_LIMITED` / `TTS_UPSTREAM_ERROR` | `True` |
| 401 / 403、参数错误、音色不存在 | `TTS_AUTH_FAILED` / `TTS_INVALID_VOICE` | `False` |

重试一个错误的密钥只会浪费配额，所以鉴权失败必须 `retryable=False`。

### 5.4 音色与格式必须是「声明过的」

前端只会展示 `voices` 里列出的音色、`formats` 里列出的格式。
用户也可能通过 API 直接传其他值——所以 `synthesize` 里应调用
`self.normalize_format()` 做二次校验（基类已实现）。

### 5.5 长文本要主动拦截

超过 `max_chars` 时应抛 `TTS_TEXT_TOO_LONG`（`self.check_length()` 已实现），
不要依赖服务商报错——那样用户只会看到一条模糊的上游错误。

---

## 6. 进阶：额外凭据字段

有些服务商需要多个凭据（Azure 要 region，火山引擎要 app_id + cluster，
MiniMax 要 GroupId）。它们通过 `extra` 字典传入，并在 `describe()` 中声明，
前端会自动渲染出对应输入框。

```python
def describe(self) -> dict:
    payload = super().describe()
    payload["extra_fields"] = [
        {
            "key": "region",              # 对应 request.extra["region"]
            "label": "区域 (region)",
            "type": "string",
            "required": True,
            "placeholder": "eastasia",
            "help": "Azure 语音资源的区域，例如 eastasia、eastus。",
        },
    ]
    return payload
```

读取方式：

```python
region = str(request.extra.get("region") or "eastasia")
```

参考实现：`app/adapters/tts/azure.py`（region）、
`app/adapters/tts/volcengine.py`（app_id + cluster）、
`app/adapters/tts/minimax.py`（group_id）。

> **注意**：`extra` 会随请求一起传递，但**不会**被写入数据库的
> `request_payload`（该字段使用 `safe_dump()`，只剔除 `api_key`）。
> 如果你把敏感值放进 `extra`，请改用 `api_key` 字段承载。

---

## 7. 进阶：SSML 类服务商

Azure 与 Edge 使用 SSML，语速通过 `<prosody rate="+50%">` 表达。
基类的 `speed` 是倍率（1.5 = 1.5 倍速），需转换成百分比：

```python
def build_ssml(self, request: TTSRequest, voice: str) -> str:
    speed = self.clamp_speed(request.speed)
    rate_percent = int(round((speed - 1.0) * 100))   # 1.5 -> +50%
    return (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
        f"<voice name='{escape(voice)}'>"
        f"<prosody rate='{rate_percent:+d}%'>{escape(request.text)}</prosody>"
        "</voice></speak>"
    )
```

**务必对文本做 XML 转义**（`xml.sax.saxutils.escape`），否则文本里的
`&`、`<` 会让请求体变成非法 XML。

参考实现：`app/adapters/tts/azure.py`、`app/adapters/tts/edge.py`。

---

## 8. 进阶：WebSocket / 非 HTTP 服务商

若服务商只提供 WebSocket 或需要 SDK，把阻塞调用放进线程池，避免卡住事件循环：

```python
class EdgeTTSAdapter(TTSAdapter):
    requires_extra = "websockets"     # 可选依赖，未安装则自动从列表中隐藏

    def _blocking_synthesize(self, request, voice, timeout) -> bytes:
        from websockets.sync.client import connect     # 延迟导入
        ...

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        audio = await asyncio.to_thread(self._blocking_synthesize, request, voice, 60.0)
        ...
```

`requires_extra` 会在依赖缺失时把该服务商从 `GET /providers` 中隐藏，
并在启动日志中给出提示，而不是等到用户点击才报错。

参考实现：`app/adapters/tts/edge.py`。

---

## 9. 测试你的适配器

### 9.1 报文形状测试（推荐，无需网络）

在 `backend/tests/test_adapters.py` 的 `TestProviderWireFormats` 类中加一个方法。
通过 monkeypatch 拦截 `httpx.AsyncClient.request`，验证**发出的请求长什么样**：

```python
@pytest.mark.asyncio
async def test_yourvendor_request_shape(self, monkeypatch):
    captured: dict = {}

    async def fake_request(self, method, url, **kwargs):
        captured.update({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
        return httpx.Response(200, content=b"ID3fakeaudio", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    adapter = registry.tts("yourvendor")
    result = await adapter.synthesize(
        TTSRequest(text="你好", api_key="test-key-123456", voice="voice-a", speed=1.5)
    )

    assert captured["url"] == "https://api.yourvendor.com/v1/tts"
    assert captured["headers"]["Authorization"] == "Bearer test-key-123456"
    assert captured["json"]["voice"] == "voice-a"
    assert captured["json"]["speed"] == 1.5
    assert result.audio == b"ID3fakeaudio"
```

### 9.2 错误归一化测试

```python
@pytest.mark.asyncio
async def test_yourvendor_auth_failure_is_typed(self, monkeypatch):
    async def fake_request(self, method, url, **kwargs):
        return httpx.Response(401, json={"message": "invalid key"},
                              request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    with pytest.raises(AppError) as exc:
        await registry.tts("yourvendor").synthesize(
            TTSRequest(text="你好", api_key="bad-key-123456")
        )
    assert exc.value.code is ErrorCode.TTS_AUTH_FAILED
```

### 9.3 自动获得的契约测试

`test_pipeline.py::TestAdapterContractCompliance` 会自动检查每个已注册的 TTS 适配器：

- `id` / `name` / `default_voice` 非空
- `voices` 至少一个
- `formats` 至少一个
- `speed_min < speed_max`
- `max_chars > 0`
- 每个声明的格式都有 `audio/*` 的 Content-Type

只要注册了，就会被自动覆盖。

### 9.4 端到端验证（有真实密钥时）

```bash
cd backend && python -m app.main
# 另开终端
curl -X POST http://localhost:8000/api/v1/providers/validate \
  -H 'Content-Type: application/json' \
  -d '{"kind":"tts","provider":"yourvendor","api_key":"YOUR_KEY","voice":"voice-a"}'
# -> {"valid":true,"message":"连接成功，已合成 12345 字节音频。","latency_ms":812, ...}
```

---

## 10. 检查清单

接入完成后逐项确认：

- [ ] 新建 `backend/app/adapters/tts/<vendor>.py`，类继承 `TTSAdapter`
- [ ] 声明了 `id` / `name` / `default_voice` / `voices` / `formats` / `speed_min` / `speed_max` / `max_chars`
- [ ] 实现了 `info()`、`resolve_base_url()`、`synthesize()`
- [ ] 用 `assert_credential()` 校验密钥，**没有**把密钥存到实例属性
- [ ] 用 `request_json` / `request_bytes` 发请求（或自行把异常转成 `AppError`）
- [ ] 服务商特有错误码已归一化到 `TTS_*` 体系，并正确设置 `retryable`
- [ ] 音频字节非空；返回了正确的 `content_type`
- [ ] 导出类到 `backend/app/adapters/tts/__init__.py`
- [ ] 注册到 `backend/app/adapters/registry.py` 的 `_TTS_ADAPTERS`
- [ ] 添加了报文形状测试与错误归一化测试
- [ ] `pytest` 全绿
- [ ] `GET /api/v1/providers` 能看到新服务商
- [ ] 前端下拉框出现，音色/语速/格式正确渲染
- [ ] 「测试连接」按钮可用
- [ ] 实际生成一次，音频可播放、可下载

---

## 参考实现

按复杂度递增，建议对照阅读：

| 文件 | 特点 | 适合参考 |
| --- | --- | --- |
| `app/adapters/tts/mock.py` | 无网络、纯本地生成 WAV | **最简单的起点**，约 120 行 |
| `app/adapters/tts/openai.py` | 直接返回音频字节 | 最常见的形态 |
| `app/adapters/tts/elevenlabs.py` | 格式名映射、`voice_settings` | 参数结构复杂的服务商 |
| `app/adapters/tts/volcengine.py` | JSON + base64 解码、自定义错误码 | 国内服务商常见形态 |
| `app/adapters/tts/minimax.py` | hex 解码、上报时长、GroupId 额外字段 | `extra_fields` 用法 |
| `app/adapters/tts/azure.py` | SSML、region 解析 | SSML 类服务商 |
| `app/adapters/tts/edge.py` | WebSocket、可选依赖、线程池 | 非 HTTP 服务商 |

LLM 侧同理：新增 `app/adapters/llm/<vendor>.py` 并注册到 `_LLM_ADAPTERS`。
若服务商兼容 OpenAI 协议，直接继承 `OpenAICompatibleAdapter` 即可（约 20 行）：

```python
from app.adapters.llm.openai_compatible import OpenAICompatibleAdapter

class YourLLMAdapter(OpenAICompatibleAdapter):
    id = "yourllm"
    name = "Your LLM"
    default_model = "your-model-v1"
    default_base_url = "https://api.yourllm.com/v1"
    api_key_hint = "sk-..."
    models = ({"id": "your-model-v1", "name": "your-model-v1"},)
```