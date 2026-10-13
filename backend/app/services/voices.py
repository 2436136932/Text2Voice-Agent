"""Voice cloning: capability declarations, the clone call, and persistence.

Three provider shapes exist in the wild, and the UI must be able to tell them
apart:

1. **Real clone API** (ElevenLabs ``/voices/add``, MiniMax ``/voice_clone``) -
   the user uploads a clip once, the vendor returns a ``voice_id`` that can be
   selected for every later synthesis. ``supports_voice_clone = True``.
2. **Reference clip at synthesis time** (OpenAI-compatible gateways, and our
   ``custom`` adapter) - there is no clone endpoint; the clip is re-sent on
   each call. ``supports_reference_audio = True``.
3. **Neither** - the provider only offers its built-in voices. Cloning must be
   refused with a message that says which provider *to* use instead.

The persistence rule is the same as everywhere else in this codebase: the
``api_key`` is used for exactly one outbound call and is **never** written to
the ``cloned_voices`` table (there is no column for it), nor to the sidecar, nor
to a log line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import TTSAdapter, VoiceCloneRequest, VoiceCloneResult
from app.adapters.registry import registry
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.models import ClonedVoice
from app.services.media import media_store

logger = get_logger(__name__)

#: Shown in ``GET /capabilities`` and in every "unsupported" error message.
CLONE_NOTE = (
    "支持克隆的服务商：ElevenLabs、MiniMax、以及任何实现了克隆接口的自定义端点；"
    "OpenAI 与自定义端点可在合成时直接携带参考音频。"
)
REFERENCE_NOTE = "其余服务商只能使用其内置音色。"


# ---------------------------------------------------------------------------
# Suggestions (shared by the errors below and by generation-time failures)
# ---------------------------------------------------------------------------
def clone_capable_ids() -> list[str]:
    """Provider ids that can actually clone a voice, sorted."""
    return sorted(
        adapter.id for adapter in registry.available_tts() if adapter.supports_voice_clone
    )


def reference_capable_ids() -> list[str]:
    """Provider ids that accept a reference clip at synthesis time, sorted."""
    return sorted(
        adapter.id
        for adapter in registry.available_tts()
        if adapter.supports_reference_audio
    )


def unsupported_clone_error(provider_id: str) -> AppError:
    """Build the friendly ``VOICE_CLONE_UNSUPPORTED`` error for one provider.

    The message names the provider the user actually selected *and* the ones
    that do work, because "不支持" alone leaves the user stuck.
    """
    capable = clone_capable_ids()
    reference = reference_capable_ids()
    pretty = ", ".join(capable) or "（当前环境没有可用的克隆服务商）"
    suggestion = f"请在「系统设置」把 TTS 服务商切换为 {pretty}，再上传参考音频进行克隆。"
    if reference:
        suggestion += (
            f" 也可以改用 {'、'.join(reference)}，"
            "在生成时直接携带参考音频（无需事先克隆）。"
        )
    else:
        suggestion += " 若只想使用内置音色，请去掉参考音频后重试。"
    return AppError(
        ErrorCode.VOICE_CLONE_UNSUPPORTED,
        f"服务商 “{provider_id}” 不支持声音克隆。",
        detail={
            "provider": provider_id,
            "supported_providers": capable,
            "reference_audio_providers": reference,
            "suggestion": suggestion,
        },
    )


def reference_unsupported_error(provider_id: str, model: str = "") -> AppError:
    """Build ``TTS_REFERENCE_UNSUPPORTED`` for a synthesis-time reference clip."""
    capable = clone_capable_ids()
    reference = reference_capable_ids()
    usable = sorted(set(capable) | set(reference))
    pretty = "、".join(usable) or "（当前环境没有可用的服务商）"
    return AppError(
        ErrorCode.TTS_REFERENCE_UNSUPPORTED,
        f"服务商 “{provider_id}” 不支持参考音频，无法用它合成克隆音色。",
        detail={
            "provider": provider_id,
            "model": model,
            "supported_providers": capable,
            "reference_audio_providers": reference,
            "suggestion": (
                f"请改用 {pretty} 进行克隆或参考音频合成；"
                "如果只想使用内置音色，请清空「参考音频」后重试。"
            ),
        },
    )


def media_missing_error(media_id: str) -> AppError:
    """The requested reference upload is gone (purged or never existed)."""
    return AppError(
        ErrorCode.MEDIA_NOT_FOUND,
        f"参考音频不存在或已过期（{media_id[:8]}…）。",
        detail={
            "media_id": media_id,
            "suggestion": "请重新上传参考音频后再试；上传文件默认保留 7 天。",
        },
        http_status=404,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CloneOutcome:
    """Result of one clone request, ready to be serialized by the route."""

    id: int
    voice_id: str
    provider: str
    name: str
    model: str
    created_at: datetime
    duration_ms: int | None


class VoiceService:
    """Create, list and delete cloned voices."""

    # -- create -------------------------------------------------------------
    async def clone(
        self,
        session: AsyncSession,
        *,
        provider: str,
        api_key: str,
        base_url: str | None,
        name: str,
        media_id: str,
        model: str,
        extra: dict[str, Any] | None = None,
    ) -> CloneOutcome:
        """Clone ``media_id`` into a new voice and persist the reference row."""
        adapter = registry.tts(provider)

        if not adapter.supports_voice_clone:
            raise unsupported_clone_error(adapter.id)

        record = self._load_media(media_id)
        audio = media_store.read_bytes(record)

        if not audio:
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                "参考音频为空，无法克隆。",
                detail={
                    "provider": adapter.id,
                    "suggestion": "请重新上传一段 5–30 秒、清晰的单人语音后重试。",
                },
            )

        request = VoiceCloneRequest(
            api_key=api_key,
            base_url=base_url,
            name=name,
            audio=audio,
            audio_filename=record.filename,
            model=model or self.default_clone_model(adapter),
            # Provenance for providers whose clone is stateless: the adapter can
            # encode this into the voice id so synthesis can find the clip again.
            extra={"media_id": record.media_id, **dict(extra or {})},
        )

        logger.info(
            "voice clone started provider=%s name=%s media_id=%s bytes=%s model=%s",
            adapter.id, name, media_id, len(audio), request.model,
        )
        result: VoiceCloneResult = await adapter.clone_voice(request)
        logger.info(
            "voice clone ok provider=%s voice_id=%s", adapter.id, result.voice_id
        )

        row = ClonedVoice(
            voice_id=result.voice_id,
            provider=adapter.id,
            name=name,
            model=result.model or request.model,
            source_media_id=record.media_id,
            source_duration_ms=result.duration_ms
            if result.duration_ms is not None
            else record.duration_ms,
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()

        return self._to_outcome(row)

    @staticmethod
    def default_clone_model(adapter: TTSAdapter) -> str:
        """Pick the model a provider expects for cloning, if any."""
        models: Sequence[dict[str, str]] = adapter.clone_models
        if models:
            return str(models[0].get("id", ""))
        return ""

    @staticmethod
    def _load_media(media_id: str):
        """Load the sidecar, translating "gone" into ``MEDIA_NOT_FOUND``."""
        if not media_id:
            raise media_missing_error(media_id)
        try:
            return media_store.load(media_id)
        except AppError as exc:
            if exc.code is ErrorCode.MEDIA_NOT_FOUND:
                raise media_missing_error(media_id) from exc
            raise

    # -- read ---------------------------------------------------------------
    async def list(self, session: AsyncSession, *, provider: str | None = None) -> list[dict]:
        """All cloned voices, newest first."""
        statement = select(ClonedVoice).order_by(
            ClonedVoice.created_at.desc(), ClonedVoice.id.desc()
        )
        if provider:
            statement = statement.where(ClonedVoice.provider == provider)
        rows = (await session.execute(statement)).scalars().all()
        return [row.to_dict() for row in rows]

    async def by_provider(self, session: AsyncSession) -> dict[str, list[dict]]:
        """Cloned voices grouped by provider id (used by ``GET /providers``).

        ``registry.describe_all()`` stays a pure, static catalog; merging the
        user's own voices in is the caller's job, and this is that lookup.
        """
        grouped: dict[str, list[dict]] = {}
        for item in await self.list(session):
            grouped.setdefault(str(item["provider"]), []).append(item)
        return grouped

    async def delete(self, session: AsyncSession, voice_pk: int) -> None:
        """Delete a local row. The vendor-side voice is left untouched."""
        row = await session.get(ClonedVoice, voice_pk)
        if row is None:
            raise AppError(
                ErrorCode.VOICE_NOT_FOUND,
                f"克隆音色不存在（id={voice_pk}）。",
                detail={
                    "id": voice_pk,
                    "suggestion": "列表可能已刷新，请重新加载「我的音色」后再试。",
                },
                http_status=404,
            )
        logger.info(
            "deleting cloned voice id=%s provider=%s", voice_pk, row.provider
        )
        await session.execute(delete(ClonedVoice).where(ClonedVoice.id == voice_pk))
        await session.flush()

    @staticmethod
    def _to_outcome(row: ClonedVoice) -> CloneOutcome:
        return CloneOutcome(
            id=row.id,
            voice_id=row.voice_id,
            provider=row.provider,
            name=row.name,
            model=row.model,
            created_at=row.created_at or datetime.now(timezone.utc),
            duration_ms=row.source_duration_ms,
        )


voice_service = VoiceService()
