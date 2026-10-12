"""ElevenLabs text-to-speech (``POST /text-to-speech/{voice_id}``)."""

from __future__ import annotations

import time
from urllib.parse import quote

from app.adapters.base import (
    AdapterInfo,
    TTSAdapter,
    TTSRequest,
    TTSResult,
    VoiceInfo,
    request_bytes,
)
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.security import assert_credential

logger = get_logger(__name__)

#: ElevenLabs output formats use ``codec_samplerate_bitrate`` notation.
FORMAT_MAP = {
    "mp3": "mp3_44100_128",
    "mp3_22050": "mp3_22050_32",
    "opus": "opus_48000_128",
    "pcm": "pcm_44100",
    "ulaw": "ulaw_8000",
}


class ElevenLabsAdapter(TTSAdapter):
    id = "elevenlabs"
    name = "ElevenLabs"
    kind = "tts"
    default_voice = "21m00Tcm4TlvDq8ikWAM"  # Rachel
    default_base_url = "https://api.elevenlabs.io/v1"
    api_key_hint = "32 位十六进制字符串"
    docs_url = "https://elevenlabs.io/docs/api-reference/text-to-speech"
    notes = "情感表现力强；音色 ID 可在 ElevenLabs 控制台 Voices 页面复制。"
    supports_speed = False  # speed is expressed via voice_settings
    speed_min = 0.7
    speed_max = 1.2
    formats = ("mp3", "opus", "pcm")
    max_chars = 5000
    voices = (
        VoiceInfo(id="21m00Tcm4TlvDq8ikWAM", name="Rachel", gender="female", language="en", description="美式女声，自然"),
        VoiceInfo(id="EXAVITQu4vr4xnSDxMaL", name="Sarah", gender="female", language="en", description="柔和女声"),
        VoiceInfo(id="ErXwobaYiN019PkySvjV", name="Antoni", gender="male", language="en", description="温暖男声"),
        VoiceInfo(id="TxGEqnHWrfWFTfGW9XjX", name="Josh", gender="male", language="en", description="低沉男声"),
        VoiceInfo(id="onwK4e9ZLuTAKqWW03F9", name="Daniel", gender="male", language="en", description="播报男声"),
        VoiceInfo(id="pNInz6obpgDQGcFmaJgB", name="Adam", gender="male", language="en", description="沉稳男声"),
        VoiceInfo(id="AZnzlk1XvdvUeBnXmlld", name="Domi", gender="female", language="en", description="有力女声"),
    )

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

    def normalize_format(self, audio_format: str | None) -> str:
        """Accept both our short names and ElevenLabs' native notation."""
        fmt = (audio_format or "mp3").lower()
        if fmt in FORMAT_MAP:
            return fmt
        if fmt in FORMAT_MAP.values():
            return fmt
        # Allow raw passthrough for exotic codecs, but reject obviously wrong values.
        if "_" in fmt and fmt.split("_")[0] in {"mp3", "opus", "pcm", "ulaw"}:
            return fmt
        raise AppError(
            ErrorCode.TTS_UNSUPPORTED_FORMAT,
            f"ElevenLabs 不支持音频格式 “{fmt}”，可选：{', '.join(self.formats)}。",
            detail={"provider": self.id, "format": fmt, "supported": list(self.formats)},
        )

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        api_key = assert_credential(request.api_key, kind="tts", provider=self.id)
        self.check_length(request.text)
        voice = request.voice or self.default_voice
        audio_format = self.normalize_format(request.audio_format)
        output_format = FORMAT_MAP.get(audio_format, audio_format)

        model_id = request.model or "eleven_multilingual_v2"
        speed = self.clamp_speed(request.speed)
        payload = {
            "text": request.text,
            "model_id": model_id,
            "voice_settings": {
                "stability": float(request.extra.get("stability", 0.5)),
                "similarity_boost": float(request.extra.get("similarity_boost", 0.75)),
                "style": float(request.extra.get("style", 0.0)),
                "use_speaker_boost": bool(request.extra.get("use_speaker_boost", True)),
                "speed": speed,
            },
        }

        url = (
            f"{self.resolve_base_url(request.base_url)}/text-to-speech/"
            f"{quote(voice, safe='')}?output_format={quote(output_format, safe='')}"
        )
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

        started = time.perf_counter()
        content, content_type, _ = await request_bytes(
            method="POST",
            url=url,
            kind="tts",
            provider=self.id,
            headers=headers,
            json_body=payload,
            timeout=settings.tts_timeout_seconds,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "tts synthesis ok provider=%s voice=%s model=%s bytes=%s latency_ms=%s",
            self.id, voice, model_id, len(content), latency_ms,
        )
        return TTSResult(
            audio=content,
            provider=self.id,
            voice=voice,
            audio_format=audio_format,
            latency_ms=latency_ms,
            content_type=content_type or "audio/mpeg",
        )

    def describe(self) -> dict:
        payload = super().describe()
        payload["models"] = [
            {"id": "eleven_multilingual_v2", "name": "eleven_multilingual_v2 (多语言)"},
            {"id": "eleven_turbo_v2_5", "name": "eleven_turbo_v2_5 (低延迟)"},
            {"id": "eleven_flash_v2_5", "name": "eleven_flash_v2_5 (最快)"},
        ]
        payload["default_model"] = "eleven_multilingual_v2"
        payload["supports_speed"] = True
        return payload