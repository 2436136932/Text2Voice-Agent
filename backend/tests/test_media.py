"""Media routes: upload/inspect, trim, Range streaming, delete.

Every test builds its own WAV with the standard library, so the suite needs no
ffmpeg, no network, and no audio fixtures on disk. Tests that exercise the
"ffmpeg is missing" path monkeypatch the probe rather than relying on this
machine's particular tooling.
"""

from __future__ import annotations

import io
import struct
import wave

import pytest

from app.core.errors import ErrorCode
from app.services import media as media_module
from app.services.tools import ToolStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_wav(
    *, duration_ms: int = 1000, sample_rate: int = 16_000, channels: int = 1, freq: float = 440.0
) -> bytes:
    """Build a real RIFF/PCM WAV of the requested length."""
    import math

    frames = int(sample_rate * duration_ms / 1000)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        payload = bytearray()
        for index in range(frames):
            value = int(12_000 * math.sin(2 * math.pi * freq * index / sample_rate))
            for _ in range(channels):
                payload += struct.pack("<h", value)
        handle.writeframes(bytes(payload))
    return buffer.getvalue()


async def upload(client, data: bytes, filename: str) -> object:
    return await client.post(
        "/api/v1/media/inspect",
        files={"file": (filename, data, "application/octet-stream")},
    )


def no_ffmpeg(monkeypatch) -> None:
    """Force the 'ffmpeg not installed' branch deterministically."""
    from app.services.tools import ToolStatus

    monkeypatch.setattr(
        media_module,
        "ffmpeg_status",
        lambda **_: ToolStatus(available=False, message="未检测到 ffmpeg"),
    )


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------
class TestMediaInspect:
    async def test_video_duration_is_probed_with_ffprobe_when_available(self, client, monkeypatch):
        """A video's duration must be read once ffmpeg is present.

        Without it the trim slider has no timeline, so the user cannot pick the
        segment to clone from — which is the whole point of uploading a video.
        """
        import app.services.media as media_mod

        monkeypatch.setattr(
            media_mod, "ffmpeg_status", lambda **_: ToolStatus(available=True, path="ffmpeg")
        )
        monkeypatch.setattr(media_mod, "ffprobe_path", lambda: "ffprobe")
        monkeypatch.setattr(media_mod, "_ffprobe_duration_ms", lambda path: 12_345)
        monkeypatch.setattr(media_mod, "_has_audio_stream", lambda path: True)

        response = await upload(client, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128, "clip.mp4")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["kind"] == "video"
        assert payload["duration_ms"] == 12_345
        assert payload["trimmable"] is True

    async def test_silent_video_warns_that_it_cannot_be_cloned_from(self, client, monkeypatch):
        """A video with no audio track is useless for cloning - say so."""
        import app.services.media as media_mod

        monkeypatch.setattr(
            media_mod, "ffmpeg_status", lambda **_: ToolStatus(available=True, path="ffmpeg")
        )
        monkeypatch.setattr(media_mod, "ffprobe_path", lambda: "ffprobe")
        monkeypatch.setattr(media_mod, "_ffprobe_duration_ms", lambda path: 8_000)
        monkeypatch.setattr(media_mod, "_has_audio_stream", lambda path: False)

        response = await upload(client, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128, "silent.mp4")
        assert response.status_code == 200
        warnings = " ".join(response.json()["warnings"])
        assert "没有音轨" in warnings

    async def test_upload_uses_the_upload_limit_not_the_json_limit(self, client, settings):
        """A video larger than the JSON body limit must still be accepted.

        Regression: the middleware applied `max_request_bytes` (2 MB) to every
        route, so a 16 MB mp4 was rejected with 413 while the UI advertised
        `max_upload_mb` (50 MB) — and because the rejection did not drain the
        body, the browser surfaced it as ERR_CONNECTION_RESET.
        """
        from app.api.middleware import RequestContextMiddleware

        upload_limit = RequestContextMiddleware._body_limit_for("/api/v1/media/inspect")
        json_limit = RequestContextMiddleware._body_limit_for("/api/v1/generate")
        assert upload_limit == settings.max_upload_mb * 1024 * 1024
        assert json_limit == settings.max_request_bytes
        assert upload_limit > json_limit

        # A payload comfortably above the JSON limit but below the upload limit.
        big = make_wav(duration_ms=120_000)
        assert len(big) > json_limit, "fixture must exceed the JSON limit to be meaningful"
        assert len(big) < upload_limit

        response = await upload(client, big, "long.wav")
        assert response.status_code == 200, response.text

    async def test_json_routes_keep_the_small_limit(self, client, settings):
        """The larger upload budget must not leak into normal JSON endpoints."""
        oversized = settings.max_request_bytes + 1024
        response = await client.post(
            "/api/v1/generate",
            content=b"x" * oversized,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["code"] == "PAYLOAD_TOO_LARGE"

    async def test_oversized_upload_is_rejected_with_guidance(self, client, settings, monkeypatch):
        """Above the upload limit: 413 with a suggestion, not a dropped socket."""
        monkeypatch.setattr(settings, "max_upload_mb", 1)
        big = make_wav(duration_ms=60_000)
        assert len(big) > 1024 * 1024

        response = await upload(client, big, "huge.wav")
        assert response.status_code == 413, response.text
        payload = response.json()
        assert payload["code"] == "PAYLOAD_TOO_LARGE"
        assert payload["detail"].get("suggestion")

    async def test_json_body_without_multipart_is_rejected(self, client):
        """Guard the failure mode that broke uploads in the UI.

        The frontend sent `Content-Type: application/json` (an interceptor
        overrode multipart), so the file field never arrived. The API must fail
        loudly with a validation error rather than accepting a bodyless request
        and reporting a bogus success.
        """
        response = await client.post(
            "/api/v1/media/inspect",
            json={"file": "not-a-file"},
        )
        assert response.status_code == 422, response.text
        payload = response.json()
        assert payload["code"] == "VALIDATION_ERROR"

    async def test_multipart_with_wrong_field_name_is_rejected(self, client):
        """The field name is part of the contract; 'upload' is not 'file'."""
        response = await client.post(
            "/api/v1/media/inspect",
            files={"upload": ("voice.wav", make_wav(duration_ms=500), "audio/wav")},
        )
        assert response.status_code == 422, response.text

    async def test_default_and_custom_trim_lengths(self, client):
        """Any window the user picks must be honoured, not a fixed length."""
        data = make_wav(duration_ms=30_000)
        created = await upload(client, data, "long.wav")
        assert created.status_code == 200, created.text
        media_id = created.json()["media_id"]

        for start_ms, end_ms in ((0, 3_000), (5_000, 8_000), (27_000, 30_000)):
            trimmed = await client.post(
                "/api/v1/media/trim",
                json={
                    "media_id": media_id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "output_format": "wav",
                },
            )
            assert trimmed.status_code == 200, trimmed.text
            duration = trimmed.json()["duration_ms"]
            assert abs(duration - (end_ms - start_ms)) < 200, (
                f"expected ~{end_ms - start_ms}ms, got {duration}ms"
            )

    async def test_wav_reports_duration_rate_and_channels(self, client):
        response = await upload(client, make_wav(duration_ms=1500), "voice.wav")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["kind"] == "audio"
        assert payload["format"] == "wav"
        assert payload["content_type"] == "audio/wav"
        assert payload["size_bytes"] > 44
        # Tolerance: header rounding, not a lossy format.
        assert abs(payload["duration_ms"] - 1500) <= 20
        assert payload["sample_rate"] == 16_000
        assert payload["channels"] == 1

    async def test_wav_is_trimmable_without_ffmpeg(self, client, monkeypatch):
        no_ffmpeg(monkeypatch)
        payload = (await upload(client, make_wav(), "voice.wav")).json()
        assert payload["trimmable"] is True
        assert payload["preview_url"] == f"/api/v1/media/{payload['media_id']}"

    async def test_stereo_wav_reports_two_channels(self, client):
        payload = (await upload(client, make_wav(channels=2), "stereo.wav")).json()
        assert payload["channels"] == 2

    async def test_unsupported_extension_is_400(self, client):
        response = await upload(client, b"not audio at all", "notes.txt")
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == ErrorCode.MEDIA_UNSUPPORTED_FORMAT.value
        assert body["detail"]["suggestion"]

    async def test_oversized_upload_is_413(self, client, settings, monkeypatch):
        """Oversized uploads are rejected with guidance.

        Which layer catches it depends on the size: the middleware rejects
        anything above `max_upload_mb` (PAYLOAD_TOO_LARGE) before the body is
        read, while the route itself raises MEDIA_TOO_LARGE. Both are correct,
        and both must carry an actionable suggestion.
        """
        monkeypatch.setattr(settings, "max_upload_mb", 0)
        response = await upload(client, make_wav(duration_ms=200), "big.wav")
        assert response.status_code == 413
        body = response.json()
        assert body["code"] in {
            ErrorCode.MEDIA_TOO_LARGE.value,
            "PAYLOAD_TOO_LARGE",
        }
        assert body["detail"].get("suggestion")

    async def test_video_without_ffmpeg_warns_and_is_not_trimmable(self, client, monkeypatch):
        no_ffmpeg(monkeypatch)
        response = await upload(client, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, "clip.mp4")
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "video"
        assert payload["trimmable"] is False
        assert payload["warnings"]
        assert any("ffmpeg" in item for item in payload["warnings"])

    async def test_mp3_duration_is_parsed_without_ffmpeg(self, client, monkeypatch):
        """MP3 frame parsing is pure Python, so ffmpeg must not be required."""
        no_ffmpeg(monkeypatch)
        # A single valid MPEG-1 Layer III frame header, repeated.
        frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
        payload = (await upload(client, frame * 40, "tone.mp3")).json()
        assert payload["kind"] == "audio"
        assert payload["duration_ms"] is not None
        assert payload["duration_ms"] > 0


# ---------------------------------------------------------------------------
# Trim
# ---------------------------------------------------------------------------
class TestMediaTrim:
    async def test_wav_trim_matches_requested_window(self, client):
        media_id = (await upload(client, make_wav(duration_ms=2000), "voice.wav")).json()[
            "media_id"
        ]

        response = await client.post(
            "/api/v1/media/trim",
            json={"media_id": media_id, "start_ms": 500, "end_ms": 1500, "output_format": "wav"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["format"] == "wav"
        assert payload["media_id"] != media_id
        # 1000 ms requested; WAV trimming is sample-exact.
        assert abs(payload["duration_ms"] - 1000) <= 50

        # The returned id must actually stream a shorter file.
        streamed = await client.get(f"/api/v1/media/{payload['media_id']}")
        assert streamed.status_code == 200
        assert len(streamed.content) < len(make_wav(duration_ms=2000))

    async def test_trimmed_wav_is_really_1000ms(self, client):
        media_id = (await upload(client, make_wav(duration_ms=3000), "voice.wav")).json()[
            "media_id"
        ]
        trimmed_id = (
            await client.post(
                "/api/v1/media/trim",
                json={"media_id": media_id, "start_ms": 1000, "end_ms": 2000},
            )
        ).json()["media_id"]

        raw = (await client.get(f"/api/v1/media/{trimmed_id}")).content
        with wave.open(io.BytesIO(raw), "rb") as handle:
            actual_ms = handle.getnframes() / handle.getframerate() * 1000
        assert abs(actual_ms - 1000) <= 50

    async def test_non_wav_without_ffmpeg_is_422_with_suggestion(self, client, monkeypatch):
        no_ffmpeg(monkeypatch)
        payload = (await upload(client, b"\xff\xfb\x90\x00" + b"\x00" * 400, "tone.mp3")).json()

        response = await client.post(
            "/api/v1/media/trim",
            json={"media_id": payload["media_id"], "start_ms": 0, "end_ms": 500},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == ErrorCode.MEDIA_TRIM_UNSUPPORTED.value
        assert "ffmpeg" in body["detail"]["suggestion"]
        assert body["detail"]["install_hint"]

    @pytest.mark.parametrize(
        "body",
        [
            {"start_ms": 1000, "end_ms": 1000},   # empty window
            {"start_ms": 2000, "end_ms": 1000},   # inverted
            {"start_ms": 0, "end_ms": 0},         # zero
        ],
    )
    async def test_invalid_range_is_422(self, client, body):
        media_id = (await upload(client, make_wav(), "voice.wav")).json()["media_id"]
        response = await client.post(
            "/api/v1/media/trim", json={"media_id": media_id, **body}
        )
        assert response.status_code == 422
        assert response.json()["code"] == ErrorCode.VALIDATION_ERROR.value

    async def test_window_longer_than_10_minutes_is_422(self, client):
        media_id = (await upload(client, make_wav(), "voice.wav")).json()["media_id"]
        response = await client.post(
            "/api/v1/media/trim",
            json={"media_id": media_id, "start_ms": 0, "end_ms": 600_001},
        )
        assert response.status_code == 422

    async def test_trim_unknown_media_is_404(self, client):
        response = await client.post(
            "/api/v1/media/trim",
            json={"media_id": "0" * 32, "start_ms": 0, "end_ms": 500},
        )
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.MEDIA_NOT_FOUND.value

    async def test_unsupported_output_format_is_422(self, client):
        media_id = (await upload(client, make_wav(), "voice.wav")).json()["media_id"]
        response = await client.post(
            "/api/v1/media/trim",
            json={"media_id": media_id, "start_ms": 0, "end_ms": 500, "output_format": "mp3"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
class TestMediaStreaming:
    async def test_full_download_has_correct_headers(self, client):
        data = make_wav(duration_ms=1000)
        payload = (await upload(client, data, "voice.wav")).json()

        response = await client.get(f"/api/v1/media/{payload['media_id']}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["accept-ranges"] == "bytes"
        assert len(response.content) == len(data)

    async def test_range_request_returns_206_and_100_bytes(self, client):
        data = make_wav(duration_ms=1000)
        payload = (await upload(client, data, "voice.wav")).json()

        response = await client.get(
            f"/api/v1/media/{payload['media_id']}", headers={"Range": "bytes=0-99"}
        )
        assert response.status_code == 206
        assert len(response.content) == 100
        assert response.headers["content-range"] == f"bytes 0-99/{len(data)}"
        assert response.content == data[:100]

    async def test_mid_file_range_is_correct(self, client):
        data = make_wav(duration_ms=800)
        payload = (await upload(client, data, "voice.wav")).json()

        response = await client.get(
            f"/api/v1/media/{payload['media_id']}", headers={"Range": "bytes=50-149"}
        )
        assert response.status_code == 206
        assert response.content == data[50:150]

    async def test_unsatisfiable_range_is_416(self, client):
        payload = (await upload(client, make_wav(), "voice.wav")).json()
        response = await client.get(
            f"/api/v1/media/{payload['media_id']}", headers={"Range": "bytes=999999-"}
        )
        assert response.status_code == 416

    async def test_unknown_media_is_404(self, client):
        response = await client.get(f"/api/v1/media/{'a' * 32}")
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == ErrorCode.MEDIA_NOT_FOUND.value
        assert body["detail"]["suggestion"]


# ---------------------------------------------------------------------------
# Delete + retention
# ---------------------------------------------------------------------------
class TestMediaDelete:
    async def test_delete_removes_file_and_sidecar(self, client, settings):
        payload = (await upload(client, make_wav(), "voice.wav")).json()
        media_id = payload["media_id"]

        response = await client.delete(f"/api/v1/media/{media_id}")
        assert response.status_code == 204

        assert (await client.get(f"/api/v1/media/{media_id}")).status_code == 404
        assert not (settings.media_path / f"{media_id}.json").exists()
        assert not (settings.media_path / f"{media_id}.wav").exists()

    async def test_delete_is_idempotent(self, client):
        payload = (await upload(client, make_wav(), "voice.wav")).json()
        assert (await client.delete(f"/api/v1/media/{payload['media_id']}")).status_code == 204
        assert (await client.delete(f"/api/v1/media/{payload['media_id']}")).status_code == 204

    async def test_purge_older_than_removes_stale_uploads(self, client, settings):
        from app.services.media import media_store

        payload = (await upload(client, make_wav(), "voice.wav")).json()
        media_id = payload["media_id"]
        assert (settings.media_path / f"{media_id}.json").exists()

        # Nothing is old enough yet.
        assert media_store.purge_older_than(days=7) == 0
        # days=0 is a no-op guard rather than "delete everything".
        assert media_store.purge_older_than(days=0) == 0

        # Rewrite the sidecar as if it were 30 days old.
        import json as _json
        import time

        sidecar = settings.media_path / f"{media_id}.json"
        data = _json.loads(sidecar.read_text(encoding="utf-8"))
        data["created_at"] = time.time() - 30 * 86_400
        sidecar.write_text(_json.dumps(data), encoding="utf-8")

        assert media_store.purge_older_than(days=7) == 1
        assert not sidecar.exists()


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------
class TestMediaStoreSafety:
    """Traversal defence: ids are sanitized, and nothing escapes the root.

    ``_resolve`` neutralizes a hostile id by stripping separators rather than
    raising on every odd input, so the property worth asserting is *containment*
    plus rejection of ids that sanitize to nothing.
    """

    @pytest.mark.parametrize(
        "hostile_id",
        ["../../etc/passwd", "a/../../b", "....//....//x", "C:\\Windows\\system32\\cfg"],
    )
    def test_hostile_ids_stay_inside_the_media_root(self, tmp_media_store, hostile_id):
        resolved = tmp_media_store._resolve(hostile_id, "wav")
        root = tmp_media_store.root.resolve()
        assert resolved.parent == root, f"{hostile_id!r} escaped the media root"

    @pytest.mark.parametrize("empty_id", ["..", "...", "/", "\\", ""])
    def test_ids_that_sanitize_to_nothing_are_refused(self, tmp_media_store, empty_id):
        from app.core.errors import AppError

        with pytest.raises(AppError) as excinfo:
            tmp_media_store._resolve(empty_id, "wav")
        assert excinfo.value.code is ErrorCode.MEDIA_NOT_FOUND

    async def test_media_id_is_32_hex_chars(self, client):
        payload = (await upload(client, make_wav(), "voice.wav")).json()
        media_id = payload["media_id"]
        assert len(media_id) == 32
        assert all(ch in "0123456789abcdef" for ch in media_id)

    async def test_sidecar_lives_next_to_the_file(self, client, settings):
        media_id = (await upload(client, make_wav(), "voice.wav")).json()["media_id"]
        assert (settings.media_path / f"{media_id}.json").is_file()
        assert (settings.media_path / f"{media_id}.wav").is_file()
