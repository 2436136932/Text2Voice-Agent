"""MiMo TTS adapter tests.

MiMo advertises "OpenAI API 兼容" but its speech surface is **not** the OpenAI
``/audio/speech`` contract: it posts to ``/chat/completions``, authenticates with
an ``api-key`` header, carries the text as an *assistant* message, and returns
base64 audio inside ``choices[0].message.audio.data``.

These tests pin that protocol so a future refactor cannot quietly turn it back
into the OpenAI shape (which returns nginx 404s, not API errors).
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.adapters.base import TTSRequest
from app.adapters.registry import registry
from app.core.errors import AppError, ErrorCode

pytestmark = pytest.mark.asyncio

FAKE_AUDIO = b"ID3" + b"\x00" * 100


def _success_body(audio: bytes = FAKE_AUDIO) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "mimo-v2.5-tts",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "audio": {
                        "id": "audio-1",
                        "data": base64.b64encode(audio).decode("ascii"),
                        "expires_at": None,
                        "transcript": None,
                    },
                },
            }
        ],
    }


class _Recorder:
    def __init__(self, responder) -> None:  # noqa: ANN001
        self.responder = responder
        self.calls: list[dict] = []

    async def request(self, method, url, **kwargs):  # noqa: ANN001
        self.calls.append({"method": method, "url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
        return self.responder(method, url)


@pytest.fixture()
def patch_client(monkeypatch):
    def install(recorder: _Recorder) -> None:
        class _Client:
            async def request(self, method, url, **kwargs):  # noqa: ANN001
                return await recorder.request(method, url, **kwargs)

        monkeypatch.setattr("app.adapters.base.get_http_client", lambda: _Client())

    return install


class TestMiMoRegistry:
    def test_registered(self):
        assert "mimo" in registry.tts_ids()

    def test_metadata(self):
        adapter = registry.tts("mimo")
        described = adapter.describe()
        assert "MiMo" in described["name"]
        assert described["default_voice"] == "mimo_default"
        assert described["default_model"] == "mimo-v2.5-tts"
        assert "mp3" in described["formats"]

    def test_preset_voices_present(self):
        ids = {voice.id for voice in registry.tts("mimo").voices}
        assert {"mimo_default", "冰糖", "茉莉", "苏打", "白桦"} <= ids
        assert {"Mia", "Chloe", "Milo", "Dean"} <= ids

    def test_clone_capability_declared(self):
        described = registry.tts("mimo").describe()
        assert described["supports_voice_clone"] is True
        assert described["supports_reference_audio"] is True
        assert any("voiceclone" in m["id"] for m in described["clone_models"])


class TestMiMoProtocol:
    async def test_posts_to_chat_completions_with_api_key_header(self, patch_client):
        """The whole point: NOT /audio/speech, and NOT Authorization."""
        recorder = _Recorder(lambda m, u: httpx.Response(200, json=_success_body(), request=httpx.Request(m, u)))
        patch_client(recorder)

        await registry.tts("mimo").synthesize(
            TTSRequest(text="你好", api_key="test-key-1234", voice="冰糖", model="mimo-v2.5-tts", audio_format="mp3")
        )

        call = recorder.calls[0]
        assert call["url"] == "https://api.xiaomimimo.com/v1/chat/completions"
        assert "/audio/speech" not in call["url"]
        assert call["headers"]["api-key"] == "test-key-1234"
        assert "Authorization" not in call["headers"]

    async def test_text_travels_as_assistant_message(self, patch_client):
        recorder = _Recorder(lambda m, u: httpx.Response(200, json=_success_body(), request=httpx.Request(m, u)))
        patch_client(recorder)

        await registry.tts("mimo").synthesize(
            TTSRequest(text="要朗读的文字", api_key="k-12345678", voice="茉莉", model="mimo-v2.5-tts", audio_format="mp3")
        )

        body = recorder.calls[0]["json"]
        assert body["messages"][-1] == {"role": "assistant", "content": "要朗读的文字"}
        assert body["messages"][0]["role"] == "user"
        assert body["audio"] == {"format": "mp3", "voice": "茉莉"}
        assert body["stream"] is False

    async def test_base64_audio_is_decoded(self, patch_client):
        recorder = _Recorder(lambda m, u: httpx.Response(200, json=_success_body(), request=httpx.Request(m, u)))
        patch_client(recorder)

        result = await registry.tts("mimo").synthesize(
            TTSRequest(text="hi", api_key="k-12345678", voice="mimo_default", model="mimo-v2.5-tts")
        )
        assert result.audio == FAKE_AUDIO
        assert result.provider == "mimo"
        assert result.content_type == "audio/mpeg"

    async def test_streaming_chunk_shape_also_accepted(self, patch_client):
        """Tolerate the delta shape even though we do not stream."""
        body = {
            "choices": [
                {"index": 0, "delta": {"audio": {"data": base64.b64encode(FAKE_AUDIO).decode()}}}
            ]
        }
        recorder = _Recorder(lambda m, u: httpx.Response(200, json=body, request=httpx.Request(m, u)))
        patch_client(recorder)

        result = await registry.tts("mimo").synthesize(
            TTSRequest(text="hi", api_key="k-12345678", voice="mimo_default", model="mimo-v2.5-tts")
        )
        assert result.audio == FAKE_AUDIO

    async def test_missing_audio_gives_actionable_error(self, patch_client):
        body = {"choices": [{"index": 0, "finish_reason": "content_filter", "message": {"content": ""}}]}
        recorder = _Recorder(lambda m, u: httpx.Response(200, json=body, request=httpx.Request(m, u)))
        patch_client(recorder)

        with pytest.raises(AppError) as exc:
            await registry.tts("mimo").synthesize(
                TTSRequest(text="hi", api_key="k-12345678", voice="mimo_default", model="mimo-v2.5-tts")
            )
        assert exc.value.code is ErrorCode.TTS_UPSTREAM_ERROR
        assert exc.value.detail.get("suggestion")

    async def test_unsupported_model_is_rejected_before_the_call(self, patch_client):
        recorder = _Recorder(lambda m, u: httpx.Response(200, json=_success_body(), request=httpx.Request(m, u)))
        patch_client(recorder)

        with pytest.raises(AppError) as exc:
            await registry.tts("mimo").synthesize(
                TTSRequest(text="hi", api_key="k-12345678", model="gpt-4o-mini-tts")
            )
        assert exc.value.code is ErrorCode.TTS_INVALID_VOICE
        assert "mimo-v2.5-tts" in str(exc.value.detail.get("suggestion", ""))
        assert recorder.calls == [], "must not call upstream with an invalid model"


class TestMiMoVoiceClone:
    async def test_clone_model_sends_base64_reference(self, patch_client):
        recorder = _Recorder(lambda m, u: httpx.Response(200, json=_success_body(), request=httpx.Request(m, u)))
        patch_client(recorder)

        reference = b"RIFF" + b"\x00" * 50
        await registry.tts("mimo").synthesize(
            TTSRequest(
                text="克隆朗读",
                api_key="k-12345678",
                model="mimo-v2.5-tts-voiceclone",
                audio_format="mp3",
                extra={"reference_audio_bytes": reference},
            )
        )
        body = recorder.calls[0]["json"]
        assert body["audio"]["voice"] == base64.b64encode(reference).decode("ascii")
        assert body["messages"][-1] == {"role": "assistant", "content": "克隆朗读"}

    async def test_clone_without_reference_is_rejected(self, patch_client):
        recorder = _Recorder(lambda m, u: httpx.Response(200, json=_success_body(), request=httpx.Request(m, u)))
        patch_client(recorder)

        with pytest.raises(AppError) as exc:
            await registry.tts("mimo").synthesize(
                TTSRequest(text="hi", api_key="k-12345678", model="mimo-v2.5-tts-voiceclone")
            )
        assert exc.value.code is ErrorCode.TTS_REFERENCE_UNSUPPORTED
        assert exc.value.detail.get("suggestion")
        assert recorder.calls == []


class TestMiMoValidation:
    async def test_validate_succeeds_with_a_short_probe(self, patch_client):
        recorder = _Recorder(lambda m, u: httpx.Response(200, json=_success_body(), request=httpx.Request(m, u)))
        patch_client(recorder)

        result = await registry.tts("mimo").validate(
            TTSRequest(text="", api_key="k-12345678", voice="冰糖", model="mimo-v2.5-tts")
        )
        assert result.valid is True
        # The probe text must stay short; this endpoint bills per character.
        assert recorder.calls[0]["json"]["messages"][-1]["content"] == "好"

    async def test_validate_reports_401_clearly(self, patch_client):
        recorder = _Recorder(
            lambda m, u: httpx.Response(401, json={"error": "unauthorized"}, request=httpx.Request(m, u))
        )
        patch_client(recorder)

        result = await registry.tts("mimo").validate(
            TTSRequest(text="", api_key="bad-key-1234", model="mimo-v2.5-tts")
        )
        assert result.valid is False
        assert result.detail.get("code") == ErrorCode.TTS_AUTH_FAILED.value
