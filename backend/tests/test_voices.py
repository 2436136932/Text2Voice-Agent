"""Voice cloning: capability refusal, the ElevenLabs path, listing, deletion.

The clone call itself is never made for real: the httpx layer is monkeypatched
so the suite stays offline and asserts on the *contract* (what we send, what we
persist) rather than on a vendor's live behaviour.
"""

from __future__ import annotations

import io
import json
import wave

import pytest

from app.adapters.registry import registry
from app.core.errors import ErrorCode
from app.db.models import ClonedVoice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_wav(duration_ms: int = 1000, sample_rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(sample_rate * duration_ms / 1000))
    return buffer.getvalue()


async def upload_reference(client, duration_ms: int = 1000) -> str:
    response = await client.post(
        "/api/v1/media/inspect",
        files={"file": ("voice.wav", make_wav(duration_ms), "audio/wav")},
    )
    assert response.status_code == 200, f"{response.status_code}: {response.text}"
    return response.json()["media_id"]


class FakeResponse:
    """Minimal stand-in for an ``httpx.Response``."""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def fake_httpx(monkeypatch):
    """Capture outbound vendor requests and replay canned JSON responses.

    Only *outbound* calls are intercepted: the returned handler is installed on
    the adapters' shared client (``app.adapters.base.get_http_client``), not on
    ``httpx.AsyncClient.request`` globally - patching the class would also
    swallow the in-process ASGI test transport and silently break every other
    request the test makes.
    """
    calls: list[dict] = []
    state: dict = {}

    async def _request(method, url, **kwargs):  # noqa: ANN001, ANN003
        record = {"method": method, "url": str(url), **kwargs}
        calls.append(record)
        return state["handler"](record)

    class _FakeClient:
        """Stands in for the pooled ``httpx.AsyncClient``."""

        is_closed = False

        async def request(self, method, url, **kwargs):  # noqa: ANN001, ANN003
            return await _request(method, url, **kwargs)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("app.adapters.base.get_http_client", lambda: _FakeClient())

    def install(handler) -> None:
        state["handler"] = handler

    install(lambda record: FakeResponse({"voice_id": "cloned-voice-123"}))
    install.calls = calls  # type: ignore[attr-defined]
    return install


def assert_no_secret_columns() -> None:
    """Prove the schema itself cannot store a credential."""
    column_names = {column.name for column in ClonedVoice.__table__.columns}
    assert column_names == {
        "id",
        "voice_id",
        "provider",
        "name",
        "model",
        "source_media_id",
        "source_duration_ms",
        "created_at",
    }
    for name in column_names:
        assert "key" not in name, f"unexpected credential-ish column: {name}"
        assert "secret" not in name
        assert "token" not in name


# ---------------------------------------------------------------------------
# Capability declarations
# ---------------------------------------------------------------------------
class TestCloneCapabilities:
    def test_declared_clone_providers(self):
        assert registry.tts("elevenlabs").supports_voice_clone is True
        assert registry.tts("minimax").supports_voice_clone is True
        assert registry.tts("custom").supports_reference_audio is True
        assert registry.tts("custom").supports_voice_clone is False

    def test_describe_exposes_clone_metadata(self):
        for provider_id in ("elevenlabs", "minimax", "custom", "openai", "mock"):
            payload = registry.tts(provider_id).describe()
            for key in (
                "supports_voice_clone",
                "supports_reference_audio",
                "clone_models",
                "clone_hint",
            ):
                assert key in payload, f"{provider_id} missing {key}"

    def test_vendor_without_clone_has_a_hint(self):
        # Even providers that cannot clone should explain themselves.
        assert registry.tts("openai").clone_hint
        assert registry.tts("openai").supports_voice_clone is False


# ---------------------------------------------------------------------------
# Capabilities endpoint
# ---------------------------------------------------------------------------
class TestCapabilitiesRoute:
    async def test_shape_and_ffmpeg_flag(self, client):
        response = await client.get("/api/v1/capabilities")
        assert response.status_code == 200
        payload = response.json()

        assert set(payload) == {"ffmpeg", "voice_clone", "upload"}
        assert set(payload["ffmpeg"]) == {"available", "path", "version", "message"}
        assert set(payload["voice_clone"]) == {
            "supported_providers",
            "reference_audio_providers",
            "note",
        }
        assert set(payload["upload"]) >= {
            "accept",
            "max_size_mb",
            "trimmable_formats",
            "video_extraction_available",
        }

        # This environment has no ffmpeg (asserted by the task brief).
        assert payload["ffmpeg"]["available"] is False
        assert payload["ffmpeg"]["path"] is None
        assert payload["ffmpeg"]["version"] is None
        assert "未检测到 ffmpeg" in payload["ffmpeg"]["message"]
        assert "winget install Gyan.FFmpeg" in payload["ffmpeg"]["message"]

    async def test_lists_clone_capable_providers(self, client):
        payload = (await client.get("/api/v1/capabilities")).json()
        assert "elevenlabs" in payload["voice_clone"]["supported_providers"]
        assert "minimax" in payload["voice_clone"]["supported_providers"]
        assert "custom" in payload["voice_clone"]["reference_audio_providers"]
        assert payload["voice_clone"]["note"]

    async def test_upload_section_matches_the_media_route(self, client):
        payload = (await client.get("/api/v1/capabilities")).json()
        upload = payload["upload"]
        extensions = upload["accept"].split(",")
        assert ".wav" in extensions
        assert ".mp4" in extensions
        assert all(item.startswith(".") for item in extensions)
        assert upload["max_size_mb"] == 50
        assert upload["trimmable_formats"] == ["wav"]
        assert upload["video_extraction_available"] is False

    async def test_refresh_parameter_is_accepted(self, client):
        assert (await client.get("/api/v1/capabilities?refresh=true")).status_code == 200


# ---------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------
class TestVoiceClone:
    async def test_initial_list_is_empty(self, client):
        response = await client.get("/api/v1/voices")
        assert response.status_code == 200
        assert response.json() == {"items": []}

    async def test_mock_provider_is_refused_with_suggestion(self, client):
        media_id = await upload_reference(client)
        response = await client.post(
            "/api/v1/voices/clone",
            json={"provider": "mock", "name": "我的声音", "media_id": media_id},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == ErrorCode.VOICE_CLONE_UNSUPPORTED.value
        assert body["detail"]["suggestion"]
        assert "elevenlabs" in body["detail"]["supported_providers"]
        # Nothing may be persisted when the clone is refused.
        assert (await client.get("/api/v1/voices")).json()["items"] == []

    async def test_openai_provider_is_refused_with_suggestion(self, client):
        media_id = await upload_reference(client)
        response = await client.post(
            "/api/v1/voices/clone",
            json={"provider": "openai", "name": "x", "media_id": media_id, "api_key": "sk-test-abcdefgh"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["suggestion"]

    async def test_unknown_provider_is_refused(self, client):
        media_id = await upload_reference(client)
        response = await client.post(
            "/api/v1/voices/clone",
            json={"provider": "does-not-exist", "name": "x", "media_id": media_id},
        )
        assert response.status_code == 400

    async def test_missing_media_is_404(self, client):
        response = await client.post(
            "/api/v1/voices/clone",
            json={
                "provider": "elevenlabs",
                "name": "我的声音",
                "media_id": "f" * 32,
                "api_key": "0123456789abcdef0123456789abcdef",
            },
        )
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.MEDIA_NOT_FOUND.value

    async def test_elevenlabs_clone_persists_row(self, client, fake_httpx, session):
        media_id = await upload_reference(client, duration_ms=1200)

        fake_httpx(lambda record: FakeResponse({"voice_id": "el-voice-abc"}))
        response = await client.post(
            "/api/v1/voices/clone",
            json={
                "provider": "elevenlabs",
                "api_key": "0123456789abcdef0123456789abcdef",
                "name": "我的声音",
                "media_id": media_id,
                "model": "eleven_multilingual_v2",
                "extra": {},
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["voice_id"] == "el-voice-abc"
        assert payload["provider"] == "elevenlabs"
        assert payload["name"] == "我的声音"
        assert payload["model"] == "eleven_multilingual_v2"
        assert payload["id"] > 0
        assert payload["created_at"]
        assert payload["duration_ms"] is not None

        # The upload really was a multipart POST to /voices/add.
        call = fake_httpx.calls[-1]
        assert call["method"] == "POST"
        assert call["url"].endswith("/voices/add")
        assert "files" in call and "files" in call["files"]

    async def test_successful_clone_appears_in_list(self, client, fake_httpx):
        media_id = await upload_reference(client)
        fake_httpx(lambda record: FakeResponse({"voice_id": "el-voice-abc"}))

        await client.post(
            "/api/v1/voices/clone",
            json={
                "provider": "elevenlabs",
                "api_key": "0123456789abcdef0123456789abcdef",
                "name": "我的声音",
                "media_id": media_id,
            },
        )

        items = (await client.get("/api/v1/voices")).json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["voice_id"] == "el-voice-abc"
        assert item["name"] == "我的声音"
        assert set(item) >= {
            "id",
            "voice_id",
            "provider",
            "name",
            "model",
            "duration_ms",
            "created_at",
        }

    async def test_list_is_newest_first(self, client, fake_httpx):
        fake_httpx(lambda record: FakeResponse({"voice_id": "v"}))
        for index in range(3):
            media_id = await upload_reference(client)
            await client.post(
                "/api/v1/voices/clone",
                json={
                    "provider": "elevenlabs",
                    "api_key": "0123456789abcdef0123456789abcdef",
                    "name": f"voice-{index}",
                    "media_id": media_id,
                },
            )
        items = (await client.get("/api/v1/voices")).json()["items"]
        assert [item["name"] for item in items] == ["voice-2", "voice-1", "voice-0"]

    async def test_api_key_is_never_persisted(self, client, fake_httpx, session):
        """The row must be credential-free, column by column."""
        assert_no_secret_columns()

        media_id = await upload_reference(client)
        fake_httpx(lambda record: FakeResponse({"voice_id": "el-voice-abc"}))
        secret = "0123456789abcdef0123456789abcdef"
        await client.post(
            "/api/v1/voices/clone",
            json={
                "provider": "elevenlabs",
                "api_key": secret,
                "name": "我的声音",
                "media_id": media_id,
                "extra": {"group_id": "g-1"},
            },
        )

        row = (await session.execute(__import__("sqlalchemy").select(ClonedVoice))).scalars().one()
        for column in ClonedVoice.__table__.columns:
            value = getattr(row, column.name)
            assert value != secret, f"{column.name} stored the api key"
            if isinstance(value, str):
                assert secret not in value

        # And the serialized payload never echoes it either.
        listed = await client.get("/api/v1/voices")
        assert secret not in listed.text

    async def test_clone_payload_never_contains_the_raw_clip(self, client, fake_httpx):
        """Audio bytes may go upstream, but not into the response or DB row."""
        media_id = await upload_reference(client)
        fake_httpx(lambda record: FakeResponse({"voice_id": "v"}))
        response = await client.post(
            "/api/v1/voices/clone",
            json={
                "provider": "elevenlabs",
                "api_key": "0123456789abcdef0123456789abcdef",
                "name": "n",
                "media_id": media_id,
            },
        )
        assert "audio" not in response.text
        assert "reference_audio_bytes" not in response.text

    async def test_vendor_failure_is_reported_as_clone_failed(self, client, fake_httpx):
        media_id = await upload_reference(client)
        # 200 but no voice_id -> our contract check must catch it.
        fake_httpx(lambda record: FakeResponse({"detail": "nope"}))
        response = await client.post(
            "/api/v1/voices/clone",
            json={
                "provider": "elevenlabs",
                "api_key": "0123456789abcdef0123456789abcdef",
                "name": "n",
                "media_id": media_id,
            },
        )
        assert response.status_code == 502
        body = response.json()
        assert body["code"] == ErrorCode.VOICE_CLONE_FAILED.value
        assert body["detail"]["suggestion"]

    async def test_minimax_clone_uploads_file_then_clones(self, client, fake_httpx):
        """MiniMax needs two calls: files/upload, then voice_clone."""
        media_id = await upload_reference(client)

        def handler(record):
            url = record["url"]
            if "files/upload" in url:
                return FakeResponse({"file": {"file_id": "file-42"}})
            return FakeResponse({"voice_id": "mm-voice-1"})

        fake_httpx(handler)
        response = await client.post(
            "/api/v1/voices/clone",
            json={
                "provider": "minimax",
                "api_key": "jwt-token-abcdefghij",
                "name": "我的音色",
                "media_id": media_id,
                "extra": {"group_id": "12345"},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["voice_id"] == "mm-voice-1"

        urls = [call["url"] for call in fake_httpx.calls]
        assert any("files/upload" in url for url in urls)
        assert any("voice_clone" in url for url in urls)
        # Both calls must carry the GroupId when supplied.
        assert all("GroupId=12345" in url for url in urls)

        clone_call = next(c for c in fake_httpx.calls if "voice_clone" in c["url"])
        # ``request_json`` forwards the body as httpx's ``json=`` kwarg.
        assert clone_call["json"]["file_id"] == "file-42"

    async def test_missing_credential_is_rejected(self, client, fake_httpx):
        media_id = await upload_reference(client)
        fake_httpx(lambda record: FakeResponse({"voice_id": "v"}))
        response = await client.post(
            "/api/v1/voices/clone",
            json={"provider": "elevenlabs", "name": "n", "media_id": media_id},
        )
        assert response.status_code == 400
        assert response.json()["code"] == ErrorCode.MISSING_CREDENTIAL.value

    async def test_blank_name_is_422(self, client):
        media_id = await upload_reference(client)
        response = await client.post(
            "/api/v1/voices/clone",
            json={"provider": "elevenlabs", "name": "   ", "media_id": media_id},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Providers integration
# ---------------------------------------------------------------------------
class TestClonedVoiceInProviders:
    async def test_not_listed_before_cloning(self, client):
        payload = (await client.get("/api/v1/providers")).json()
        elevenlabs = next(item for item in payload["tts"] if item["id"] == "elevenlabs")
        assert all(not voice.get("cloned") for voice in elevenlabs["voices"])

    async def test_cloned_voice_is_merged_into_provider(self, client, fake_httpx):
        media_id = await upload_reference(client)
        fake_httpx(lambda record: FakeResponse({"voice_id": "el-voice-abc"}))
        await client.post(
            "/api/v1/voices/clone",
            json={
                "provider": "elevenlabs",
                "api_key": "0123456789abcdef0123456789abcdef",
                "name": "我的声音",
                "media_id": media_id,
            },
        )

        payload = (await client.get("/api/v1/providers")).json()
        elevenlabs = next(item for item in payload["tts"] if item["id"] == "elevenlabs")
        cloned = [voice for voice in elevenlabs["voices"] if voice.get("cloned")]
        assert len(cloned) == 1
        assert cloned[0]["id"] == "el-voice-abc"
        assert "我的声音" in cloned[0]["name"]

    async def test_other_providers_do_not_gain_the_voice(self, client, fake_httpx):
        media_id = await upload_reference(client)
        fake_httpx(lambda record: FakeResponse({"voice_id": "el-voice-abc"}))
        await client.post(
            "/api/v1/voices/clone",
            json={
                "provider": "elevenlabs",
                "api_key": "0123456789abcdef0123456789abcdef",
                "name": "我的声音",
                "media_id": media_id,
            },
        )
        payload = (await client.get("/api/v1/providers")).json()
        for entry in payload["tts"]:
            if entry["id"] == "elevenlabs":
                continue
            assert all(not voice.get("cloned") for voice in entry["voices"]), entry["id"]

    async def test_provider_listing_still_hides_no_keys(self, client):
        text = (await client.get("/api/v1/providers")).text
        assert "0123456789abcdef0123456789abcdef" not in text


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
class TestVoiceDelete:
    async def test_delete_removes_the_row(self, client, fake_httpx):
        media_id = await upload_reference(client)
        fake_httpx(lambda record: FakeResponse({"voice_id": "v"}))
        created = (
            await client.post(
                "/api/v1/voices/clone",
                json={
                    "provider": "elevenlabs",
                    "api_key": "0123456789abcdef0123456789abcdef",
                    "name": "n",
                    "media_id": media_id,
                },
            )
        ).json()

        assert (await client.delete(f"/api/v1/voices/{created['id']}")).status_code == 204
        assert (await client.get("/api/v1/voices")).json()["items"] == []

        # And it disappears from the provider catalog too.
        payload = (await client.get("/api/v1/providers")).json()
        elevenlabs = next(item for item in payload["tts"] if item["id"] == "elevenlabs")
        assert all(not voice.get("cloned") for voice in elevenlabs["voices"])

    async def test_delete_unknown_is_404(self, client):
        response = await client.delete("/api/v1/voices/999")
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == ErrorCode.VOICE_NOT_FOUND.value
        assert body["detail"]["suggestion"]


# ---------------------------------------------------------------------------
# Reference-audio synthesis path
# ---------------------------------------------------------------------------
class TestReferenceAudio:
    async def test_unsupported_provider_is_refused_with_suggestion(self, client):
        """A provider that can neither clone nor take a clip must not pretend."""
        media_id = await upload_reference(client)
        response = await client.post(
            "/api/v1/generate",
            json={
                "text": "你好",
                "llm": {"provider": "mock", "model": "mock-echo-v1"},
                "tts": {"provider": "openai", "api_key": "sk-test-abcdefghijklmnop"},
                "options": {"reference_media_id": media_id},
            },
        )
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == ErrorCode.TTS_REFERENCE_UNSUPPORTED.value
        assert body["detail"]["suggestion"]
        assert body["detail"]["provider"] == "openai"

    async def test_missing_reference_media_is_404(self, client):
        response = await client.post(
            "/api/v1/generate",
            json={
                "text": "你好",
                "llm": {"provider": "mock", "model": "mock-echo-v1"},
                "tts": {"provider": "mock", "voice": "mock-neutral", "format": "wav"},
                "options": {"reference_media_id": "a" * 32},
            },
        )
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.MEDIA_NOT_FOUND.value

    async def test_reference_audio_reaches_the_adapter_for_custom(self, client, monkeypatch):
        """``custom`` declares support, so the clip must actually be forwarded."""
        media_id = await upload_reference(client, duration_ms=800)
        captured: dict = {}

        from app.adapters.tts.custom import CustomTTSAdapter

        async def fake_synthesize(self, request):  # noqa: ANN001
            captured["extra"] = dict(request.extra)
            from app.adapters.base import TTSResult

            return TTSResult(
                audio=make_wav(200),
                provider=self.id,
                voice=request.voice,
                audio_format="wav",
                latency_ms=1,
            )

        monkeypatch.setattr(CustomTTSAdapter, "synthesize", fake_synthesize)

        response = await client.post(
            "/api/v1/generate",
            json={
                "text": "你好",
                "llm": {"provider": "mock", "model": "mock-echo-v1"},
                "tts": {
                    "provider": "custom",
                    "base_url": "https://example.invalid/v1",
                    "api_key": "sk-test-abcdefghijklmnop",
                    "format": "wav",
                },
                "options": {"reference_media_id": media_id, "auto_save": False},
            },
        )
        assert response.status_code == 200, response.text
        assert captured["extra"]["reference_audio_media_id"] == media_id
        assert isinstance(captured["extra"]["reference_audio_bytes"], bytes)
        assert captured["extra"]["reference_audio_bytes"]


# ---------------------------------------------------------------------------
# Error catalogue
# ---------------------------------------------------------------------------
class TestNewErrorCodesAreDocumented:
    @pytest.mark.parametrize(
        ("code", "expected_status"),
        [
            ("MEDIA_NOT_FOUND", 404),
            ("MEDIA_TRIM_UNSUPPORTED", 422),
            ("MEDIA_UNSUPPORTED_FORMAT", 400),
            ("MEDIA_TOO_LARGE", 413),
            ("VOICE_CLONE_UNSUPPORTED", 400),
            ("VOICE_CLONE_FAILED", 502),
            ("VOICE_NOT_FOUND", 404),
            ("TTS_REFERENCE_UNSUPPORTED", 400),
            ("FFMPEG_REQUIRED", 422),
        ],
    )
    async def test_code_is_in_the_catalogue(self, client, code, expected_status):
        payload = (await client.get("/api/v1/meta/error-codes")).json()
        by_code = {item["code"]: item for item in payload["codes"]}
        assert code in by_code, f"{code} missing from /meta/error-codes"
        assert by_code[code]["http_status"] == expected_status
        assert by_code[code]["description"], f"{code} has no description"
