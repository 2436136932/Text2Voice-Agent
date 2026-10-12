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
        monkeypatch.setattr(settings, "max_upload_mb", 0)
        response = await upload(client, make_wav(duration_ms=200), "big.wav")
        assert response.status_code == 413
        body = response.json()
        assert body["code"] == ErrorCode.MEDIA_TOO_LARGE.value
        assert "suggestion" in body["detail"]

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
