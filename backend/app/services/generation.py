"""The generation pipeline: text -> LLM reply -> speech -> persisted record.

Flow
----
1. **LLM stage** - resolve the adapter, call it through
   :func:`app.core.retry.call_with_retry`, record latency and token usage.
   On failure, if ``options.fallback_enabled`` (and the platform default),
   synthesize a *degraded* reply from the template so the user still receives
   audio, and mark the record ``degraded=True`` with a reason.
2. **TTS stage** - resolve the TTS adapter, synthesize the reply. On failure
   with ``fallback_tts_enabled``, emit a silent WAV placeholder so the result
   stays structurally valid (playable, downloadable, correct duration).
3. **Persist stage** - write the audio to disk, insert the history row, and
   return the response DTO.

Progress is reported through an optional ``on_progress(stage, percent,
message)`` callback so both the synchronous route and the async task manager
can surface the same stages to the UI.

Credential lifetime
-------------------
Keys arrive in :class:`GenerateRequest`, are used to build
:class:`LLMRequest` / :class:`TTSRequest`, and go out of scope when this method
returns. Nothing is written to the database except the truncated SHA-256
fingerprint produced by ``GenerateRequest.key_fingerprints()``.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import LLMRequest, TTSRequest
from app.adapters.registry import registry
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.retry import call_with_retry, llm_budget, tts_budget
from app.db.models import GenerationRecord
from app.schemas.generation import (
    GenerateRequest,
    GenerateResponse,
    LLMResultSchema,
    TTSResultSchema,
    UsageSchema,
)
from app.services.audio import audio_store, estimate_duration_ms
from app.services.media import media_store
from app.services.metrics import metrics
from app.services.providers import annotate_tts_error
from app.services.voices import media_missing_error, reference_unsupported_error

logger = get_logger(__name__)

ProgressCallback = Callable[[str, int, str], None]

#: Prefix for voice ids that stand for a stored reference clip rather than a
#: vendor-side voice. Providers whose cloning is stateless (MiMo sends the
#: reference on every call) mint ids in this form, and synthesis resolves them
#: back to the clip.
_CLONE_VOICE_PREFIX = "mimo-clone:"

#: Failures that are the *caller's* fault and must never be masked by the
#: degradation path - retrying or faking a result would be misleading.
NON_DEGRADABLE_CODES = frozenset(
    {
        ErrorCode.MISSING_CREDENTIAL,
        ErrorCode.INVALID_PROVIDER,
        ErrorCode.INVALID_PARAMETER,
        ErrorCode.VALIDATION_ERROR,
        ErrorCode.TEXT_EMPTY,
        ErrorCode.TEXT_TOO_LONG,
        ErrorCode.LLM_UNSUPPORTED_PROVIDER,
        ErrorCode.TTS_UNSUPPORTED_PROVIDER,
        ErrorCode.TTS_UNSUPPORTED_FORMAT,
        ErrorCode.TTS_INVALID_VOICE,
        ErrorCode.TTS_TEXT_TOO_LONG,
    }
)


def _is_degradable(error: AppError) -> bool:
    """True when a vendor failure may be replaced by a fallback result."""
    return error.code not in NON_DEGRADABLE_CODES


@dataclass(slots=True)
class StageTiming:
    stage: str
    duration_ms: int


def _noop_progress(stage: str, percent: int, message: str) -> None:  # noqa: ARG001
    return None


class GenerationService:
    """Stateless orchestrator for one generation."""

    # ------------------------------------------------------------------
    # Stage 1 - LLM
    # ------------------------------------------------------------------
    async def _run_llm(
        self,
        request: GenerateRequest,
        *,
        on_progress: ProgressCallback,
    ) -> tuple[str, LLMResultSchema, bool, str | None]:
        """Return ``(reply_text, result, degraded, degraded_reason)``."""
        adapter = registry.llm(request.llm.provider)
        api_key = request.llm.api_key.get_secret_value() if request.llm.api_key else ""

        if request.options.reuse_reply_text:
            # "Re-synthesize only": keep the previous reply verbatim.
            reply = request.options.reuse_reply_text
            on_progress("llm", 40, "复用上一条回复文本，跳过 LLM 调用")
            return (
                reply,
                LLMResultSchema(
                    provider=request.llm.provider,
                    model=request.llm.model or adapter.default_model,
                    latency_ms=0,
                    usage=UsageSchema(),
                    finish_reason="reused",
                ),
                False,
                None,
            )

        on_progress("llm", 15, f"正在调用 {adapter.name} 生成回复…")

        llm_request = LLMRequest(
            prompt=request.text,
            api_key=api_key,
            model=request.llm.model or adapter.default_model,
            base_url=request.llm.base_url,
            system_prompt=request.llm.system_prompt,
            temperature=request.llm.temperature,
            max_tokens=request.llm.max_tokens,
            extra=request.llm.extra,
        )

        if llm_budget.is_open(adapter.id):
            error = AppError(
                ErrorCode.LLM_UPSTREAM_ERROR,
                f"{adapter.name} 近期连续失败，已暂时熔断 {llm_budget.retry_after(adapter.id):.0f} 秒。",
                detail={"provider": adapter.id},
                retryable=False,
            )
            return await self._llm_fallback(request, error, on_progress)

        try:
            result = await call_with_retry(
                lambda attempt: adapter.complete(llm_request),
                provider=adapter.id,
                kind="llm",
            )
        except AppError as exc:
            metrics.record_provider_call("llm", adapter.id, ok=False)
            llm_budget.record_failure(adapter.id)
            logger.error(
                "llm stage failed provider=%s code=%s message=%s",
                adapter.id, exc.code.value, exc.message,
            )
            if not request.options.fallback_enabled or not settings.fallback_enabled:
                raise
            if not _is_degradable(exc):
                raise
            return await self._llm_fallback(request, exc, on_progress)

        metrics.record_provider_call("llm", adapter.id, ok=True)
        llm_budget.record_success(adapter.id)
        on_progress("llm", 40, "大模型回复生成完成")

        usage_raw = result.usage or {}
        usage = UsageSchema(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            total_tokens=int(
                usage_raw.get("total_tokens")
                or sum(int(usage_raw.get(k, 0)) for k in ("prompt_tokens", "completion_tokens"))
            ),
        )
        return (
            result.text,
            LLMResultSchema(
                provider=result.provider,
                model=result.model,
                latency_ms=result.latency_ms,
                usage=usage,
                finish_reason=result.finish_reason,
            ),
            False,
            None,
        )

    async def _llm_fallback(
        self, request: GenerateRequest, error: AppError, on_progress: ProgressCallback
    ) -> tuple[str, LLMResultSchema, bool, str | None]:
        """Produce a canned reply so the TTS leg can still run."""
        reason = error.message
        reply = settings.fallback_reply_template.format(reason=reason, text=request.text)
        on_progress("llm", 40, "大模型调用失败，已启用降级回复")
        logger.warning(
            "llm degraded provider=%s code=%s", request.llm.provider, error.code.value
        )
        return (
            reply,
            LLMResultSchema(
                provider=request.llm.provider,
                model=request.llm.model or "",
                latency_ms=0,
                usage=UsageSchema(),
                finish_reason="degraded",
            ),
            True,
            f"LLM({request.llm.provider}) 调用失败：{reason}",
        )

    # ------------------------------------------------------------------
    # Stage 2 - TTS
    # ------------------------------------------------------------------
    @staticmethod
    def _silent_wav(duration_ms: int = 1200, sample_rate: int = 16_000) -> bytes:
        """Build a silent mono 16-bit WAV used as the TTS fallback."""
        import struct

        frames = int(sample_rate * duration_ms / 1000)
        data = b"\x00\x00" * frames
        byte_rate = sample_rate * 2
        header = b"".join(
            [
                b"RIFF",
                struct.pack("<I", 36 + len(data)),
                b"WAVE",
                b"fmt ",
                struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16),
                b"data",
                struct.pack("<I", len(data)),
            ]
        )
        return header + data

    async def _run_tts(
        self,
        request: GenerateRequest,
        reply_text: str,
        *,
        on_progress: ProgressCallback,
    ) -> tuple[bytes, TTSResultSchema, str, bool, str | None]:
        """Return ``(audio, result, audio_format, degraded, degraded_reason)``."""
        adapter = registry.tts(request.tts.provider)
        api_key = request.tts.api_key.get_secret_value() if request.tts.api_key else ""
        audio_format = adapter.normalize_format(request.tts.format)
        voice = request.tts.voice or adapter.default_voice

        on_progress("tts", 60, f"正在调用 {adapter.name} 合成语音…")

        extra = dict(request.tts.extra)

        # A cloned voice id can *be* a reference to a stored clip. MiMo works
        # this way: `mimo-v2.5-tts-voiceclone` receives the reference audio on
        # every call, so selecting the cloned voice must resolve back to the
        # clip that created it. Without this the request went out with no
        # reference and the vendor rejected it.
        resolved_media_id = request.options.reference_media_id
        if not resolved_media_id and voice.startswith(_CLONE_VOICE_PREFIX):
            resolved_media_id = voice[len(_CLONE_VOICE_PREFIX) :].strip() or None
            logger.info(
                "resolved cloned voice to reference clip voice=%s media_id=%s",
                voice, resolved_media_id,
            )

        if resolved_media_id:
            # Resolve the clip first: if the upload is gone, that is the
            # actionable problem ("上传已过期"), and reporting a capability
            # mismatch instead would send the user down the wrong path.
            try:
                record = media_store.load(resolved_media_id)
                reference_bytes = media_store.read_bytes(record)
            except AppError as exc:
                if exc.code is ErrorCode.MEDIA_NOT_FOUND:
                    raise media_missing_error(resolved_media_id) from exc
                raise

            # Then validate the capability, *before* spending a TTS call: a
            # provider that can neither clone nor consume a reference clip
            # would silently ignore the clip and produce the wrong voice, which
            # is worse than an error.
            if not (adapter.supports_reference_audio or adapter.supports_voice_clone):
                raise reference_unsupported_error(adapter.id, request.tts.model or "")

            extra["reference_audio_media_id"] = record.media_id
            extra["reference_audio_bytes"] = reference_bytes
            extra["reference_audio_filename"] = record.filename
            extra["reference_audio_duration_ms"] = record.duration_ms
            logger.info(
                "tts reference audio resolved provider=%s media_id=%s bytes=%s",
                adapter.id,
                resolved_media_id,
                len(reference_bytes),
            )

            # A reference clip makes the request a *cloning* request. If the
            # configured model cannot consume one (e.g. the plain MiMo TTS
            # model), the adapter would silently drop the clip and speak in a
            # preset voice — the worst failure mode, because nothing errors.
            # Upgrade to the provider's clone model instead, so "upload a
            # reference + hit generate" always clones.
            clone_model = getattr(adapter, "clone_model", "")
            if clone_model and request.tts.model != clone_model:
                logger.info(
                    "upgrading tts model for reference audio provider=%s from=%s to=%s",
                    adapter.id, request.tts.model, clone_model,
                )
                request.tts.model = clone_model

        tts_request = TTSRequest(
            text=reply_text,
            api_key=api_key,
            voice=voice,
            model=request.tts.model or "",
            base_url=request.tts.base_url,
            speed=adapter.clamp_speed(request.tts.speed),
            audio_format=audio_format,
            language=request.tts.language,
            extra=extra,
        )

        if tts_budget.is_open(adapter.id):
            error = AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                f"{adapter.name} 近期连续失败，已暂时熔断 {tts_budget.retry_after(adapter.id):.0f} 秒。",
                detail={"provider": adapter.id},
                retryable=False,
            )
            return self._tts_fallback(adapter.id, error, audio_format, voice, on_progress)

        try:
            result = await call_with_retry(
                lambda attempt: adapter.synthesize(tts_request),
                provider=adapter.id,
                kind="tts",
            )
        except AppError as exc:
            metrics.record_provider_call("tts", adapter.id, ok=False)
            tts_budget.record_failure(adapter.id)
            # Enrich model-mismatch failures ("model not found") with advice on
            # which model to pick, so the user is not left with a raw 404.
            exc = annotate_tts_error(exc, adapter.id)
            logger.error(
                "tts stage failed provider=%s code=%s message=%s",
                adapter.id, exc.code.value, exc.message,
            )
            if not request.options.fallback_enabled or not settings.fallback_tts_enabled:
                raise
            if not _is_degradable(exc):
                raise
            return self._tts_fallback(adapter.id, exc, audio_format, voice, on_progress)

        metrics.record_provider_call("tts", adapter.id, ok=True)
        tts_budget.record_success(adapter.id)
        on_progress("tts", 85, "语音合成完成")

        return (
            result.audio,
            TTSResultSchema(
                provider=result.provider,
                voice=result.voice,
                latency_ms=result.latency_ms,
                format=result.audio_format,
                bytes=len(result.audio),
                used_reference_audio=bool(extra.get("reference_audio_bytes")),
            ),
            result.audio_format,
            False,
            None,
        )

    def _tts_fallback(
        self,
        provider: str,
        error: AppError,
        audio_format: str,
        voice: str,
        on_progress: ProgressCallback,
    ) -> tuple[bytes, TTSResultSchema, str, bool, str | None]:
        """Emit a silent placeholder so the pipeline result stays valid."""
        audio = self._silent_wav()
        on_progress("tts", 85, "语音合成失败，已生成占位音频")
        logger.warning("tts degraded provider=%s code=%s", provider, error.code.value)
        return (
            audio,
            TTSResultSchema(
                provider=provider,
                voice=voice,
                latency_ms=0,
                format="wav",
                bytes=len(audio),
            ),
            "wav",
            True,
            f"TTS({provider}) 调用失败：{error.message}",
        )

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    async def generate(
        self,
        session: AsyncSession,
        request: GenerateRequest,
        *,
        on_progress: ProgressCallback | None = None,
        request_id: str | None = None,
    ) -> GenerateResponse:
        """Run the full pipeline and persist the result."""
        progress = on_progress or _noop_progress
        started = time.perf_counter()
        metrics.incr("generations_total")

        logger.info(
            "generation started llm=%s/%s tts=%s/%s text_chars=%s request_id=%s",
            request.llm.provider,
            request.llm.model or "(default)",
            request.tts.provider,
            request.tts.voice or "(default)",
            len(request.text),
            request_id,
        )

        degraded_reasons: list[str] = []

        reply_text, llm_result, llm_degraded, llm_reason = await self._run_llm(
            request, on_progress=progress
        )
        if llm_degraded and llm_reason:
            degraded_reasons.append(llm_reason)

        audio, tts_result, audio_format, tts_degraded, tts_reason = await self._run_tts(
            request, reply_text, on_progress=progress
        )
        if tts_degraded and tts_reason:
            degraded_reasons.append(tts_reason)

        progress("persist", 92, "正在保存结果…")

        duration_ms = estimate_duration_ms(audio, audio_format)
        total_latency_ms = int((time.perf_counter() - started) * 1000)
        degraded = bool(degraded_reasons)

        record = GenerationRecord(
            input_text=request.text,
            reply_text=reply_text,
            llm_provider=request.llm.provider,
            llm_model=llm_result.model,
            tts_provider=request.tts.provider,
            tts_voice=tts_result.voice,
            tts_model=request.tts.model or "",
            key_fingerprint=(request.key_fingerprints().get("llm") or "")[:64],
            audio_format=audio_format,
            audio_bytes=len(audio),
            duration_ms=duration_ms,
            llm_latency_ms=llm_result.latency_ms,
            tts_latency_ms=tts_result.latency_ms,
            total_latency_ms=total_latency_ms,
            prompt_tokens=llm_result.usage.prompt_tokens,
            completion_tokens=llm_result.usage.completion_tokens,
            total_tokens=llm_result.usage.total_tokens,
            status="succeeded",
            degraded=degraded,
            degraded_reason="；".join(degraded_reasons) if degraded_reasons else None,
            request_id=request_id,
            created_at=datetime.now(timezone.utc),
        )

        if request.options.auto_save:
            session.add(record)
            await session.flush()
            record.audio_path = audio_store.save(
                record_id=record.id,
                provider=request.tts.provider,
                voice=tts_result.voice,
                audio_format=audio_format,
                data=audio,
            )
            await session.flush()
            record_id = record.id
        else:
            # Still allocate an id so the audio route works for this session,
            # but keep the row out of the history.
            record_id = int(time.time() * 1000) % 2_000_000_000
            record.id = record_id
            record.audio_path = audio_store.save(
                record_id=record_id,
                provider=request.tts.provider,
                voice=tts_result.voice,
                audio_format=audio_format,
                data=audio,
            )

        progress("done", 100, "生成完成")
        metrics.incr("generations_succeeded")
        if degraded:
            metrics.incr("generations_degraded")

        logger.info(
            "generation finished record_id=%s total_latency_ms=%s audio_bytes=%s duration_ms=%s degraded=%s",
            record_id, total_latency_ms, len(audio), duration_ms, degraded,
        )

        return GenerateResponse(
            record_id=record_id,
            reply_text=reply_text,
            input_text=request.text,
            audio_url=f"/api/v1/audio/{record_id}",
            audio_format=audio_format,
            audio_bytes=len(audio),
            audio_base64=(
                base64.b64encode(audio).decode("ascii")
                if request.options.include_audio_base64
                else None
            ),
            duration_ms=duration_ms,
            degraded=degraded,
            degraded_reason="；".join(degraded_reasons) if degraded_reasons else None,
            llm=llm_result,
            tts=tts_result,
            total_latency_ms=total_latency_ms,
            created_at=record.created_at or datetime.now(timezone.utc),
        )


generation_service = GenerationService()