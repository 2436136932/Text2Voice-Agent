"""Abstract adapter contracts and shared upstream plumbing.

Everything a provider module needs lives here:

* value objects (:class:`LLMRequest`, :class:`LLMResult`, :class:`TTSRequest`,
  :class:`TTSResult`, :class:`VoiceInfo`, :class:`AdapterInfo`);
* the process-wide :class:`httpx.AsyncClient` (connection pooling, timeouts);
* :func:`map_upstream_error`, which turns any vendor HTTP failure into the
  single :class:`AppError` taxonomy used across the platform.

Contract for every adapter
--------------------------
1. ``id`` is a stable lowercase slug used in API payloads and the database.
2. ``api_key`` is passed per call and must never be stored on the instance.
3. Failures raise :class:`AppError` - never ``httpx`` exceptions.
4. ``describe()`` powers ``GET /providers`` so the UI can render itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

import httpx

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, log_upstream_call

logger = get_logger(__name__)

ProviderKind = Literal["llm", "tts"]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AdapterInfo:
    """UI-facing description of a provider (``GET /providers`` entry)."""

    id: str
    name: str
    kind: ProviderKind
    requires_base_url: bool = False
    supports_base_url: bool = True
    #: ``False`` for locally hosted providers (e.g. Ollama) where a key is optional.
    requires_api_key: bool = True
    #: Optional python package that must be importable for this provider to
    #: work (e.g. ``websockets`` for the Edge TTS adapter). ``None`` means the
    #: provider only needs the core dependencies.
    requires_extra: str | None = None
    api_key_hint: str = ""
    docs_url: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "requires_base_url": self.requires_base_url,
            "supports_base_url": self.supports_base_url,
            "requires_api_key": self.requires_api_key,
            "requires_extra": self.requires_extra,
            "api_key_hint": self.api_key_hint,
            "docs_url": self.docs_url,
            "notes": self.notes,
        }


@dataclass(slots=True)
class VoiceInfo:
    """A selectable voice / speaker."""

    id: str
    name: str
    gender: str = "neutral"
    language: str = "multi"
    description: str = ""
    #: Provider specific passthrough (e.g. Azure ``style``, Volcengine ``cluster``).
    extra: dict[str, Any] = field(default_factory=dict)
    #: True for voices the user created through this app (see
    #: :class:`app.db.models.ClonedVoice`); the UI groups them separately.
    cloned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "gender": self.gender,
            "language": self.language,
            "description": self.description,
            "cloned": self.cloned,
        }


@dataclass(slots=True)
class VoiceCloneRequest:
    """One vendor-side voice-cloning call.

    ``audio`` holds the raw reference clip. It must never be logged - callers
    log only ``len(audio)``.
    """

    api_key: str
    name: str
    audio: bytes
    audio_filename: str = "reference.wav"
    base_url: str | None = None
    model: str = ""
    #: Provider specific knobs (e.g. MiniMax ``group_id``, ElevenLabs ``labels``).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VoiceCloneResult:
    """Outcome of a successful clone call."""

    voice_id: str
    provider: str
    name: str
    model: str = ""
    #: Vendor-reported duration of the reference clip, when it reports one.
    duration_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)



@dataclass(slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(slots=True)
class LLMRequest:
    """Normalized chat-completion request."""

    prompt: str
    api_key: str
    model: str = ""
    base_url: str | None = None
    system_prompt: str | None = None
    temperature: float = 0.7
    max_tokens: int = 1024
    history: Sequence[ChatMessage] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None


@dataclass(slots=True)
class TTSRequest:
    """Normalized speech-synthesis request."""

    text: str
    api_key: str
    voice: str = ""
    model: str = ""
    base_url: str | None = None
    speed: float = 1.0
    audio_format: str = "mp3"
    language: str = "zh-CN"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TTSResult:
    audio: bytes
    provider: str
    voice: str
    audio_format: str
    latency_ms: int
    content_type: str = "audio/mpeg"
    #: Estimated speech duration, derived from the audio bytes when the vendor
    #: does not report it (see :func:`app.services.audio.estimate_duration_ms`).
    duration_ms: int | None = None


@dataclass(slots=True)
class ValidationResult:
    """Outcome of ``POST /providers/validate``."""

    valid: bool
    message: str
    latency_ms: int
    provider: str
    kind: ProviderKind
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared client (created lazily, closed on shutdown)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=max(settings.llm_timeout_seconds, settings.tts_timeout_seconds),
                write=30.0,
                pool=10.0,
            ),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            headers={"User-Agent": f"Text2Voice-Agent/{settings.version}"},
            follow_redirects=True,
        )
    return _client


async def shutdown_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------
_AUTH_STATUSES = {401, 403}
_RATE_STATUSES = {429}
_TIMEOUT_EXCEPTIONS = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)


def _truncate(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _extract_upstream_message(body: Any) -> str:
    """Best-effort extraction of a vendor error message from a JSON body."""
    if isinstance(body, dict):
        for key in ("message", "error_description", "detail", "msg", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = value.get("message") or value.get("msg")
                if isinstance(nested, str) and nested:
                    return nested
    if isinstance(body, str):
        return body
    return ""


def map_upstream_error(
    *,
    kind: ProviderKind,
    provider: str,
    status_code: int | None = None,
    body: Any = None,
    exception: Exception | None = None,
    endpoint: str = "",
) -> AppError:
    """Convert any upstream failure into the platform :class:`AppError`.

    Retryability is decided here so callers do not need vendor knowledge:
    401/403 and 4xx validation errors are terminal, 429/5xx/timeouts retry.
    """
    prefix = kind.upper()
    upstream_message = _truncate(_extract_upstream_message(body))
    detail: dict[str, Any] = {"provider": provider, "kind": kind}
    if endpoint:
        detail["endpoint"] = endpoint
    if status_code is not None:
        detail["upstream_status"] = status_code
    if upstream_message:
        detail["upstream_message"] = upstream_message

    # -- transport level ----------------------------------------------------
    if exception is not None:
        if isinstance(exception, _TIMEOUT_EXCEPTIONS) or isinstance(exception, httpx.TimeoutException):
            return AppError(
                ErrorCode(f"{prefix}_TIMEOUT"),
                f"{provider} 服务响应超时，请稍后重试或缩短输入文本。",
                detail=detail,
                retryable=True,
            )
        if isinstance(exception, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
            return AppError(
                ErrorCode(f"{prefix}_UPSTREAM_ERROR"),
                f"无法连接到 {provider} 服务，请检查网络或 base_url 配置。",
                detail=detail,
                retryable=True,
            )
        return AppError(
            ErrorCode(f"{prefix}_UPSTREAM_ERROR"),
            f"调用 {provider} 服务时发生网络错误：{type(exception).__name__}",
            detail=detail,
            retryable=True,
        )

    assert status_code is not None  # by contract one of the two is provided

    if status_code in _AUTH_STATUSES:
        return AppError(
            ErrorCode(f"{prefix}_AUTH_FAILED"),
            f"{provider} 鉴权失败，请检查 API 密钥是否正确、是否已开通对应服务。",
            detail=detail,
            retryable=False,
        )
    if status_code in _RATE_STATUSES:
        return AppError(
            ErrorCode(f"{prefix}_RATE_LIMITED"),
            f"{provider} 触发限流，请降低请求频率后重试。",
            detail=detail,
            retryable=True,
        )
    if status_code == 400 and kind == "tts" and "voice" in upstream_message.lower():
        return AppError(
            ErrorCode.TTS_INVALID_VOICE,
            f"{provider} 不支持所选音色：{upstream_message or '未知音色'}",
            detail=detail,
            retryable=False,
        )
    if status_code == 404:
        return AppError(
            ErrorCode(f"{prefix}_UPSTREAM_ERROR"),
            f"{provider} 接口不存在（404），请检查模型名或 base_url 配置。",
            detail=detail,
            retryable=False,
        )
    if 400 <= status_code < 500:
        return AppError(
            ErrorCode(f"{prefix}_ERROR"),
            f"{provider} 拒绝了请求（HTTP {status_code}）：{upstream_message or '参数不合法'}",
            detail=detail,
            retryable=False,
        )
    if status_code >= 500:
        return AppError(
            ErrorCode(f"{prefix}_UPSTREAM_ERROR"),
            f"{provider} 服务端异常（HTTP {status_code}），请稍后重试。",
            detail=detail,
            retryable=True,
        )
    return AppError(
        ErrorCode(f"{prefix}_ERROR"),
        f"调用 {provider} 失败（HTTP {status_code}）。",
        detail=detail,
        retryable=False,
    )


async def request_json(
    *,
    method: str,
    url: str,
    kind: ProviderKind,
    provider: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> tuple[Any, int]:
    """Perform a JSON-ish upstream call and return ``(parsed_body, status)``.

    Any failure is translated by :func:`map_upstream_error`, so adapters stay
    free of ``httpx`` error handling.
    """
    client = get_http_client()
    log_upstream_call(logger, provider=provider, endpoint=url, payload=json_body or data or {})
    try:
        response = await client.request(
            method,
            url,
            headers=headers,
            json=json_body,
            data=data,
            files=files,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - normalized below
        raise map_upstream_error(kind=kind, provider=provider, exception=exc, endpoint=url) from exc

    if response.status_code >= 400:
        try:
            body: Any = response.json()
        except Exception:  # noqa: BLE001 - non-JSON error page
            body = response.text
        raise map_upstream_error(
            kind=kind,
            provider=provider,
            status_code=response.status_code,
            body=body,
            endpoint=url,
        )

    try:
        return response.json(), response.status_code
    except Exception:  # noqa: BLE001
        return response.text, response.status_code


async def request_bytes(
    *,
    method: str,
    url: str,
    kind: ProviderKind,
    provider: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> tuple[bytes, str, int]:
    """Perform an upstream call that returns raw bytes (audio).

    Returns ``(content, content_type, status)``.
    """
    client = get_http_client()
    log_upstream_call(logger, provider=provider, endpoint=url, payload=json_body or data or {})
    try:
        response = await client.request(
            method,
            url,
            headers=headers,
            json=json_body,
            data=data,
            files=files,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        raise map_upstream_error(kind=kind, provider=provider, exception=exc, endpoint=url) from exc

    if response.status_code >= 400:
        try:
            body: Any = response.json()
        except Exception:  # noqa: BLE001
            body = response.text
        raise map_upstream_error(
            kind=kind, provider=provider, status_code=response.status_code, body=body, endpoint=url
        )

    if not response.content:
        raise AppError(
            ErrorCode(f"{kind.upper()}_UPSTREAM_ERROR"),
            f"{provider} 返回了空的音频数据。",
            detail={"provider": provider, "endpoint": url},
            retryable=True,
        )
    return response.content, response.headers.get("content-type", "application/octet-stream"), response.status_code


# ---------------------------------------------------------------------------
# Abstract contracts
# ---------------------------------------------------------------------------
class BaseAdapter(ABC):
    """Common metadata + description plumbing."""

    id: str = ""
    name: str = ""
    kind: ProviderKind = "llm"
    #: Optional python package this adapter needs at call time. When absent the
    #: provider is hidden from ``GET /providers`` (see
    #: :meth:`app.adapters.registry.AdapterRegistry.available_tts`).
    requires_extra: str | None = None
    #: Whether the provider needs a user-supplied endpoint. Declared on the
    #: class (not only in ``info()``) so subclasses that inherit an ``info()``
    #: implementation from a parent vendor still report it correctly.
    requires_base_url: bool = False
    supports_base_url: bool = True
    requires_api_key: bool = True
    api_key_hint: str = ""
    docs_url: str = ""
    notes: str = ""

    @abstractmethod
    def info(self) -> AdapterInfo:
        """Return the static description used by ``GET /providers``."""

    def resolve_base_url(self, base_url: str | None) -> str:
        """Return the effective base URL, normalized (no trailing slash)."""
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        """Serialize the adapter for ``GET /providers``.

        The class attributes are treated as authoritative for the capability
        flags. Several vendors implement ``info()`` by constructing
        ``AdapterInfo`` field-by-field and inherit that method from a parent
        (e.g. ``CustomTTSAdapter`` <- ``OpenAITTSAdapter``); without this sync a
        subclass that sets ``requires_base_url = True`` would still be
        advertised as ``false`` and the UI would never mark the field required.
        """
        payload = self.info().to_dict()
        payload.update(
            {
                "requires_base_url": self.requires_base_url,
                "supports_base_url": self.supports_base_url,
                "requires_api_key": self.requires_api_key,
                "requires_extra": self.requires_extra,
                "api_key_hint": self.api_key_hint or payload.get("api_key_hint", ""),
                "docs_url": self.docs_url or payload.get("docs_url", ""),
                "notes": self.notes or payload.get("notes", ""),
            }
        )
        return payload


class LLMAdapter(BaseAdapter):
    """Unified chat-completion interface."""

    kind: ProviderKind = "llm"
    default_model: str = ""
    models: tuple[dict[str, str], ...] = ()

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResult:
        """Generate a completion or raise :class:`AppError`."""

    async def validate(self, request: LLMRequest) -> ValidationResult:
        """Cheap credential probe used by ``POST /providers/validate``.

        The default implementation performs a 1-token completion, which works
        for every OpenAI-compatible vendor. Override for cheaper endpoints.
        """
        import time

        started = time.perf_counter()
        probe = LLMRequest(
            prompt="ping",
            api_key=request.api_key,
            model=request.model or self.default_model,
            base_url=request.base_url,
            system_prompt=None,
            temperature=0.0,
            max_tokens=1,
        )
        try:
            result = await self.complete(probe)
        except AppError as exc:
            return ValidationResult(
                valid=False,
                message=exc.message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                provider=self.id,
                kind="llm",
                detail={"code": exc.code.value, **exc.detail},
            )
        return ValidationResult(
            valid=True,
            message=f"连接成功，模型 {result.model} 可用。",
            latency_ms=int((time.perf_counter() - started) * 1000),
            provider=self.id,
            kind="llm",
            detail={"model": result.model},
        )

    def describe(self) -> dict[str, Any]:
        payload = super().describe()
        payload.update(
            {
                "default_model": self.default_model,
                "models": list(self.models),
            }
        )
        return payload


class TTSAdapter(BaseAdapter):
    """Unified text-to-speech interface."""

    kind: ProviderKind = "tts"
    default_voice: str = ""
    voices: tuple[VoiceInfo, ...] = ()
    supports_speed: bool = True
    speed_min: float = 0.5
    speed_max: float = 2.0
    formats: tuple[str, ...] = ("mp3",)
    #: Hard limit imposed by the vendor on a single synthesis call.
    max_chars: int = 4000
    # -- voice cloning capabilities (see app/services/voices.py) -------------
    #: True when the vendor exposes a real clone endpoint, so a clip is
    #: uploaded once and the returned id can be reused for every synthesis.
    supports_voice_clone: bool = False
    #: Models that can be used for cloning, in the same
    #: ``{"id": ..., "name": ...}`` shape as ``models``.
    clone_models: tuple[dict[str, str], ...] = ()
    #: Friendly, user-facing advice about which model to pick for cloning.
    clone_hint: str = ""
    #: True when a reference clip can be attached to a synthesis call instead of
    #: being cloned up front (no vendor clone API involved).
    supports_reference_audio: bool = False
    content_type_map: dict[str, str] = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "opus": "audio/ogg",
        "ogg": "audio/ogg",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "pcm": "audio/L16",
        "webm": "audio/webm",
        "ulaw": "audio/basic",
    }

    @abstractmethod
    async def synthesize(self, request: TTSRequest) -> TTSResult:
        """Synthesize speech or raise :class:`AppError`."""

    async def clone_voice(self, request: VoiceCloneRequest) -> VoiceCloneResult:
        """Create a reusable voice from a reference clip.

        The default implementation refuses politely: cloning is an opt-in
        capability, and a provider that has not implemented it must fail with a
        message that tells the user which provider *can* do the job, rather
        than with a generic 500. Adapters with a clone API override this and
        set :attr:`supports_voice_clone`.
        """
        raise AppError(
            ErrorCode.VOICE_CLONE_UNSUPPORTED,
            f"{self.name} 暂未实现声音克隆接口。",
            detail={
                "provider": self.id,
                "suggestion": (
                    "请选择支持克隆的服务商（如 elevenlabs、minimax）后再试，"
                    "或改用「参考音频」模式直接合成。"
                ),
            },
        )

    def content_type_for(self, audio_format: str) -> str:
        return self.content_type_map.get(audio_format.lower(), "application/octet-stream")

    def normalize_format(self, audio_format: str | None) -> str:
        fmt = (audio_format or self.formats[0]).lower()
        if fmt not in self.formats:
            raise AppError(
                ErrorCode.TTS_UNSUPPORTED_FORMAT,
                f"{self.name} 不支持音频格式 “{fmt}”，可选：{', '.join(self.formats)}。",
                detail={"provider": self.id, "format": fmt, "supported": list(self.formats)},
            )
        return fmt

    def clamp_speed(self, speed: float | None) -> float:
        if speed is None:
            return 1.0
        return max(self.speed_min, min(self.speed_max, float(speed)))

    def check_length(self, text: str) -> None:
        if len(text) > self.max_chars:
            raise AppError(
                ErrorCode.TTS_TEXT_TOO_LONG,
                f"文本长度 {len(text)} 超过 {self.name} 单次合成上限 {self.max_chars} 字符，请缩短文本。",
                detail={"provider": self.id, "length": len(text), "max_chars": self.max_chars},
            )

    async def validate(self, request: TTSRequest) -> ValidationResult:
        """Synthesize a very short phrase to prove the key/voice work."""
        import time

        started = time.perf_counter()
        probe = TTSRequest(
            text="测试",
            api_key=request.api_key,
            voice=request.voice or self.default_voice,
            # Forward the model: OpenAI-compatible gateways 404 when asked for a
            # model they do not host, so dropping it here would make "测试连接"
            # fail for every custom endpoint even though synthesis works.
            model=request.model or "",
            base_url=request.base_url,
            speed=1.0,
            audio_format=self.formats[0],
            extra=request.extra,
        )
        try:
            result = await self.synthesize(probe)
        except AppError as exc:
            return ValidationResult(
                valid=False,
                message=exc.message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                provider=self.id,
                kind="tts",
                detail={"code": exc.code.value, **exc.detail},
            )
        return ValidationResult(
            valid=True,
            message=f"连接成功，已合成 {len(result.audio)} 字节音频。",
            latency_ms=int((time.perf_counter() - started) * 1000),
            provider=self.id,
            kind="tts",
            detail={"voice": result.voice, "bytes": len(result.audio)},
        )

    def describe(self) -> dict[str, Any]:
        payload = super().describe()
        payload.update(
            {
                "default_voice": self.default_voice,
                "voices": [voice.to_dict() for voice in self.voices],
                "supports_speed": self.supports_speed,
                "speed_min": self.speed_min,
                "speed_max": self.speed_max,
                "formats": list(self.formats),
                "max_chars": self.max_chars,
                # Cloning capabilities: the UI decides whether to show the
                # "克隆音色" tab, and which model to preselect, from these.
                "supports_voice_clone": self.supports_voice_clone,
                "clone_models": list(self.clone_models),
                "clone_hint": self.clone_hint,
                "supports_reference_audio": self.supports_reference_audio,
            }
        )
        return payload