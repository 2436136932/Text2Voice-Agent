"""Application settings loaded from environment variables / .env file.

All knobs are overridable via environment variables using the ``T2V_`` prefix,
e.g. ``T2V_PORT=9000`` or ``T2V_LOG_LEVEL=DEBUG``.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration for the Text2Voice Agent backend."""

    model_config = SettingsConfigDict(
        env_prefix="T2V_",
        env_file=(BACKEND_ROOT / ".env", BACKEND_ROOT / ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Application --------------------------------------------------------
    app_name: str = "Text2Voice Agent API"
    version: str = "1.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"

    # -- Server -------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1

    # -- CORS ---------------------------------------------------------------
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
        ]
    )

    # -- Storage ------------------------------------------------------------
    # Relative paths are resolved against the ``backend/`` directory.
    database_url: str = "sqlite+aiosqlite:///./data/text2voice.db"
    audio_dir: str = "./data/audio"
    #: Uploaded reference clips (audio/video) for voice cloning.
    media_dir: str = "./data/media"
    #: Generated audio is kept on disk only for this long (0 disables cleanup).
    audio_retention_days: int = 30
    #: Uploaded reference clips are scratch data — shorter retention by default.
    media_retention_days: int = 7
    #: Largest accepted upload (MB) for reference clips.
    max_upload_mb: int = 50

    # -- Synthesis cache ----------------------------------------------------
    # Deduplicates identical TTS requests (same provider/model/voice/speed/
    # format/text). 0 disables the cache entirely.
    tts_cache_max_entries: int = 500
    # Entries not touched for this many days are evicted regardless of count.
    tts_cache_ttl_days: int = 14

    # -- Security -----------------------------------------------------------
    # ``/docs`` and ``/openapi.json`` are disabled when true.
    disable_docs: bool = False
    max_text_length: int = 5000
    # Maximum accepted request body (bytes) - guards against abuse.
    max_request_bytes: int = 2 * 1024 * 1024

    # -- Rate limiting (in-process token bucket, per client IP) --------------
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 30
    rate_limit_window_seconds: int = 60
    # Expensive endpoints (LLM/TTS calls) get their own, stricter bucket.
    rate_limit_generate_requests: int = 10
    rate_limit_generate_window_seconds: int = 60

    # -- Upstream calls -----------------------------------------------------
    llm_timeout_seconds: float = 60.0
    tts_timeout_seconds: float = 90.0
    validate_timeout_seconds: float = 20.0
    upstream_max_retries: int = 2
    upstream_backoff_base_seconds: float = 0.5
    upstream_backoff_max_seconds: float = 4.0

    # -- Async task manager -------------------------------------------------
    task_workers: int = 4
    task_queue_size: int = 200
    task_retention_minutes: int = 60

    # -- Fallback -----------------------------------------------------------
    # When the LLM call fails we can still synthesize a canned reply so the
    # user always receives audio; disable to surface the raw failure.
    fallback_enabled: bool = True
    fallback_reply_template: str = (
        "抱歉，大模型服务暂时不可用（{reason}）。这里是您的原始输入：{text}"
    )
    # When the TTS provider fails, emit a silent placeholder WAV so the
    # pipeline result stays structurally valid.
    fallback_tts_enabled: bool = True

    # -- Provider default endpoints -----------------------------------------
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"
    elevenlabs_base_url: str = "https://api.elevenlabs.io/v1"
    azure_speech_base_url: str = "https://{region}.tts.speech.microsoft.com"
    volcengine_tts_base_url: str = "https://openspeech.bytedance.com/api/v1/tts"

    # -- Logging ------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    # Extra substrings whose occurrences must be redacted from logs.
    redact_extra_keys: list[str] = Field(default_factory=list)

    # ----------------------------------------------------------------------
    @field_validator("cors_origins", "redact_extra_keys", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Allow ``T2V_CORS_ORIGINS=a,b`` style comma separated env values."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return value
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def resolve_path(self, raw: str) -> Path:
        """Resolve a possibly relative path against the backend root."""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (BACKEND_ROOT / path).resolve()
        return path

    @property
    def audio_path(self) -> Path:
        return self.resolve_path(self.audio_dir)

    @property
    def media_path(self) -> Path:
        return self.resolve_path(self.media_dir)

    @property
    def cache_dir(self) -> Path:
        """Cache audio lives *beside* regular audio, in its own subtree.

        Keeping it separate (a) makes the "cache is disposable" contract
        explicit — wiping it never touches history files — and (b) mirrors
        the cache/user-data separation Open WebUI adopted after its bundled
        cache grew into user data (open-webui#12040).
        """
        return self.audio_path / "cache"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


settings = get_settings()