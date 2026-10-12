"""OpenAI text-to-speech (``POST /audio/speech``).

Returns raw audio bytes (not JSON), so this adapter uses ``request_bytes``.
"""

from __future__ import annotations

import time

from app.adapters.base import (
    AdapterInfo,
    TTSAdapter,
    TTSRequest,
    TTSResult,
    VoiceInfo,
    request_bytes,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import assert_credential

logger = get_logger(__name__)


class OpenAITTSAdapter(TTSAdapter):
    id = "openai"
    name = "OpenAI TTS"
    kind = "tts"
    default_voice = "alloy"
    default_base_url = "https://api.openai.com/v1"
    api_key_hint = "sk-..."
    docs_url = "https://platform.openai.com/docs/guides/text-to-speech"
    notes = "gpt-4o-mini-tts / tts-1 / tts-1-hd，音色自然，支持多语言。"
    supports_speed = True
    speed_min = 0.25
    speed_max = 4.0
    formats = ("mp3", "opus", "aac", "flac", "wav", "pcm")
    max_chars = 4096
    voices = (
        VoiceInfo(id="alloy", name="Alloy", gender="neutral", language="multi", description="中性、平衡"),
        VoiceInfo(id="echo", name="Echo", gender="male", language="multi", description="沉稳男声"),
        VoiceInfo(id="fable", name="Fable", gender="neutral", language="multi", description="叙事感强"),
        VoiceInfo(id="onyx", name="Onyx", gender="male", language="multi", description="低沉男声"),
        VoiceInfo(id="nova", name="Nova", gender="female", language="multi", description="明亮女声"),
        VoiceInfo(id="shimmer", name="Shimmer", gender="female", language="multi", description="柔和女声"),
        VoiceInfo(id="coral", name="Coral", gender="female", language="multi", description="温暖女声"),
        VoiceInfo(id="sage", name="Sage", gender="neutral", language="multi", description="知性中性"),
    )
    models = (
        {"id": "gpt-4o-mini-tts", "name": "gpt-4o-mini-tts (推荐)"},
        {"id": "tts-1", "name": "tts-1 (低延迟)"},
        {"id": "tts-1-hd", "name": "tts-1-hd (高音质)"},
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

    def describe(self) -> dict:
        payload = super().describe()
        payload["models"] = list(self.models)
        payload["default_model"] = "gpt-4o-mini-tts"
        return payload

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        api_key = assert_credential(request.api_key, kind="tts", provider=self.id)
        self.check_length(request.text)
        audio_format = self.normalize_format(request.audio_format)
        voice = request.voice or self.default_voice
        model = request.model or "gpt-4o-mini-tts"

        payload = {
            "model": model,
            "input": request.text,
            "voice": voice,
            "response_format": audio_format,
            "speed": self.clamp_speed(request.speed),
        }
        # Optional style instructions supported by gpt-4o-mini-tts.
        instructions = request.extra.get("instructions")
        if instructions and model.startswith("gpt-4o"):
            payload["instructions"] = str(instructions)[:1000]

        url = f"{self.resolve_base_url(request.base_url)}/audio/speech"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": self.content_type_for(audio_format),
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
            self.id, voice, model, len(content), latency_ms,
        )
        return TTSResult(
            audio=content,
            provider=self.id,
            voice=voice,
            audio_format=audio_format,
            latency_ms=latency_ms,
            content_type=content_type or self.content_type_for(audio_format),
        )