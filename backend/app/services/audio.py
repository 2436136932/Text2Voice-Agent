"""Audio persistence and duration analysis.

Files are written to ``settings.audio_path`` as
``<record_id>_<provider>_<voice>.<ext>``. Only the *relative* path is stored in
the database, so the whole ``data/`` directory can be moved to another disk
(or to a network share) without touching any row.

Duration is derived from the bytes themselves rather than trusted from the
vendor, because:

* most providers do not report it at all;
* the UI needs a stable number for the history table;
* a wrong number is worse than a computed one, and we can compute it exactly
  for WAV and near-exactly for MP3/OGG.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.security import sanitize_filename

logger = get_logger(__name__)

#: MPEG audio bitrate tables (kbps), indexed by [version][layer][bitrate_index]
_MP3_BITRATES = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MP3_SAMPLE_RATES = {1: [44100, 48000, 32000], 2: [22050, 24000, 16000], 2.5: [11025, 12000, 8000]}

EXTENSION_BY_FORMAT = {
    "mp3": "mp3",
    "wav": "wav",
    "opus": "opus",
    "ogg": "ogg",
    "aac": "aac",
    "flac": "flac",
    "pcm": "pcm",
    "webm": "webm",
    "ulaw": "ulaw",
}


# ---------------------------------------------------------------------------
# Duration estimation
# ---------------------------------------------------------------------------
def _wav_duration_ms(data: bytes) -> int | None:
    """Exact duration for a RIFF/WAVE payload."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    offset = 12
    byte_rate = 0
    data_size = 0
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        body = offset + 8
        if chunk_id == b"fmt " and chunk_size >= 16:
            _, channels, sample_rate, byte_rate, _, bits = struct.unpack_from("<HHIIHH", data, body)
            if byte_rate == 0 and channels and sample_rate:
                byte_rate = channels * sample_rate * max(1, bits // 8)
        elif chunk_id == b"data":
            data_size = min(chunk_size, len(data) - body)
            break
        offset = body + chunk_size + (chunk_size % 2)
    if byte_rate <= 0 or data_size <= 0:
        return None
    return int(round(data_size / byte_rate * 1000))


def _mp3_duration_ms(data: bytes) -> int | None:
    """Parse MPEG audio frames; falls back to a bitrate estimate."""
    start = 0
    # Skip an ID3v2 tag when present.
    if data[:3] == b"ID3" and len(data) > 10:
        size = (
            (data[6] & 0x7F) << 21
            | (data[7] & 0x7F) << 14
            | (data[8] & 0x7F) << 7
            | (data[9] & 0x7F)
        )
        start = 10 + size

    offset = start
    total_samples = 0
    sample_rate = 0
    frames = 0
    limit = min(len(data), start + 4 * 1024 * 1024)  # cap the scan at 4 MB

    while offset + 4 <= limit:
        header = data[offset : offset + 4]
        if header[0] != 0xFF or (header[1] & 0xE0) != 0xE0:
            offset += 1
            continue
        version_bits = (header[1] >> 3) & 0x03
        layer_bits = (header[1] >> 1) & 0x03
        bitrate_index = (header[2] >> 4) & 0x0F
        rate_index = (header[2] >> 2) & 0x03
        padding = (header[2] >> 1) & 0x01

        if version_bits == 1 or layer_bits == 0 or bitrate_index in (0, 15) or rate_index == 3:
            offset += 1
            continue

        version = {0: 2.5, 2: 2, 3: 1}[version_bits]
        layer = {1: 3, 2: 2, 3: 1}[layer_bits]
        table_version = 1 if version == 1 else 2
        bitrate = _MP3_BITRATES.get((table_version, layer), [0] * 15)[bitrate_index] * 1000
        rate = _MP3_SAMPLE_RATES[version][rate_index]
        if not bitrate or not rate:
            offset += 1
            continue

        if layer == 1:
            frame_size = int((12 * bitrate / rate + padding) * 4)
            samples = 384
        elif layer == 2:
            frame_size = int(144 * bitrate / rate + padding)
            samples = 1152
        else:  # layer 3
            if version == 1:
                frame_size = int(144 * bitrate / rate + padding)
                samples = 1152
            else:
                frame_size = int(72 * bitrate / rate + padding)
                samples = 576

        if frame_size <= 4 or offset + frame_size > len(data):
            break
        total_samples += samples
        sample_rate = rate
        frames += 1
        offset += frame_size

    if frames >= 4 and sample_rate:
        return int(round(total_samples / sample_rate * 1000))

    # Very short files / malformed headers: estimate from the average bitrate.
    if frames >= 1 and sample_rate and frames > 0:
        return int(round(total_samples / sample_rate * 1000))
    if len(data) > 0:
        # Assume a typical 128 kbps MP3 stream.
        return int(round(len(data) * 8 / 128_000 * 1000))
    return None


def _ogg_duration_ms(data: bytes) -> int | None:
    """Read the granule position of the last Ogg page (48 kHz for Opus)."""
    if not data.startswith(b"OggS"):
        return None
    index = data.rfind(b"OggS")
    if index < 0 or index + 27 > len(data):
        return None
    granule = struct.unpack_from("<q", data, index + 6)[0]
    if granule <= 0:
        return None
    return int(round(granule / 48_000 * 1000))


def _pcm_duration_ms(data: bytes, *, sample_rate: int = 24_000, channels: int = 1, width: int = 2) -> int:
    frames = len(data) / (channels * width)
    return int(round(frames / sample_rate * 1000))


def estimate_duration_ms(data: bytes, audio_format: str = "mp3") -> int | None:
    """Best-effort duration of an audio payload, in milliseconds."""
    if not data:
        return None
    fmt = (audio_format or "").lower()
    try:
        if fmt == "wav":
            return _wav_duration_ms(data)
        if fmt in {"mp3", "mp3_22050"}:
            return _mp3_duration_ms(data)
        if fmt in {"ogg", "opus", "webm"}:
            return _ogg_duration_ms(data) or _mp3_duration_ms(data)
        if fmt == "flac":
            if len(data) > 42 and data[:4] == b"fLaC":
                # STREAMINFO: sample rate and total samples are packed here.
                header = data[8:42]
                sample_rate = (header[10] << 12) | (header[11] << 4) | (header[12] >> 4)
                total = (
                    ((header[13] & 0x0F) << 32)
                    | (header[14] << 24)
                    | (header[15] << 16)
                    | (header[16] << 8)
                    | header[17]
                )
                if sample_rate and total:
                    return int(round(total / sample_rate * 1000))
            return None
        if fmt == "pcm":
            return _pcm_duration_ms(data)
        if fmt in {"aac", "ulaw"}:
            # 16 kHz μ-law / AAC-LC at 128 kbps is close enough for a label.
            return int(round(len(data) * 8 / 128_000 * 1000))
    except Exception as exc:  # noqa: BLE001 - never fail a generation over metadata
        logger.warning("duration estimation failed format=%s error=%s", fmt, exc)
    return None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class AudioStore:
    """Filesystem-backed audio storage."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        path = self._root or settings.audio_path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def build_filename(self, *, record_id: int, provider: str, voice: str, audio_format: str) -> str:
        extension = EXTENSION_BY_FORMAT.get((audio_format or "mp3").lower(), "bin")
        stem = sanitize_filename(f"{record_id}_{provider}_{voice}", fallback=f"audio_{record_id}")
        return f"{stem}.{extension}"

    def save(
        self,
        *,
        record_id: int,
        provider: str,
        voice: str,
        audio_format: str,
        data: bytes,
    ) -> str:
        """Write ``data`` and return the path **relative to the audio root**."""
        filename = self.build_filename(
            record_id=record_id, provider=provider, voice=voice, audio_format=audio_format
        )
        target = self.root / filename
        # Guard against any path escape, however unlikely.
        resolved = target.resolve()
        if self.root.resolve() not in resolved.parents and resolved.parent != self.root.resolve():
            raise AppError(
                ErrorCode.STORAGE_ERROR,
                "音频文件路径非法。",
                detail={"filename": filename},
            )
        tmp = resolved.with_suffix(resolved.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(resolved)
        logger.info(
            "audio saved record_id=%s file=%s bytes=%s format=%s",
            record_id, filename, len(data), audio_format,
        )
        return filename

    def path_for(self, relative_path: str) -> Path | None:
        """Resolve a stored relative path, refusing traversal attempts."""
        if not relative_path:
            return None
        candidate = (self.root / relative_path).resolve()
        root = self.root.resolve()
        if root != candidate and root not in candidate.parents:
            logger.warning("rejected audio path outside root path=%s", relative_path)
            return None
        return candidate if candidate.is_file() else None

    def delete(self, relative_path: str) -> bool:
        path = self.path_for(relative_path)
        if path is None:
            return False
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning("failed to delete audio file=%s error=%s", relative_path, exc)
            return False

    def total_bytes(self) -> int:
        return sum(item.stat().st_size for item in self.root.glob("*") if item.is_file())

    def count(self) -> int:
        return sum(1 for item in self.root.glob("*") if item.is_file())


audio_store = AudioStore()