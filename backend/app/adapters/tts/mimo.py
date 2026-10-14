"""Xiaomi MiMo TTS adapter.

MiMo brands its API as "OpenAI compatible", but the *speech* surface is not the
OpenAI ``/audio/speech`` contract at all — it reuses the chat completions route
and carries audio inside the message payload:

.. code-block:: text

    POST https://api.xiaomimimo.com/v1/chat/completions
    api-key: $MIMO_API_KEY            # note: ``api-key``, not ``Authorization``
    {
      "model": "mimo-v2.5-tts",
      "messages": [
        {"role": "user",      "content": "<how to read it>"},
        {"role": "assistant", "content": "<the text to speak>"}
      ],
      "audio": {"format": "mp3", "voice": "冰糖"}
    }

The audio comes back **base64 encoded** inside the JSON:

* non-streaming → ``choices[0].message.audio.data``
* streaming     → ``choices[n].delta.audio.data`` chunks

which is why the generic ``custom`` (OpenAI-shaped) adapter 404s against it.

Model variants
--------------
======================  ==========================================
model                   voice field means
======================  ==========================================
``mimo-v2.5-tts``       a preset voice name (``mimo_default``, 冰糖…)
``mimo-v2.5-tts-voiceclone``  base64 of a 3–10 s mp3/wav reference clip
``mimo-v2.5-tts-voicedesign``  not supported; needs a description
======================  ==========================================
"""

from __future__ import annotations

import base64
import binascii
import time
import uuid
from pathlib import Path

from app.adapters.base import (
    AdapterInfo,
    TTSAdapter,
    TTSRequest,
    TTSResult,
    VoiceCloneRequest,
    VoiceCloneResult,
    VoiceInfo,
    request_json,
)
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.security import assert_credential

logger = get_logger(__name__)

#: Models that can actually synthesize speech.
MODEL_TTS = "mimo-v2.5-tts"
MODEL_VOICE_CLONE = "mimo-v2.5-tts-voiceclone"
MODEL_VOICE_DESIGN = "mimo-v2.5-tts-voicedesign"

#: The first ``user`` message is a *prompt* (an instruction about how to read),
#: while the ``assistant`` message holds the literal text to be spoken. Sending
#: the text as ``user`` still works but produces a conversational reply instead
#: of a faithful reading, so we always emit both.
DEFAULT_STYLE_PROMPT = "请用自然、流畅的语气朗读下面这段文字。"


class MiMoTTSAdapter(TTSAdapter):
    id = "mimo"
    name = "小米 MiMo TTS"
    kind = "tts"
    default_voice = "mimo_default"
    default_base_url = "https://api.xiaomimimo.com/v1"
    api_key_hint = "在 MiMo 开放平台获取（api-key 认证）"
    docs_url = "https://mimo.mi.com/docs/zh-CN/api/audio/tts"
    notes = (
        "小米 MiMo 语音合成（复用 /chat/completions 接口，音频以 base64 返回）。"
        "含预置音色 mimo_default、冰糖、茉莉、苏打、白桦、Mia、Chloe、Milo、Dean。"
    )
    requires_api_key = True

    #: The protocol exposes no speed control, so the UI hides the slider
    #: (``supports_speed``). ``speed_min``/``speed_max`` must still form a valid
    #: ascending range because the adapter contract asserts it, and the request
    #: builder clamps the value into it.
    supports_speed = False
    speed_min = 0.5
    speed_max = 2.0
    formats = ("mp3", "wav", "pcm")
    max_chars = 4000

    #: A cloned voice is sent as base64 audio, so the reference-audio path is
    #: supported natively (see ``app/services/voices.py`` for the one-shot
    #: clone flow, which registers a voice id instead).
    supports_voice_clone = True
    supports_reference_audio = True
    #: Attaching a reference clip to the plain TTS model makes the vendor call
    #: ignore the clip entirely, so the orchestration layer auto-switches to
    #: this model whenever a clip is present.
    clone_model = MODEL_VOICE_CLONE
    #: The inverse guard. The model selection persists between runs, so after
    #: one clone the plain path would keep hitting "reference audio required";
    #: with no clip we fall back to the preset-voice model instead of failing.
    plain_model = MODEL_TTS
    clone_models = (
        {"id": MODEL_VOICE_CLONE, "name": "mimo-v2.5-tts-voiceclone (音色克隆)"},
    )
    clone_hint = (
        "克隆请选择 mimo-v2.5-tts-voiceclone 模型并上传 3–10 秒的 mp3/wav 参考音频；"
        "普通朗读请选择 mimo-v2.5-tts（使用预置音色）。"
    )

    voices = (
        VoiceInfo(id="mimo_default", name="默认 (mimo_default)", gender="neutral", language="zh-CN"),
        VoiceInfo(id="冰糖", name="冰糖 (女)", gender="female", language="zh-CN"),
        VoiceInfo(id="茉莉", name="茉莉 (女)", gender="female", language="zh-CN"),
        VoiceInfo(id="苏打", name="苏打 (男)", gender="male", language="zh-CN"),
        VoiceInfo(id="白桦", name="白桦 (男)", gender="male", language="zh-CN"),
        VoiceInfo(id="Mia", name="Mia (女)", gender="female", language="en-US"),
        VoiceInfo(id="Chloe", name="Chloe (女)", gender="female", language="en-US"),
        VoiceInfo(id="Milo", name="Milo (男)", gender="male", language="en-US"),
        VoiceInfo(id="Dean", name="Dean (男)", gender="male", language="en-US"),
    )

    models = (
        {"id": MODEL_TTS, "name": "mimo-v2.5-tts (预置音色)"},
        {"id": MODEL_VOICE_CLONE, "name": "mimo-v2.5-tts-voiceclone (音色克隆)"},
        {"id": MODEL_VOICE_DESIGN, "name": "mimo-v2.5-tts-voicedesign (音色设计)"},
    )

    #: PCM is returned raw; the rest map onto their own MIME types.
    content_type_map = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "pcm": "audio/L16",
        "pcm16": "audio/L16",
    }

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id,
            name=self.name,
            kind="tts",
            requires_api_key=True,
            api_key_hint=self.api_key_hint,
            docs_url=self.docs_url,
            notes=self.notes,
        )

    def resolve_base_url(self, base_url: str | None) -> str:
        return (base_url or self.default_base_url).rstrip("/")

    def normalize_format(self, audio_format: str | None) -> str:
        fmt = (audio_format or "mp3").lower()
        if fmt not in self.formats:
            raise AppError(
                ErrorCode.TTS_UNSUPPORTED_FORMAT,
                f"MiMo 不支持音频格式 “{fmt}”，可选：{', '.join(self.formats)}。",
                detail={"provider": self.id, "format": fmt, "supported": list(self.formats)},
            )
        return fmt

    def describe(self) -> dict:
        payload = super().describe()
        payload["default_model"] = MODEL_TTS
        payload["models"] = list(self.models)
        return payload

    # ------------------------------------------------------------------
    # Voice cloning
    # ------------------------------------------------------------------
    #: MiMo's reference clip must be 3–10 seconds of mp3 or wav (per its docs).
    CLONE_MIN_MS = 3_000
    CLONE_MAX_MS = 10_000
    CLONE_FORMATS = ("mp3", "wav")
    #: Documented upper bound for the encoded sample.
    CLONE_MAX_BYTES = 10 * 1024 * 1024

    @staticmethod
    def _mime_for(filename: str) -> str:
        extension = (Path(filename).suffix.lstrip(".") or "wav").lower()
        return {
            "wav": "audio/wav",
            "mp3": "audio/mpeg",
        }.get(extension, "audio/wav")

    @classmethod
    def encode_reference_voice(cls, audio: bytes, filename: str) -> str:
        """Wrap a reference clip the way MiMo expects ``audio.voice``.

        The vendor wants a **data URI** (``data:audio/wav;base64,<...>``), not
        bare base64. Sending raw base64 made it fail to decode the sample: the
        request still succeeded, but produced ~1 second of audio that ignored
        the requested text, which is a very confusing failure to debug.
        """
        encoded = base64.b64encode(audio).decode("ascii")
        return f"data:{cls._mime_for(filename)};base64,{encoded}"

    async def clone_voice(self, request: VoiceCloneRequest) -> VoiceCloneResult:
        """Register a reference clip for ``mimo-v2.5-tts-voiceclone``.

        MiMo has no独立 clone endpoint: ``mimo-v2.5-tts-voiceclone`` takes the
        reference audio as base64 in ``audio.voice`` **on every synthesis call**.
        So "cloning" here means validating the clip against MiMo's published
        limits and recording enough metadata for synthesis to reuse it, rather
        than calling a vendor API.

        Validation happens now rather than at first use because the vendor
        rejects out-of-range clips with an opaque error, and the user would have
        no idea whether the problem was the clip, the key, or the model.
        """
        from app.services.audio import estimate_duration_ms

        if not request.audio:
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                "参考音频为空，无法创建音色。",
                detail={
                    "provider": self.id,
                    "suggestion": "请重新上传一段 3–10 秒的清晰单人语音。",
                },
            )

        extension = (Path(request.audio_filename).suffix.lstrip(".") or "wav").lower()
        if extension not in self.CLONE_FORMATS:
            raise AppError(
                ErrorCode.VOICE_CLONE_FAILED,
                f"MiMo 声音克隆只接受 {' / '.join(self.CLONE_FORMATS)} 格式的参考音频，"
                f"当前是 .{extension}。",
                detail={
                    "provider": self.id,
                    "format": extension,
                    "supported": list(self.CLONE_FORMATS),
                    "suggestion": (
                        "请在声音克隆页用「应用裁剪」导出 WAV 片段后再创建音色。"
                    ),
                },
            )

        duration_ms = estimate_duration_ms(request.audio, extension)
        if duration_ms is not None:
            if duration_ms < self.CLONE_MIN_MS:
                raise AppError(
                    ErrorCode.VOICE_CLONE_FAILED,
                    f"参考音频太短（{duration_ms / 1000:.1f} 秒），MiMo 要求至少 3 秒。",
                    detail={
                        "provider": self.id,
                        "duration_ms": duration_ms,
                        "min_ms": self.CLONE_MIN_MS,
                        "suggestion": "请选择更长、包含完整语句的片段（建议 5–10 秒）。",
                    },
                )
            if duration_ms > self.CLONE_MAX_MS:
                raise AppError(
                    ErrorCode.VOICE_CLONE_FAILED,
                    f"参考音频太长（{duration_ms / 1000:.1f} 秒），MiMo 要求不超过 10 秒。",
                    detail={
                        "provider": self.id,
                        "duration_ms": duration_ms,
                        "max_ms": self.CLONE_MAX_MS,
                        "suggestion": (
                            "请用裁剪滑块把片段缩短到 5–10 秒——更长的参考音频并不会"
                            "提升克隆效果。"
                        ),
                    },
                )

        # The id is a deterministic handle: synthesis resolves it back to the
        # stored clip. Encoding the media id keeps the mapping inspectable and
        # avoids a second registry.
        voice_id = f"mimo-clone:{request.extra.get('media_id') or uuid.uuid4().hex}"

        logger.info(
            "mimo voice clone registered name=%s voice_id=%s duration_ms=%s bytes=%s",
            request.name, voice_id, duration_ms, len(request.audio),
        )
        return VoiceCloneResult(
            voice_id=voice_id,
            provider=self.id,
            name=request.name,
            model=MODEL_VOICE_CLONE,
            duration_ms=duration_ms,
            extra={
                "reference_audio_b64": base64.b64encode(request.audio).decode("ascii"),
                "note": "MiMo 在每次合成时都需要参考音频，已在创建时保存。",
            },
        )

    # ------------------------------------------------------------------
    def build_payload(self, request: TTSRequest, model: str, audio_format: str) -> dict:
        """Assemble the chat-completions body MiMo expects."""
        text = request.text

        # The `user` message is a *style instruction*, and its default must
        # depend on the model:
        #   * voiceclone: leave it EMPTY (per MiMo's own example). A style
        #     prompt here competes with the reference clip and the model may
        #     answer the instruction instead of reading the target text.
        #   * otherwise: a neutral "read this naturally" instruction, which is
        #     what the vendor's non-streaming example uses.
        default_style = "" if model == MODEL_VOICE_CLONE else DEFAULT_STYLE_PROMPT
        style = request.extra.get("style_prompt")
        style = default_style if style is None else str(style).strip()

        messages: list[dict[str, str]] = [{"role": "user", "content": style}]

        audio: dict[str, object] = {"format": audio_format}

        if model == MODEL_VOICE_CLONE:
            # The cloned voice *is* the reference audio. MiMo expects a **data
            # URI**, not bare base64: `data:audio/wav;base64,<...>`. Sending the
            # raw base64 made the vendor fail to decode the clip, which showed
            # up as a ~1 second result that ignored the requested text.
            reference = request.extra.get("reference_audio_bytes")
            if not isinstance(reference, (bytes, bytearray)) or not reference:
                raise AppError(
                    ErrorCode.TTS_REFERENCE_UNSUPPORTED,
                    "使用 mimo-v2.5-tts-voiceclone 时必须提供参考音频。",
                    detail={
                        "provider": self.id,
                        "suggestion": (
                            "请在「系统设置 → 声音克隆」上传一段 3–10 秒的 mp3/wav 参考音频，"
                            "或在生成页的克隆模式下选择参考文件。"
                        ),
                    },
                )
            audio["voice"] = self.encode_reference_voice(
                bytes(reference), str(request.extra.get("reference_audio_filename") or "reference.wav")
            )
        else:
            audio["voice"] = request.voice or self.default_voice

        messages.append({"role": "assistant", "content": text})

        payload: dict[str, object] = {
            "model": model,
            "messages": messages,
            "audio": audio,
            "stream": False,
        }
        if model == MODEL_VOICE_DESIGN:
            payload["audio"] = {**audio, "optimize_text_preview": True}
        return payload

    @staticmethod
    def extract_audio(body: object) -> bytes:
        """Pull the base64 audio out of a MiMo chat response."""
        if not isinstance(body, dict):
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "MiMo 返回了非预期的响应格式。",
                detail={"provider": "mimo", "response_type": type(body).__name__},
                retryable=True,
            )

        choices = body.get("choices") or []
        encoded = ""
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            # Non-streaming shape.
            message = choice.get("message")
            if isinstance(message, dict):
                audio = message.get("audio")
                if isinstance(audio, dict) and audio.get("data"):
                    encoded = str(audio["data"])
                    break
            # Streaming chunk shape (we do not stream, but stay tolerant).
            delta = choice.get("delta")
            if isinstance(delta, dict):
                audio = delta.get("audio")
                if isinstance(audio, dict) and audio.get("data"):
                    encoded = str(audio["data"])
                    break

        if not encoded:
            finish = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "MiMo 未返回音频数据。请确认模型选择正确（mimo-v2.5-tts / -voiceclone），"
                "且文本放在 assistant 消息中。",
                detail={
                    "provider": "mimo",
                    "finish_reason": finish,
                    "suggestion": (
                        "若使用 mimo-v2.5-tts-voicedesign，它需要音色描述而非普通朗读，"
                        "请改用 mimo-v2.5-tts。"
                    ),
                },
                retryable=True,
            )

        try:
            return base64.b64decode(encoded)
        except (binascii.Error, ValueError) as exc:
            raise AppError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                "MiMo 返回的音频数据无法解码。",
                detail={"provider": "mimo"},
                retryable=True,
            ) from exc

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        api_key = assert_credential(request.api_key, kind="tts", provider=self.id)
        self.check_length(request.text)
        audio_format = self.normalize_format(request.audio_format)

        model = request.model or MODEL_TTS
        if model not in {MODEL_TTS, MODEL_VOICE_CLONE, MODEL_VOICE_DESIGN}:
            raise AppError(
                ErrorCode.TTS_INVALID_VOICE,
                f"MiMo 不支持模型 “{model}”。",
                detail={
                    "provider": self.id,
                    "supported": [MODEL_TTS, MODEL_VOICE_CLONE, MODEL_VOICE_DESIGN],
                    "suggestion": f"请在「系统设置」把 TTS 模型改为 {MODEL_TTS}（普通朗读）。",
                },
            )

        url = f"{self.resolve_base_url(request.base_url)}/chat/completions"
        # MiMo uses ``api-key`` rather than the OpenAI ``Authorization`` header.
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        payload = self.build_payload(request, model, audio_format)

        started = time.perf_counter()
        body, _ = await request_json(
            method="POST",
            url=url,
            kind="tts",
            provider=self.id,
            headers=headers,
            json_body=payload,
            timeout=settings.tts_timeout_seconds,
        )
        audio = self.extract_audio(body)
        latency_ms = int((time.perf_counter() - started) * 1000)

        logger.info(
            "tts synthesis ok provider=%s model=%s voice=%s bytes=%s latency_ms=%s",
            self.id, model, payload["audio"].get("voice", "")[:24] if isinstance(payload["audio"], dict) else "",
            len(audio), latency_ms,
        )
        return TTSResult(
            audio=audio,
            provider=self.id,
            voice=str(request.voice or self.default_voice),
            audio_format=audio_format,
            latency_ms=latency_ms,
            content_type=self.content_type_for(audio_format),
        )

    async def validate(self, request: TTSRequest):  # noqa: ANN201 - matches base signature
        """Probe with a single short character to keep the call cheap."""
        import time as _time

        from app.adapters.base import ValidationResult

        started = _time.perf_counter()
        probe = TTSRequest(
            text="好",
            api_key=request.api_key,
            voice=request.voice or self.default_voice,
            model=request.model or MODEL_TTS,
            base_url=request.base_url,
            audio_format="mp3",
            extra=request.extra,
        )
        try:
            result = await self.synthesize(probe)
        except AppError as exc:
            return ValidationResult(
                valid=False,
                message=exc.message,
                latency_ms=int((_time.perf_counter() - started) * 1000),
                provider=self.id,
                kind="tts",
                detail={"code": exc.code.value, **exc.detail},
            )
        return ValidationResult(
            valid=True,
            message=f"连接成功，已合成 {len(result.audio)} 字节音频。",
            latency_ms=int((_time.perf_counter() - started) * 1000),
            provider=self.id,
            kind="tts",
            detail={"model": probe.model, "voice": result.voice, "bytes": len(result.audio)},
        )