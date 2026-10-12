"""Adapter layer tests: registry, capability metadata, error mapping, HTTP.

The mock adapters are exercised end-to-end (no network). The real adapters are
tested through a monkeypatched transport so their request *shape* - headers,
payload, endpoint - is verified without ever touching a vendor.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.adapters.base import (
    LLMRequest,
    TTSRequest,
    map_upstream_error,
    request_bytes,
    request_json,
)
from app.adapters.registry import registry
from app.adapters.tts.mock import MockLLMAdapter, MockTTSAdapter
from app.core.errors import AppError, ErrorCode
from app.services.audio import estimate_duration_ms


class TestRegistry:
    def test_required_llm_providers_are_registered(self):
        ids = registry.llm_ids()
        for required in ("openai", "anthropic", "qwen"):
            assert required in ids, f"{required} must be natively supported"

    def test_required_tts_providers_are_registered(self):
        ids = registry.tts_ids()
        for required in ("openai", "elevenlabs", "azure", "volcengine"):
            assert required in ids, f"{required} must be natively supported"

    def test_unknown_provider_raises_typed_error(self):
        with pytest.raises(AppError) as exc:
            registry.llm("does-not-exist")
        assert exc.value.code is ErrorCode.LLM_UNSUPPORTED_PROVIDER
        assert "supported" in exc.value.detail

        with pytest.raises(AppError) as tts_exc:
            registry.tts("does-not-exist")
        assert tts_exc.value.code is ErrorCode.TTS_UNSUPPORTED_PROVIDER

    def test_catalog_payload_is_ui_ready(self):
        catalog = registry.describe_all()
        assert set(catalog) == {"llm", "tts"}
        llm_openai = next(item for item in catalog["llm"] if item["id"] == "openai")
        assert llm_openai["default_model"]
        assert llm_openai["models"]
        tts_openai = next(item for item in catalog["tts"] if item["id"] == "openai")
        assert tts_openai["default_voice"] == "alloy"
        assert tts_openai["voices"]
        assert "mp3" in tts_openai["formats"]
        assert tts_openai["speed_min"] <= 1.0 <= tts_openai["speed_max"]

    def test_every_provider_declares_capabilities(self):
        for item in registry.describe_all()["tts"]:
            assert item["id"]
            assert item["name"]
            assert item["formats"], f"{item['id']} must declare audio formats"
            assert item["voices"], f"{item['id']} must declare at least one voice"
            assert item["speed_min"] < item["speed_max"]

    def test_registry_is_case_insensitive(self):
        assert registry.llm("OpenAI").id == "openai"
        assert registry.tts("ELEVENLABS").id == "elevenlabs"


class TestCapabilityRules:
    def test_unsupported_format_is_rejected(self):
        adapter = registry.tts("openai")
        with pytest.raises(AppError) as exc:
            adapter.normalize_format("midi")
        assert exc.value.code is ErrorCode.TTS_UNSUPPORTED_FORMAT
        assert "mp3" in exc.value.detail["supported"]

    def test_format_normalization_is_case_insensitive(self):
        assert registry.tts("openai").normalize_format("MP3") == "mp3"

    def test_speed_is_clamped_to_vendor_range(self):
        adapter = registry.tts("openai")
        assert adapter.clamp_speed(99.0) == adapter.speed_max
        assert adapter.clamp_speed(-5.0) == adapter.speed_min
        assert adapter.clamp_speed(None) == 1.0

    def test_length_limit_is_enforced(self):
        adapter = registry.tts("volcengine")
        with pytest.raises(AppError) as exc:
            adapter.check_length("字" * (adapter.max_chars + 1))
        assert exc.value.code is ErrorCode.TTS_TEXT_TOO_LONG
        assert exc.value.detail["max_chars"] == adapter.max_chars

    def test_azure_builds_ssml_with_rate(self):
        adapter = registry.tts("azure")
        ssml = adapter.build_ssml(
            TTSRequest(text="你好 & 世界", api_key="k", voice="zh-CN-XiaoxiaoNeural", speed=1.5),
            "zh-CN-XiaoxiaoNeural",
            "zh-CN",
        )
        assert "<speak" in ssml
        assert "rate=\"+50%\"" in ssml
        assert "&amp;" in ssml  # escaped, so the XML stays valid
        assert "zh-CN-XiaoxiaoNeural" in ssml

    def test_azure_region_resolution(self):
        adapter = registry.tts("azure")
        assert adapter.resolve_region(TTSRequest(text="x", api_key="k", base_url="eastasia")) == "eastasia"
        assert (
            adapter.resolve_region(
                TTSRequest(
                    text="x",
                    api_key="k",
                    base_url="https://eastus.tts.speech.microsoft.com/cognitiveservices/v1",
                )
            )
            == "eastus"
        )
        assert adapter.resolve_region(TTSRequest(text="x", api_key="k", extra={"region": "japaneast"})) == "japaneast"


class TestUpstreamErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected", "retryable"),
        [
            (401, ErrorCode.LLM_AUTH_FAILED, False),
            (403, ErrorCode.LLM_AUTH_FAILED, False),
            (429, ErrorCode.LLM_RATE_LIMITED, True),
            (500, ErrorCode.LLM_UPSTREAM_ERROR, True),
            (502, ErrorCode.LLM_UPSTREAM_ERROR, True),
            (503, ErrorCode.LLM_UPSTREAM_ERROR, True),
            (400, ErrorCode.LLM_ERROR, False),
        ],
    )
    def test_status_codes_map_to_taxonomy(self, status, expected, retryable):
        error = map_upstream_error(
            kind="llm", provider="openai", status_code=status, body={"error": {"message": "boom"}}
        )
        assert error.code is expected
        assert error.retryable is retryable
        assert error.detail["upstream_status"] == status
        assert error.detail["upstream_message"] == "boom"

    def test_timeout_is_retryable(self):
        error = map_upstream_error(
            kind="tts", provider="openai", exception=httpx.ReadTimeout("slow")
        )
        assert error.code is ErrorCode.TTS_TIMEOUT
        assert error.retryable is True

    def test_connection_error_is_retryable(self):
        error = map_upstream_error(
            kind="llm", provider="openai", exception=httpx.ConnectError("refused")
        )
        assert error.code is ErrorCode.LLM_UPSTREAM_ERROR
        assert error.retryable is True

    def test_invalid_voice_is_detected_from_message(self):
        error = map_upstream_error(
            kind="tts",
            provider="azure",
            status_code=400,
            body={"message": "Invalid voice name provided"},
        )
        assert error.code is ErrorCode.TTS_INVALID_VOICE

    def test_error_payload_shape(self):
        error = map_upstream_error(kind="llm", provider="openai", status_code=401)
        payload = error.to_payload("req-123")
        assert set(payload) == {"code", "message", "detail", "request_id"}
        assert payload["code"] == "LLM_AUTH_FAILED"
        assert payload["request_id"] == "req-123"


class TestHttpHelpers:
    @pytest.mark.asyncio
    async def test_request_json_maps_http_error(self, monkeypatch):
        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            return httpx.Response(401, json={"error": {"message": "bad key"}}, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        with pytest.raises(AppError) as exc:
            await request_json(
                method="POST",
                url="https://example.com/v1/chat/completions",
                kind="llm",
                provider="openai",
                headers={"Authorization": "Bearer sk-secret-value-1234"},
                json_body={"model": "gpt-4o-mini"},
            )
        assert exc.value.code is ErrorCode.LLM_AUTH_FAILED

    @pytest.mark.asyncio
    async def test_request_bytes_rejects_empty_body(self, monkeypatch):
        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            return httpx.Response(200, content=b"", request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        with pytest.raises(AppError) as exc:
            await request_bytes(
                method="POST",
                url="https://example.com/v1/audio/speech",
                kind="tts",
                provider="openai",
                headers={},
                json_body={},
            )
        assert exc.value.code is ErrorCode.TTS_UPSTREAM_ERROR

    @pytest.mark.asyncio
    async def test_successful_json_request_returns_body(self, monkeypatch):
        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "hi"}}]}, request=httpx.Request(method, url)
            )

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        body, status = await request_json(
            method="POST",
            url="https://example.com/v1/chat/completions",
            kind="llm",
            provider="openai",
            headers={},
            json_body={},
        )
        assert status == 200
        assert body["choices"][0]["message"]["content"] == "hi"


class TestMockLLM:
    @pytest.mark.asyncio
    async def test_completion_is_deterministic_and_uses_no_credentials(self):
        adapter = MockLLMAdapter()
        request = LLMRequest(prompt="请介绍一下杭州", api_key="", max_tokens=128)
        first = await adapter.complete(request)
        second = await adapter.complete(request)
        assert first.text == second.text
        assert "请介绍一下杭州" in first.text
        assert first.usage["total_tokens"] > 0
        assert first.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_long_prompt_is_truncated_in_reply(self):
        adapter = MockLLMAdapter()
        result = await adapter.complete(LLMRequest(prompt="字" * 500, api_key=""))
        assert "…" in result.text
        assert len(result.text) < 900

    def test_mock_provider_does_not_require_a_key(self):
        assert registry.llm("mock").info().requires_api_key is False


class TestMockTTS:
    @pytest.mark.asyncio
    async def test_produces_valid_wav(self):
        adapter = MockTTSAdapter()
        result = await adapter.synthesize(
            TTSRequest(
                text="你好世界，这是一段测试语音。",
                api_key="",
                voice="mock-neutral",
                audio_format="wav",
            )
        )
        assert result.audio[:4] == b"RIFF"
        assert result.audio[8:12] == b"WAVE"
        assert result.content_type == "audio/wav"
        assert len(result.audio) > 44

    @pytest.mark.asyncio
    async def test_reported_duration_matches_header_math(self):
        adapter = MockTTSAdapter()
        result = await adapter.synthesize(
            TTSRequest(text="测试文本", api_key="", voice="mock-neutral", speed=1.0, audio_format="wav")
        )
        computed = estimate_duration_ms(result.audio, "wav")
        assert computed is not None
        # The adapter's own number and the independent parser must agree.
        assert abs(computed - (result.duration_ms or 0)) <= 50

    @pytest.mark.asyncio
    async def test_speed_changes_duration(self):
        adapter = MockTTSAdapter()
        slow = await adapter.synthesize(
            TTSRequest(text="一段用于比较语速的文本", api_key="", speed=0.5, audio_format="wav")
        )
        fast = await adapter.synthesize(
            TTSRequest(text="一段用于比较语速的文本", api_key="", speed=2.0, audio_format="wav")
        )
        assert (slow.duration_ms or 0) > (fast.duration_ms or 0)

    @pytest.mark.asyncio
    async def test_voice_changes_waveform(self):
        adapter = MockTTSAdapter()
        low = await adapter.synthesize(
            TTSRequest(text="同样文本", api_key="", voice="mock-low", audio_format="wav")
        )
        high = await adapter.synthesize(
            TTSRequest(text="同样文本", api_key="", voice="mock-high", audio_format="wav")
        )
        assert low.audio != high.audio

    @pytest.mark.asyncio
    async def test_validation_probe_succeeds_without_key(self):
        adapter = MockTTSAdapter()
        result = await adapter.validate(TTSRequest(text="", api_key=""))
        assert result.valid is True
        assert result.kind == "tts"


class TestProviderWireFormats:
    """Verify request shapes without any network access."""

    @pytest.mark.asyncio
    async def test_openai_tts_request_shape(self, monkeypatch):
        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            captured.update({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
            return httpx.Response(200, content=b"ID3fakeaudio", request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        adapter = registry.tts("openai")
        await adapter.synthesize(
            TTSRequest(text="你好", api_key="sk-test-key-123456", voice="nova", speed=1.25, audio_format="mp3")
        )
        assert captured["url"] == "https://api.openai.com/v1/audio/speech"
        assert captured["json"]["voice"] == "nova"
        assert captured["json"]["response_format"] == "mp3"
        assert captured["json"]["speed"] == 1.25
        assert captured["headers"]["Authorization"] == "Bearer sk-test-key-123456"

    @pytest.mark.asyncio
    async def test_elevenlabs_request_shape(self, monkeypatch):
        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            captured.update({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
            return httpx.Response(200, content=b"\xff\xfbfake", request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        adapter = registry.tts("elevenlabs")
        await adapter.synthesize(
            TTSRequest(text="hello", api_key="0123456789abcdef0123456789abcdef", voice="21m00Tcm4TlvDq8ikWAM")
        )
        assert "/text-to-speech/21m00Tcm4TlvDq8ikWAM" in captured["url"]
        assert "output_format=mp3_44100_128" in captured["url"]
        assert captured["headers"]["xi-api-key"] == "0123456789abcdef0123456789abcdef"
        assert "voice_settings" in captured["json"]

    @pytest.mark.asyncio
    async def test_volcengine_decodes_base64_payload(self, monkeypatch):
        audio = b"volcengine-audio-bytes"
        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            captured.update({"json": kwargs.get("json"), "headers": kwargs.get("headers")})
            return httpx.Response(
                200, json={"code": 3000, "data": base64.b64encode(audio).decode()}, request=httpx.Request(method, url)
            )

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        adapter = registry.tts("volcengine")
        result = await adapter.synthesize(
            TTSRequest(
                text="你好",
                api_key="volc-access-token-value",
                voice="zh_female_shuangkuaisisi_moon_bigtts",
                extra={"app_id": "1234567890", "cluster": "volcano_tts"},
            )
        )
        assert result.audio == audio
        assert captured["headers"]["Authorization"].startswith("Bearer;")
        assert captured["json"]["app"]["appid"] == "1234567890"
        assert captured["json"]["audio"]["voice_type"] == "zh_female_shuangkuaisisi_moon_bigtts"

    @pytest.mark.asyncio
    async def test_volcengine_auth_failure_is_typed(self, monkeypatch):
        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            return httpx.Response(
                200, json={"code": 3001, "message": "invalid token"}, request=httpx.Request(method, url)
            )

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        with pytest.raises(AppError) as exc:
            await registry.tts("volcengine").synthesize(
                TTSRequest(text="你好", api_key="bad-token-value", extra={"app_id": "1"})
            )
        assert exc.value.code is ErrorCode.TTS_AUTH_FAILED

    @pytest.mark.asyncio
    async def test_anthropic_request_shape(self, monkeypatch):
        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            captured.update({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "你好！"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "stop_reason": "end_turn",
                },
                request=httpx.Request(method, url),
            )

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        adapter = registry.llm("anthropic")
        result = await adapter.complete(
            LLMRequest(
                prompt="你好",
                api_key="sk-ant-api03-abcdefghijklmnop",
                model="claude-3-5-haiku-latest",
                system_prompt="你是助手",
                max_tokens=100,
            )
        )
        assert captured["url"] == "https://api.anthropic.com/v1/messages"
        assert captured["headers"]["anthropic-version"] == "2023-06-01"
        assert captured["headers"]["x-api-key"].startswith("sk-ant-")
        assert captured["json"]["system"] == "你是助手"
        # The system prompt must NOT be duplicated as a message.
        assert all(message["role"] != "system" for message in captured["json"]["messages"])
        assert result.text == "你好！"
        assert result.usage["total_tokens"] == 15

    @pytest.mark.asyncio
    async def test_gemini_request_shape(self, monkeypatch):
        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            captured.update({"url": url, "headers": kwargs.get("headers")})
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
                },
                request=httpx.Request(method, url),
            )

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        result = await registry.llm("gemini").complete(
            LLMRequest(prompt="hi", api_key="AIzaSyTestKeyValue1234567890", model="gemini-2.0-flash")
        )
        assert ":generateContent" in captured["url"]
        assert captured["headers"]["x-goog-api-key"].startswith("AIza")
        assert result.text == "hi"

    @pytest.mark.asyncio
    async def test_openai_compatible_uses_custom_base_url(self, monkeypatch):
        captured: dict = {}

        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            captured["url"] = url
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}], "model": "local"},
                request=httpx.Request(method, url),
            )

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        await registry.llm("openai").complete(
            LLMRequest(
                prompt="hi",
                api_key="sk-test-key-123456",
                base_url="https://my-gateway.example.com/v1/",
            )
        )
        assert captured["url"] == "https://my-gateway.example.com/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_empty_completion_raises_typed_error(self, monkeypatch):
        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            return httpx.Response(200, json={"choices": []}, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        with pytest.raises(AppError) as exc:
            await registry.llm("openai").complete(
                LLMRequest(prompt="hi", api_key="sk-test-key-123456")
            )
        assert exc.value.code is ErrorCode.LLM_EMPTY_RESPONSE

    @pytest.mark.asyncio
    async def test_missing_credential_fails_before_any_request(self, monkeypatch):
        called = {"count": 0}

        async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
            called["count"] += 1
            return httpx.Response(200, json={}, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        with pytest.raises(AppError) as exc:
            await registry.llm("openai").complete(LLMRequest(prompt="hi", api_key=""))
        assert exc.value.code is ErrorCode.MISSING_CREDENTIAL
        assert called["count"] == 0, "must not call upstream without a credential"


class TestDurationEstimation:
    def test_wav_duration_is_exact(self):
        adapter = MockTTSAdapter()
        audio, reported = adapter._render("测试", "mock-neutral", 1.0)
        assert estimate_duration_ms(audio, "wav") == reported

    def test_unknown_format_returns_none(self):
        assert estimate_duration_ms(b"junk", "exotic") is None

    def test_empty_returns_none(self):
        assert estimate_duration_ms(b"", "mp3") is None

    def test_mp3_falls_back_to_bitrate_estimate(self):
        # 16 KB of "MP3" -> ~1s at 128 kbps even without a parseable header.
        result = estimate_duration_ms(b"\xff\xfb" + b"\x00" * 16_000, "mp3")
        assert result is not None and result > 500