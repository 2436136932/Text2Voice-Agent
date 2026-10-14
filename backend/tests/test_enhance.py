"""Reference-audio enhancement: loudness normalization + silence trimming."""

from __future__ import annotations

import math
import struct
import wave

import pytest

from app.core.errors import AppError, ErrorCode
from app.services.media import enhance_media, media_store
from app.services.tools import ffmpeg_status

pytestmark = pytest.mark.asyncio

RATE = 24_000


def wav_bytes(tone_seconds: float, *, amplitude: int = 20000, lead_silence: float = 0.0, tail_silence: float = 0.0) -> bytes:
    """Build a mono 16-bit WAV: lead silence + tone + tail silence."""
    lead = int(lead_silence * RATE)
    tail = int(tail_silence * RATE)
    tone = int(tone_seconds * RATE)
    frames = [0] * lead
    for i in range(tone):
        frames.append(int(amplitude * math.sin(2 * math.pi * 440 * i / RATE)))
    frames.extend([0] * tail)
    buf = b"".join(struct.pack("<h", max(-32768, min(32767, s))) for s in frames)
    header = b"RIFF" + struct.pack("<I", 36 + len(buf)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16
    ) + b"data" + struct.pack("<I", len(buf))
    return header + buf


def requires_ffmpeg() -> None:
    status = ffmpeg_status()
    if not status.available:
        pytest.skip("ffmpeg not installed on this machine")


class TestEnhance:
    async def test_strips_leading_trailing_silence(self, tmp_path, monkeypatch):
        """2s silence + 3s tone + 2s silence must come out ≈ the 3s tone."""
        requires_ffmpeg()
        monkeypatch.setattr(media_store, "_root", tmp_path / "media")
        record = media_store.save(
            data=wav_bytes(3.0, lead_silence=2.0, tail_silence=2.0),
            filename="ref.wav",
            extension="wav",
            kind="audio",
            content_type="audio/wav",
            duration_ms=7000,
            sample_rate=RATE,
            channels=1,
        )
        enhanced = enhance_media(record.media_id)
        assert enhanced.media_id != record.media_id  # new record, source intact
        assert enhanced.duration_ms is not None
        # Silences removed: meaningfully shorter than the 7 s input...
        assert enhanced.duration_ms < 7000
        # ...but the tone survived: close to 3 s plus small padding.
        assert 2500 <= enhanced.duration_ms <= 3500

    async def test_normalized_clip_is_louder_than_quiet_source(self, tmp_path, monkeypatch):
        """A very quiet source comes out at a healthy level after loudnorm."""
        requires_ffmpeg()
        monkeypatch.setattr(media_store, "_root", tmp_path / "media")
        quiet = wav_bytes(3.0, amplitude=300)
        record = media_store.save(
            data=quiet,
            filename="quiet.wav",
            extension="wav",
            kind="audio",
            content_type="audio/wav",
            duration_ms=3000,
            sample_rate=RATE,
            channels=1,
        )
        enhanced = enhance_media(record.media_id)
        enhanced_bytes = media_store.read_bytes(enhanced)
        peak_source = max(
            (abs(s[0]) for s in struct.iter_unpack("<h", quiet[44:])), default=0
        )
        with wave.open(io := __import__("io").BytesIO(enhanced_bytes), "rb") as handle:
            frames = handle.readframes(handle.getnframes())
        peak_enhanced = max((abs(s[0]) for s in struct.iter_unpack("<h", frames)), default=0)
        # Loudnorm at -16 LUFS should lift a near-silent tone dramatically.
        assert peak_enhanced > peak_source

    async def test_all_silence_is_rejected(self, tmp_path, monkeypatch):
        requires_ffmpeg()
        monkeypatch.setattr(media_store, "_root", tmp_path / "media")
        record = media_store.save(
            data=wav_bytes(3.0, amplitude=0),
            filename="silent.wav",
            extension="wav",
            kind="audio",
            content_type="audio/wav",
            duration_ms=3000,
            sample_rate=RATE,
            channels=1,
        )
        with pytest.raises(AppError) as excinfo:
            enhance_media(record.media_id)
        # Either the empty-output guard or the too-short guard fires.
        assert excinfo.value.code in {ErrorCode.MEDIA_TRIM_UNSUPPORTED, ErrorCode.MEDIA_INVALID_RANGE}

    async def test_source_record_untouched(self, tmp_path, monkeypatch):
        requires_ffmpeg()
        monkeypatch.setattr(media_store, "_root", tmp_path / "media")
        record = media_store.save(
            data=wav_bytes(2.0),
            filename="src.wav",
            extension="wav",
            kind="audio",
            content_type="audio/wav",
            duration_ms=2000,
            sample_rate=RATE,
            channels=1,
        )
        before = media_store.read_bytes(record)
        enhance_media(record.media_id)
        after = media_store.read_bytes(record)
        assert before == after
