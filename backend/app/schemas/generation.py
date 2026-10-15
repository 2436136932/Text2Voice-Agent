"""Pydantic request/response models - the API contract.

These models are the single source of truth for the OpenAPI schema rendered at
``/docs``. Validation rules live here (length limits, ranges, provider ids),
so services can assume well-formed input.

Credential handling
-------------------
``api_key`` fields are typed ``SecretStr``. Pydantic therefore:

* hides the value in ``repr()``, ``str()`` and the generated docs;
* forces an explicit ``.get_secret_value()`` at the exact point of use.

Combined with ``safe_dump()`` (which drops credentials entirely), this is what
makes "keys are forwarded once and never persisted" enforceable rather than
aspirational.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from app.schemas.datetime_utils import AwareUTC

from app.core.config import settings

ProviderKind = Literal["llm", "tts"]
#: Task lifecycle states. ``pending``/``running`` are the only *non-terminal*
#: ones (the UI polls until it sees anything else); ``interrupted`` is the
#: terminal state a task falls into when the server restarted while it was
#: still queued or running — it can never complete, so it ends polling too.
TaskStatus = Literal["pending", "running", "succeeded", "failed", "canceled", "interrupted"]
TaskStage = Literal["queued", "llm", "tts", "persist", "done"]

#: Matches a leading URI scheme (``javascript:``, ``file:``, ``https:``…).
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")


class BaseSchema(BaseModel):
    """Common configuration for every schema."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="forbid",
        json_schema_extra=None,
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class VoiceSchema(BaseModel):
    id: str
    name: str
    gender: str = "neutral"
    language: str = "multi"
    description: str = ""
    #: True for voices the user cloned through this app. The UI groups these
    #: under "我的音色" and shows the delete action.
    cloned: bool = False


class ModelSchema(BaseModel):
    id: str
    name: str


class ProviderSchema(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    requires_base_url: bool = False
    supports_base_url: bool = True
    requires_api_key: bool = True
    requires_extra: str | None = None
    api_key_hint: str = ""
    docs_url: str = ""
    notes: str = ""
    # LLM only
    default_model: str | None = None
    models: list[ModelSchema] = Field(default_factory=list)
    # TTS only
    default_voice: str | None = None
    voices: list[VoiceSchema] = Field(default_factory=list)
    supports_speed: bool = False
    speed_min: float = 0.5
    speed_max: float = 2.0
    formats: list[str] = Field(default_factory=list)
    max_chars: int | None = None
    default_region: str | None = None
    #: Voice-cloning capabilities (see ``app/services/voices.py``).
    supports_voice_clone: bool = False
    supports_reference_audio: bool = False
    clone_models: list[ModelSchema] = Field(default_factory=list)
    clone_hint: str = ""
    #: Provider specific credential fields (e.g. Azure region, Volcengine app_id).
    extra_fields: list[dict[str, Any]] = Field(default_factory=list)


class ProviderListResponse(BaseModel):
    llm: list[ProviderSchema]
    tts: list[ProviderSchema]


class ValidateProviderRequest(BaseSchema):
    kind: ProviderKind
    provider: str = Field(min_length=1, max_length=32)
    api_key: SecretStr | None = None
    base_url: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=128)
    voice: str | None = Field(default=None, max_length=128)
    speed: float | None = Field(default=None, ge=0.1, le=4.0)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        return validate_base_url(value)


class ValidateProviderResponse(BaseModel):
    valid: bool
    message: str
    latency_ms: int
    provider: str
    kind: ProviderKind
    detail: dict[str, Any] = Field(default_factory=dict)


def validate_base_url(value: str | None) -> str | None:
    """Reject obviously wrong base URLs early (SSRF-ish input hygiene).

    Accepts three shapes, all of which are legitimate in practice:

    * a full URL - ``https://api.openai.com/v1``;
    * a bare host - ``my-gateway.internal:8000``;
    * a bare region/alias - ``eastasia`` (used by the Azure adapter).
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > 512:
        raise ValueError("base_url 过长")

    match = _SCHEME_RE.match(candidate)
    if match:
        scheme = match.group(1).lower()
        if scheme not in {"http", "https"}:
            raise ValueError("base_url 仅支持 http/https 协议")
    return candidate.rstrip("/")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
class LLMConfigSchema(BaseSchema):
    provider: str = Field(min_length=1, max_length=32)
    api_key: SecretStr | None = None
    model: str | None = Field(default=None, max_length=128)
    base_url: str | None = Field(default=None, max_length=512)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    system_prompt: str | None = Field(default=None, max_length=4000)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        return validate_base_url(value)


class TTSConfigSchema(BaseSchema):
    provider: str = Field(min_length=1, max_length=32)
    api_key: SecretStr | None = None
    voice: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    base_url: str | None = Field(default=None, max_length=512)
    speed: float = Field(default=1.0, ge=0.1, le=4.0)
    format: str = Field(default="mp3", max_length=16)
    language: str = Field(default="zh-CN", max_length=16)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        return validate_base_url(value)

    @field_validator("format")
    @classmethod
    def _lower_format(cls, value: str) -> str:
        return value.strip().lower()


class GenerateOptionsSchema(BaseSchema):
    auto_save: bool = True
    include_audio_base64: bool = False
    fallback_enabled: bool = True
    #: Reuse a previous reply verbatim and only re-synthesize audio.
    reuse_reply_text: str | None = Field(default=None, max_length=20000)
    #: Uploaded reference clip (``POST /media/inspect``) to use as the voice
    #: for this generation. Interpreted differently per provider:
    #: a real clone id is preferred, otherwise the clip is forwarded at
    #: synthesis time (see ``TTSAdapter.supports_reference_audio``).
    reference_media_id: str | None = Field(default=None, max_length=64)


class GenerateRequest(BaseSchema):
    """Body of ``POST /generate`` and ``POST /tasks``."""

    text: str = Field(min_length=1, max_length=settings.max_text_length)
    llm: LLMConfigSchema
    tts: TTSConfigSchema
    options: GenerateOptionsSchema = Field(default_factory=GenerateOptionsSchema)

    @field_validator("text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("文本不能为空")
        return value.strip()

    @model_validator(mode="after")
    def _check_reuse(self) -> "GenerateRequest":
        if self.options.reuse_reply_text is not None and not self.options.reuse_reply_text.strip():
            self.options.reuse_reply_text = None
        return self

    def safe_dump(self) -> dict[str, Any]:
        """Serialize **without any credential**, for storage and logging."""
        data = self.model_dump(mode="json", exclude={"llm": {"api_key"}, "tts": {"api_key"}})
        return data

    def key_fingerprints(self) -> dict[str, str | None]:
        """Non-reversible fingerprints for audit lines (never the raw key)."""
        from app.core.security import key_fingerprint

        llm_key = self.llm.api_key.get_secret_value() if self.llm.api_key else ""
        tts_key = self.tts.api_key.get_secret_value() if self.tts.api_key else ""
        return {
            "llm": key_fingerprint(llm_key) if llm_key else None,
            "tts": key_fingerprint(tts_key) if tts_key else None,
        }


class UsageSchema(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMResultSchema(BaseModel):
    provider: str
    model: str
    latency_ms: int
    usage: UsageSchema = Field(default_factory=UsageSchema)
    finish_reason: str | None = None


class TTSResultSchema(BaseModel):
    provider: str
    voice: str
    latency_ms: int
    format: str = "mp3"
    bytes: int = 0
    #: True when this synthesis actually used a reference clip (cloning). The
    #: UI shows "参考音频" instead of the (unused) preset voice name, which
    #: otherwise misleads users into thinking the vendor voice was used.
    used_reference_audio: bool = False


class GenerateResponse(BaseModel):
    """Body returned by ``POST /generate`` and ``GET /tasks/{id}/result``."""

    record_id: int
    reply_text: str
    input_text: str
    audio_url: str | None = None
    audio_format: str = "mp3"
    audio_bytes: int = 0
    audio_base64: str | None = None
    duration_ms: int | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    llm: LLMResultSchema
    tts: TTSResultSchema
    total_latency_ms: int = 0
    created_at: AwareUTC


class TaskSubmitResponse(BaseModel):
    task_id: str
    status: TaskStatus
    stage: TaskStage
    progress: int
    message: str = "任务已提交"


class TaskStatusResponse(BaseModel):
    task_id: str
    status: TaskStatus
    stage: TaskStage
    progress: int = Field(ge=0, le=100)
    message: str = ""
    record_id: int | None = None
    created_at: AwareUTC | None = None
    started_at: AwareUTC | None = None
    finished_at: AwareUTC | None = None
    elapsed_ms: int | None = None
    error: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class HistoryItem(BaseModel):
    id: int
    input_text: str
    reply_text: str
    llm_provider: str
    llm_model: str
    tts_provider: str
    tts_voice: str
    tts_model: str = ""
    audio_format: str
    audio_bytes: int
    duration_ms: int | None = None
    llm_latency_ms: int | None = None
    tts_latency_ms: int | None = None
    total_latency_ms: int | None = None
    usage: UsageSchema = Field(default_factory=UsageSchema)
    status: str
    degraded: bool = False
    degraded_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: AwareUTC | None = None
    audio_url: str | None = None


class HistoryListResponse(BaseModel):
    items: list[HistoryItem]
    total: int
    page: int
    page_size: int
    pages: int


class HistoryDeleteRequest(BaseSchema):
    ids: list[int] = Field(default_factory=list, max_length=500)
    all: bool = False

    @model_validator(mode="after")
    def _at_least_one(self) -> "HistoryDeleteRequest":
        if not self.all and not self.ids:
            raise ValueError("请至少选择一个要删除的记录，或设置 all=true")
        return self


class HistoryDeleteResponse(BaseModel):
    deleted: int


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class SettingsSchema(BaseModel):
    """System-level preferences only.

    Generation parameters (provider, model, voice, speed, system prompt) are
    deliberately absent: they belong to the generate page and are remembered in
    the browser, so storing a second copy here would only create a way for the
    two to disagree.
    """

    auto_save_history: bool = True
    auto_save_credentials: bool = True
    request_timeout_seconds: int = Field(default=120, ge=5, le=600)
    updated_at: AwareUTC | None = None


class SettingsUpdateRequest(BaseSchema):
    """Partial update; every field is optional."""

    auto_save_history: bool | None = None
    auto_save_credentials: bool | None = None
    request_timeout_seconds: int | None = Field(default=None, ge=5, le=600)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = "ok"
    version: str
    uptime_seconds: float
    environment: str = "development"
    database: str = "ok"
    providers: dict[str, int] = Field(default_factory=dict)
    timestamp: AwareUTC | None = None


class ErrorResponse(BaseModel):
    """Documented in OpenAPI so every client can rely on one error shape."""

    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": "TTS_AUTH_FAILED",
                "message": "openai 鉴权失败，请检查 API 密钥是否正确。",
                "detail": {"provider": "openai", "kind": "tts", "upstream_status": 401},
                "request_id": "8f14e45fceea167a5a36dedd4bea2543",
            }
        }
    )


class ErrorCodeItem(BaseModel):
    code: str
    http_status: int
    description: str


class ErrorCodeCatalogResponse(BaseModel):
    """``GET /meta/error-codes`` - self-documenting error taxonomy."""

    total: int
    codes: list[ErrorCodeItem]


class MetricsResponse(BaseModel):
    uptime_seconds: float
    requests_total: int
    generations_total: int
    generations_succeeded: int
    generations_failed: int
    generations_degraded: int
    active_tasks: int
    provider_calls: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Media (upload / inspect / trim)
# ---------------------------------------------------------------------------
class MediaInspectResponse(BaseModel):
    """Metadata for one stored upload."""

    media_id: str
    filename: str
    content_type: str
    #: ``"audio"`` or ``"video"``.
    kind: Literal["audio", "video"] = "audio"
    size_bytes: int
    duration_ms: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    format: str = ""
    #: Whether ``POST /media/trim`` can handle this file right now.
    trimmable: bool = False
    #: Ready-to-use URL for ``<audio src>`` / ``<video src>``.
    preview_url: str = ""
    #: Non-fatal problems (e.g. "视频需要 ffmpeg 才能提取音轨").
    warnings: list[str] = Field(default_factory=list)


class MediaTrimRequest(BaseSchema):
    """Body of ``POST /media/trim``."""

    media_id: str = Field(min_length=1, max_length=64)
    start_ms: int = Field(ge=0, le=86_400_000, description="裁剪起点（毫秒）")
    end_ms: int = Field(ge=1, le=86_400_000, description="裁剪终点（毫秒，不含）")
    output_format: Literal["wav"] = "wav"

    @model_validator(mode="after")
    def _ordered_range(self) -> "MediaTrimRequest":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms 必须大于 start_ms")
        if self.end_ms - self.start_ms > 600_000:
            raise ValueError("单次裁剪区间不能超过 10 分钟（600000 毫秒）")
        return self


class MediaTrimResponse(BaseModel):
    media_id: str
    preview_url: str
    duration_ms: int | None = None
    size_bytes: int = 0
    format: str = "wav"


class MediaEnhanceRequest(BaseSchema):
    """Body of ``POST /media/enhance``."""

    media_id: str = Field(min_length=1, max_length=64)


class MediaEnhanceResponse(BaseModel):
    media_id: str
    preview_url: str
    duration_ms: int | None = None
    size_bytes: int = 0
    format: str = "wav"
    #: Non-fatal notes ("optimized clip is very short", ...) shown in the UI.
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Voice cloning
# ---------------------------------------------------------------------------
class VoiceCloneRequestSchema(BaseSchema):
    """Body of ``POST /voices/clone``.

    ``api_key`` is a ``SecretStr``: it is forwarded to the vendor for this one
    call and never persisted (``cloned_voices`` has no credential column).
    """

    provider: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=80, description="音色名称（用户可见）")
    media_id: str = Field(min_length=1, max_length=64, description="参考音频的 media_id")
    api_key: SecretStr | None = None
    base_url: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=128)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        return validate_base_url(value)

    @field_validator("name")
    @classmethod
    def _non_blank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("音色名称不能为空")
        return value.strip()


class ClonedVoiceResponse(BaseModel):
    """One cloned voice; ``duration_ms`` is the reference clip's length."""

    id: int
    voice_id: str
    provider: str
    name: str
    model: str = ""
    created_at: AwareUTC | None = None
    duration_ms: int | None = None


class VoiceListResponse(BaseModel):
    items: list[ClonedVoiceResponse] = Field(default_factory=list)