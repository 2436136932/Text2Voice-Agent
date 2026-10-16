"""Regression tests for the custom (OpenAI-compatible) TTS path.

These pin the bugs found while debugging a real MiMo endpoint:

1. ``POST /providers/validate`` dropped ``model`` when building the TTS probe,
   so "测试连接" sent OpenAI's default model name to a third-party host and got
   a 404 — while actual synthesis (which does forward the model) worked.
2. ``CustomTTSAdapter`` inherited ``default_model_name = "gpt-4o-mini-tts"``,
   so a caller that omitted the model would ask MiMo for an OpenAI model.
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.base import TTSRequest
from app.adapters.registry import registry
from app.core.errors import AppError, ErrorCode


class _Recorder:
    """Captures the outgoing request and replies like a strict gateway."""

    def __init__(self, *, hosted_models: set[str]) -> None:
        self.hosted_models = hosted_models
        self.calls: list[dict] = []

    async def __call__(self, method: str, url: str, **kwargs):  # noqa: ANN001
        body = kwargs.get("json") or {}
        self.calls.append({"method": method, "url": url, "json": body})
        model = body.get("model")
        if model not in self.hosted_models:
            # This is exactly what a strict third-party gateway returns.
            return httpx.Response(
                404,
                json={"error": {"message": f"model `{model}` not found"}},
                request=httpx.Request(method, url),
            )
        return httpx.Response(
            200, content=b"ID3fakeaudio", request=httpx.Request(method, url)
        )


@pytest.fixture()
def patched_client(monkeypatch):
    """Patch the adapters' shared client factory (not httpx globally).

    Patching ``httpx.AsyncClient.request`` directly also intercepts the ASGI
    test transport and silently breaks unrelated assertions.
    """

    def install(recorder: _Recorder) -> None:
        class _Client:
            async def request(self, method, url, **kwargs):  # noqa: ANN001
                return await recorder(method, url, **kwargs)

        monkeypatch.setattr(
            "app.adapters.base.get_http_client", lambda: _Client()
        )

    return install


class TestCustomTtsModelForwarding:
    async def test_explicit_model_is_sent(self, patched_client):
        recorder = _Recorder(hosted_models={"mimo-v2.5-tts"})
        patched_client(recorder)

        result = await registry.tts("custom").synthesize(
            TTSRequest(
                text="你好",
                api_key="test-key-1234",
                voice="alloy",
                model="mimo-v2.5-tts",
                base_url="https://api.example.com/v1",
                audio_format="mp3",
            )
        )
        assert result.audio == b"ID3fakeaudio"
        assert recorder.calls[0]["json"]["model"] == "mimo-v2.5-tts"

    async def test_custom_never_defaults_to_an_openai_model(self, patched_client):
        """Omitting the model must not silently become 'gpt-4o-mini-tts'."""
        recorder = _Recorder(hosted_models={"mimo-v2.5-tts"})
        patched_client(recorder)

        # The gateway does not host any OpenAI model, so a leaked default would
        # surface as a 404 here.
        with pytest.raises(AppError):
            await registry.tts("custom").synthesize(
                TTSRequest(
                    text="你好",
                    api_key="test-key-1234",
                    voice="alloy",
                    base_url="https://api.example.com/v1",
                    audio_format="mp3",
                )
            )
        sent = recorder.calls[0]["json"].get("model")
        assert sent != "gpt-4o-mini-tts", "custom adapter must not send an OpenAI model name"

    async def test_speech_path_is_overridable(self, patched_client):
        recorder = _Recorder(hosted_models={"tts-x"})
        patched_client(recorder)

        await registry.tts("custom").synthesize(
            TTSRequest(
                text="你好",
                api_key="test-key-1234",
                voice="alloy",
                model="tts-x",
                base_url="https://api.example.com/v1",
                audio_format="mp3",
                extra={"speech_path": "/tts"},
            )
        )
        assert recorder.calls[0]["url"] == "https://api.example.com/v1/tts"

    async def test_default_path_is_audio_speech(self, patched_client):
        recorder = _Recorder(hosted_models={"tts-x"})
        patched_client(recorder)

        await registry.tts("custom").synthesize(
            TTSRequest(
                text="你好",
                api_key="test-key-1234",
                voice="alloy",
                model="tts-x",
                base_url="https://api.example.com/v1",
                audio_format="mp3",
            )
        )
        assert recorder.calls[0]["url"] == "https://api.example.com/v1/audio/speech"


class TestValidationForwardsModel:
    async def test_validate_probe_carries_the_model(self, patched_client):
        """`POST /providers/validate` must probe with the caller's model.

        Reproduces the reported bug: the probe dropped ``model`` and therefore
        asked the foreign gateway for OpenAI's default model -> 404 on a
        perfectly working MiMo endpoint.
        """
        from app.schemas.generation import ValidateProviderRequest
        from app.services.providers import provider_service

        recorder = _Recorder(hosted_models={"mimo-v2.5-tts"})
        patched_client(recorder)

        result = await provider_service.validate(
            ValidateProviderRequest(
                kind="tts",
                provider="custom",
                api_key="test-key-1234",
                base_url="https://api.example.com/v1",
                model="mimo-v2.5-tts",
                voice="alloy",
            )
        )
        assert result.valid is True, result.message
        assert recorder.calls[0]["json"]["model"] == "mimo-v2.5-tts"

    async def test_validate_reports_404_clearly(self, patched_client):
        """A wrong model still fails, but with an actionable message."""
        from app.schemas.generation import ValidateProviderRequest
        from app.services.providers import provider_service

        recorder = _Recorder(hosted_models={"mimo-v2.5-tts"})
        patched_client(recorder)

        result = await provider_service.validate(
            ValidateProviderRequest(
                kind="tts",
                provider="custom",
                api_key="test-key-1234",
                base_url="https://api.example.com/v1",
                model="not-a-real-model",
                voice="alloy",
            )
        )
        assert result.valid is False
        assert result.detail.get("code") == ErrorCode.LLM_UPSTREAM_ERROR.value or result.detail.get(
            "code"
        ) == ErrorCode.TTS_UPSTREAM_ERROR.value


class TestCustomAdapterMetadata:
    def test_custom_tts_has_chinese_display_name(self):
        """The provider list is Chinese throughout; an English-only label made
        the entry hard to find."""
        adapter = registry.tts("custom")
        assert "自定义" in adapter.name

    def test_custom_tts_requires_base_url(self):
        """The UI reads `describe()`; that is where the flag must be correct.

        The adapter inherits `info()` from OpenAITTSAdapter, which builds
        `AdapterInfo` field-by-field and would otherwise report the parent's
        defaults.
        """
        described = registry.tts("custom").describe()
        assert described["requires_base_url"] is True
        assert described["requires_api_key"] is True

    def test_openai_keeps_its_own_default_model(self):
        """The subclass override must not leak into the real OpenAI adapter."""
        assert registry.tts("openai").default_model_name == "gpt-4o-mini-tts"
        assert registry.tts("custom").default_model_name == ""
