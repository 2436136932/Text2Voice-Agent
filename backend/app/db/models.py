"""ORM models.

Security note
-------------
**No table has a column for an API key.** The ``key_fingerprint`` column on
:class:`GenerationRecord` stores a truncated SHA-256 digest so the UI can show
"generated with a key ending in …cdef" for audit purposes; the digest is not
reversible and the raw key exists only inside the request that carried it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GenerationRecord(Base):
    """One completed (or failed) text -> reply -> speech generation."""

    __tablename__ = "generation_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # -- content ------------------------------------------------------------
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # -- providers (never credentials) --------------------------------------
    llm_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    llm_model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tts_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    tts_voice: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tts_model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # -- audio --------------------------------------------------------------
    audio_format: Mapped[str] = mapped_column(String(16), nullable=False, default="mp3")
    audio_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audio_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- observability ------------------------------------------------------
    llm_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tts_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # -- outcome ------------------------------------------------------------
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="succeeded")
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    degraded_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_records_created_at", "created_at"),
        Index("ix_records_status", "status"),
        Index("ix_records_provider", "llm_provider", "tts_provider"),
    )

    @property
    def audio_url(self) -> str:
        return f"/api/v1/audio/{self.id}"

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        """Serialize for the API (used by the history endpoints)."""
        payload: dict[str, Any] = {
            "id": self.id,
            "input_text": self.input_text if include_text else _truncate(self.input_text, 120),
            "reply_text": self.reply_text if include_text else _truncate(self.reply_text, 120),
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "tts_provider": self.tts_provider,
            "tts_voice": self.tts_voice,
            "tts_model": self.tts_model,
            "audio_format": self.audio_format,
            "audio_bytes": self.audio_bytes,
            "duration_ms": self.duration_ms,
            "llm_latency_ms": self.llm_latency_ms,
            "tts_latency_ms": self.tts_latency_ms,
            "total_latency_ms": self.total_latency_ms,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            },
            "status": self.status,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "audio_url": self.audio_url if self.status == "succeeded" else None,
        }
        return payload


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


class TaskModel(Base):
    """Durable state of an asynchronous generation task.

    ``request_payload`` holds the submitted request **with credentials
    stripped** (:meth:`app.schemas.generation.GenerateRequest.safe_dump`), so
    a task can be inspected and audited without ever storing a key.
    """

    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    stage: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("ix_tasks_status_created", "status", "created_at"),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "record_id": self.record_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


class SettingModel(Base):
    """Single-row key/value store for user preferences.

    Deliberately a single row with a JSON blob: preferences are read and
    written as a whole, and this keeps migrations trivial.
    """

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    DEFAULT_SETTINGS: dict[str, Any] = {
        "default_llm_provider": "openai",
        "default_llm_model": "gpt-4o-mini",
        "default_tts_provider": "openai",
        "default_tts_voice": "alloy",
        "default_speed": 1.0,
        "default_system_prompt": "你是一个乐于助人的中文助手，回答简洁、口语化，适合朗读。",
        "auto_save_history": True,
        "auto_save_credentials": False,
        "request_timeout_seconds": 120,
        "temperature": 0.7,
        "max_tokens": 1024,
    }

    @classmethod
    async def get_or_create(cls, session: AsyncSession) -> "SettingModel":
        row = await session.get(cls, 1)
        if row is None:
            row = cls(id=1, data=dict(cls.DEFAULT_SETTINGS))
            session.add(row)
            await session.flush()
        else:
            # Backfill keys added in later versions.
            merged = {**cls.DEFAULT_SETTINGS, **(row.data or {})}
            if merged != (row.data or {}):
                row.data = merged
                await session.flush()
        return row

    @classmethod
    async def load(cls, session: AsyncSession) -> dict[str, Any]:
        row = await session.get(cls, 1)
        if row is None:
            row = await cls.get_or_create(session)
        return {**cls.DEFAULT_SETTINGS, **(row.data or {})}

    @classmethod
    async def save(cls, session: AsyncSession, values: dict[str, Any]) -> dict[str, Any]:
        row = await cls.get_or_create(session)
        merged = {**cls.DEFAULT_SETTINGS, **(row.data or {}), **values}
        row.data = merged
        await session.flush()
        return merged


# Backwards-friendly aliases used by ``app.db.__init__``.
AppSetting = SettingModel

__all__ = ["GenerationRecord", "TaskModel", "SettingModel", "AppSetting", "utcnow"]