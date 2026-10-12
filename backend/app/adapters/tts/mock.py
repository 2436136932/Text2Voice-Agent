"""Offline ``mock`` adapters.

These exist so that the platform is **demonstrable without any credentials**:
start the backend, open the UI, and you get real audio, real history rows and
real latency numbers, with zero outbound calls.

* :class:`MockLLMAdapter` produces a deterministic, readable reply that echoes
  the prompt, including token counts and a simulated latency.
* :class:`MockTTSAdapter` synthesizes a genuine **WAV** file (RIFF/PCM) whose
  tone and length are derived from the text, so the browser audio player, the
  duration estimation and the download button all behave exactly as they do
  with a paid vendor.

They are also the reference implementation for ``docs/EXTENDING_TTS.md``:
about 120 lines, no I/O, fully typed.
"""

from __future__ import annotations

import hashlib
import math
import struct
import time

from app.adapters.base import (
    AdapterInfo,
    LLMAdapter,
    LLMRequest,
    LLMResult,
    TTSAdapter,
    TTSRequest,
    TTSResult,
    VoiceInfo,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2  # 16-bit mono


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
class MockLLMAdapter(LLMAdapter):
    id = "mock"
    name = "本地模拟 LLM (无需密钥)"
    kind = "llm"
    default_model = "mock-echo-v1"
    api_key_hint = "(无需密钥)"
    docs_url = ""
    notes = "离线演示用：不调用任何外部服务，直接生成一段可朗读的回复。"
    requires_api_key = False
    models = ({"id": "mock-echo-v1", "name": "mock-echo-v1 (离线演示)"},)

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id,
            name=self.name,
            kind="llm",
            requires_api_key=False,
            api_key_hint=self.api_key_hint,
            notes=self.notes,
        )

    def resolve_base_url(self, base_url: str | None) -> str:
        return base_url or "mock://local"

    async def complete(self, request: LLMRequest) -> LLMResult:
        started = time.perf_counter()
        # Deterministic pseudo latency so tests are stable and the UI still
        # shows a believable progress animation.
        seed = int(hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()[:8], 16)
        delay = 0.05 + (seed % 100) / 1000.0
        import asyncio

        await asyncio.sleep(min(delay, 0.2))

        prompt = request.prompt.strip()
        preview = prompt if len(prompt) <= 200 else f"{prompt[:200]}…"
        text = (
            "【离线模拟回复】\n"
            f"我收到了你的输入：{preview}\n\n"
            "当前使用本地模拟大模型（未配置任何真实 LLM 密钥），"
            "因此这条回复是确定性生成的占位内容。"
            "在“系统设置”或生成页中配置 OpenAI / Anthropic / 通义千问等服务的密钥后，"
            "这里会替换为真实大模型的回答；语音合成链路完全一致，可以直接试听。"
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        prompt_tokens = max(1, len(prompt) // 2)
        completion_tokens = max(1, len(text) // 2)
        logger.info("mock llm completion latency_ms=%s chars=%s", latency_ms, len(text))
        return LLMResult(
            text=text,
            provider=self.id,
            model=request.model or self.default_model,
            latency_ms=latency_ms,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            finish_reason="stop",
        )


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
class MockTTSAdapter(TTSAdapter):
    id = "mock"
    name = "本地模拟 TTS (无需密钥)"
    kind = "tts"
    default_voice = "mock-neutral"
    api_key_hint = "(无需密钥)"
    docs_url = ""
    notes = "离线演示用：本地生成一段带旋律的 WAV 音频，用于验证播放、下载与时长统计。"
    supports_speed = True
    speed_min = 0.5
    speed_max = 2.0
    #: WAV only. ``base.TTSAdapter.normalize_format`` rejects anything else, and
    #: the pipeline writes the file using this declared format, so the metadata
    #: must match what ``synthesize`` actually renders.
    formats = ("wav",)
    max_chars = 5000
    voices = (
        VoiceInfo(id="mock-neutral", name="模拟中性音", gender="neutral", language="zh-CN", description="440Hz 基准音"),
        VoiceInfo(id="mock-low", name="模拟低音", gender="male", language="zh-CN", description="220Hz 低音"),
        VoiceInfo(id="mock-high", name="模拟高音", gender="female", language="zh-CN", description="660Hz 高音"),
    )
    _BASE_FREQ = {"mock-neutral": 440.0, "mock-low": 220.0, "mock-high": 660.0}

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id,
            name=self.name,
            kind="tts",
            requires_api_key=False,
            api_key_hint=self.api_key_hint,
            notes=self.notes,
        )

    def resolve_base_url(self, base_url: str | None) -> str:
        return base_url or "mock://local"

    @staticmethod
    def _wav_header(data_bytes: int, sample_rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
        byte_rate = sample_rate * channels * BYTES_PER_SAMPLE
        block_align = channels * BYTES_PER_SAMPLE
        return b"".join(
            [
                b"RIFF",
                struct.pack("<I", 36 + data_bytes),
                b"WAVE",
                b"fmt ",
                struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16),
                b"data",
                struct.pack("<I", data_bytes),
            ]
        )

    def _render(self, text: str, voice: str, speed: float) -> tuple[bytes, int]:
        """Render a deterministic tone sequence derived from the text."""
        base_freq = self._BASE_FREQ.get(voice, 440.0)
        digest = hashlib.sha256(text.encode("utf-8")).digest()

        # ~0.09s per character, clamped so a 5000 char text stays affordable.
        duration_s = max(0.8, min(60.0, len(text) * 0.09)) / max(0.25, speed)
        total_samples = int(SAMPLE_RATE * duration_s)

        frames = bytearray()
        # Six-note motif derived from the text hash -> stable across runs.
        motif = [1.0 + (digest[i] % 5) / 8.0 for i in range(6)]
        note_len = max(1, total_samples // len(motif))

        for index in range(total_samples):
            note = motif[min(len(motif) - 1, index // note_len)]
            freq = base_freq * note
            # Gentle tremolo + fade in/out to avoid clicks at the boundaries.
            envelope = 1.0
            fade = int(SAMPLE_RATE * 0.02)
            if index < fade:
                envelope = index / fade
            elif index > total_samples - fade:
                envelope = max(0.0, (total_samples - index) / fade)
            tremolo = 0.85 + 0.15 * math.sin(2 * math.pi * 4.0 * index / SAMPLE_RATE)
            value = math.sin(2 * math.pi * freq * index / SAMPLE_RATE) * envelope * tremolo
            frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 12_000))

        audio = self._wav_header(len(frames)) + bytes(frames)
        duration_ms = int(duration_s * 1000)
        return audio, duration_ms

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        started = time.perf_counter()
        voice = request.voice or self.default_voice
        audio_format = self.normalize_format(request.audio_format)
        speed = self.clamp_speed(request.speed)
        audio, duration_ms = self._render(request.text, voice, speed)
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "mock tts synthesis voice=%s bytes=%s duration_ms=%s latency_ms=%s",
            voice, len(audio), duration_ms, latency_ms,
        )
        return TTSResult(
            audio=audio,
            provider=self.id,
            voice=voice,
            audio_format=audio_format,
            latency_ms=latency_ms,
            content_type="audio/wav",
            duration_ms=duration_ms,
        )