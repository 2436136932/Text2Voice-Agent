"""A reference clip must never be silently dropped at synthesis time.

Three provider shapes exist:

1. consumes the clip per call          (MiMo, custom)   -> works
2. registers the voice first           (ElevenLabs, MiniMax) -> must refuse
   a per-call clip, pointing at the register-then-select flow
3. cannot clone at all                 (OpenAI, mock)  -> must refuse

Case 2 is the dangerous one: ``supports_voice_clone`` is True, so a naive
``supports_reference_audio or supports_voice_clone`` check lets the request
through, the adapter never reads the clip, and the user hears a built-in
voice believing they cloned.
"""

from __future__ import annotations

import pytest

from app.adapters.base import TTSRequest
from app.adapters.registry import registry
from app.core.errors import AppError, ErrorCode
from app.schemas.generation import GenerateRequest
from app.services.generation import GenerationService
from app.services.media import media_store
from app.services.voices import clone_requires_registration_error

pytestmark = pytest.mark.asyncio


def make_clip() -> str:
    record = media_store.save(
        data=b"RIFF-fake-reference-clip",
        filename="ref.wav",
        extension="wav",
        kind="audio",
        content_type="audio/wav",
        duration_ms=5000,
        sample_rate=24000,
        channels=1,
    )
    return record.media_id


class TestProviderShapes:
    def test_mimo_consumes_clips_per_call(self):
        adapter = registry.tts("mimo")
        assert adapter.supports_reference_audio is True

    def test_custom_consumes_clips_per_call(self):
        adapter = registry.tts("custom")
        assert adapter.supports_reference_audio is True

    def test_elevenlabs_registers_instead(self):
        """Clone-capable but NOT reference-audio capable."""
        adapter = registry.tts("elevenlabs")
        assert adapter.supports_voice_clone is True
        assert adapter.supports_reference_audio is False

    def test_minimax_registers_instead(self):
        adapter = registry.tts("minimax")
        assert adapter.supports_voice_clone is True
        assert adapter.supports_reference_audio is False

    def test_openai_cannot_clone_at_all(self):
        adapter = registry.tts("openai")
        assert adapter.supports_voice_clone is False
        assert adapter.supports_reference_audio is False


class TestReferenceClipIsNeverDropped:
    async def test_elevenlabs_refuses_per_call_clip(self, session):
        """The failure must be an error, not a silent preset voice."""
        clip = make_clip()
        request = GenerateRequest(
            text="测试文本",
            llm={"provider": "mock"},
            tts={"provider": "elevenlabs", "api_key": "sk-test-abcdefghijklmnop", "format": "mp3"},
            options={"auto_save": False, "reuse_reply_text": "测试文本", "reference_media_id": clip},
        )
        with pytest.raises(AppError) as excinfo:
            await GenerationService().generate(session, request)
        assert excinfo.value.code is ErrorCode.TTS_REFERENCE_UNSUPPORTED
        # The message must route the user to the working flow.
        suggestion = excinfo.value.detail.get("suggestion", "")
        assert "声音克隆" in suggestion
        assert excinfo.value.detail.get("clone_mode") == "register-then-use"

    async def test_minimax_refuses_per_call_clip(self, session):
        clip = make_clip()
        request = GenerateRequest(
            text="测试文本",
            llm={"provider": "mock"},
            tts={"provider": "minimax", "api_key": "sk-test-abcdefghijklmnop", "format": "mp3"},
            options={"auto_save": False, "reuse_reply_text": "测试文本", "reference_media_id": clip},
        )
        with pytest.raises(AppError) as excinfo:
            await GenerationService().generate(session, request)
        assert excinfo.value.code is ErrorCode.TTS_REFERENCE_UNSUPPORTED

    async def test_openai_without_clone_gets_the_other_error(self, session):
        """No clone support at all -> the generic capability error."""
        clip = make_clip()
        request = GenerateRequest(
            text="测试文本",
            llm={"provider": "mock"},
            tts={"provider": "openai", "api_key": "sk-test-abcdefghijklmnop", "format": "mp3"},
            options={"auto_save": False, "reuse_reply_text": "测试文本", "reference_media_id": clip},
        )
        with pytest.raises(AppError) as excinfo:
            await GenerationService().generate(session, request)
        assert excinfo.value.code is ErrorCode.TTS_REFERENCE_UNSUPPORTED
        # Distinguishable from the register-first case.
        assert excinfo.value.detail.get("clone_mode") is None

    async def test_mock_without_clip_still_works(self, session):
        """A request with no clip is unaffected by the guard."""
        request = GenerateRequest(
            text="测试文本",
            llm={"provider": "mock"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": False, "reuse_reply_text": "测试文本"},
        )
        response = await GenerationService().generate(session, request)
        assert response.audio_bytes > 0


class TestErrorMessageShape:
    def test_registration_error_names_the_supported_providers(self):
        error = clone_requires_registration_error("elevenlabs", "eleven_multilingual_v2")
        assert error.code is ErrorCode.TTS_REFERENCE_UNSUPPORTED
        suggestion = error.detail["suggestion"]
        # MiMo (and custom) are the ones that accept per-call clips.
        assert "mimo" in suggestion or "custom" in suggestion
        assert error.detail["provider"] == "elevenlabs"
        assert error.detail["model"] == "eleven_multilingual_v2"
