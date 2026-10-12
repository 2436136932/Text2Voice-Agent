"""Media upload, inspection and trimming.

The reference clip for voice cloning arrives as an arbitrary user file: WAV,
MP3, or a video (MP4/MOV/WebM). Handling that without a heavy dependency means:

* **WAV** — parsed and trimmed with the standard library's :mod:`wave`. Exact,
  fast and dependency-free.
* **everything else** — duration is read from the frame headers when we can
  (:func:`app.services.audio.estimate_duration_ms` handles MP3/OGG/FLAC), and
  trimming/transcoding goes through **ffmpeg when it is installed**. When it is
  not, we raise ``MEDIA_TRIM_UNSUPPORTED`` with an install hint instead of
  failing obscurely.

Every file is stored as ``data/media/{media_id}.{ext}`` with a JSON sidecar so
metadata survives a restart without a database round-trip.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.services.audio import estimate_duration_ms
from app.services.tools import INSTALL_HINT, ffmpeg_status, ffprobe_path

logger = get_logger(__name__)

#: Extensions we accept for upload, mapped to (kind, content-type).
ACCEPTED_FORMATS: dict[str, tuple[str, str]] = {
    "wav": ("audio", "audio/wav"),
    "mp3": ("audio", "audio/mpeg"),
    "m4a": ("audio", "audio/mp4"),
    "aac": ("audio", "audio/aac"),
    "ogg": ("audio", "audio/ogg"),
    "opus": ("audio", "audio/ogg"),
    "flac": ("audio", "audio/flac"),
    "webm": ("video", "video/webm"),
    "mp4": ("video", "video/mp4"),
    "mov": ("video", "video/quicktime"),
    "mkv": ("video", "video/x-matroska"),
    "avi": ("video", "video/x-msvideo"),
}

#: Formats whose duration the backend can read without ffmpeg.
NATIVE_DURATION_FORMATS = frozenset({"wav", "mp3", "ogg", "opus", "flac"})

#: Formats the stdlib can trim.
NATIVE_TRIM_FORMATS = frozenset({"wav"})

#: Hard cap on a single trim window (10 minutes).
MAX_TRIM_WINDOW_MS = 600_000


def accepted_extensions() -> list[str]:
    return sorted(ACCEPTED_FORMATS)


def accept_attribute() -> str:
    """Value for an ``<input accept="...">`` attribute."""
    return ",".join(f".{ext}" for ext in accepted_extensions())


@dataclass(slots=True)
class MediaRecord:
    """Metadata for one stored upload."""

    media_id: str
    filename: str
    extension: str
    kind: str
    content_type: str
    size_bytes: int
    duration_ms: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    created_at: float = field(default_factory=time.time)
    warnings: list[str] = field(default_factory=list)
    #: Media id this file was derived from (set by trim).
    source_media_id: str | None = None

    @property
    def format(self) -> str:
        return self.extension

    @property
    def trimmable(self) -> bool:
        if self.extension in NATIVE_TRIM_FORMATS:
            return True
        return ffmpeg_status().available

    @property
    def preview_url(self) -> str:
        return f"/api/v1/media/{self.media_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "format": self.format,
            "trimmable": self.trimmable,
            "preview_url": self.preview_url,
            "warnings": list(self.warnings),
        }


def probe_wav(data: bytes) -> dict[str, Any]:
    """Read sample rate / channel count / duration from a WAV payload."""
    result: dict[str, Any] = {"sample_rate": None, "channels": None, "duration_ms": None}
    import io

    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            frames = handle.getnframes()
            result["sample_rate"] = rate
            result["channels"] = channels
            if rate:
                result["duration_ms"] = int(round(frames / rate * 1000))
    except (wave.Error, EOFError, OSError) as exc:
        logger.warning("wav probe failed: %s", exc)
    return result


def _ffprobe_duration_ms(path: Path) -> int | None:
    """Ask ffprobe for a duration (used for video containers)."""
    binary = ffprobe_path()
    if not binary:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv
            [
                binary,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        raw = (completed.stdout or "").strip()
        if raw:
            return int(round(float(raw) * 1000))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("ffprobe failed for %s: %s", path.name, exc)
    return None


class MediaStore:
    """Filesystem store for uploaded reference clips."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        path = self._root or settings.media_path
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- paths --------------------------------------------------------------
    def _resolve(self, media_id: str, extension: str) -> Path:
        safe_id = "".join(ch for ch in media_id if ch.isalnum() or ch in "-_")
        if not safe_id:
            raise AppError(ErrorCode.MEDIA_NOT_FOUND, "媒体标识不合法。", http_status=404)
        candidate = (self.root / f"{safe_id}.{extension}").resolve()
        root = self.root.resolve()
        if root != candidate and root not in candidate.parents:
            raise AppError(ErrorCode.MEDIA_NOT_FOUND, "媒体路径非法。", http_status=404)
        return candidate

    def path_for(self, record: MediaRecord) -> Path:
        return self._resolve(record.media_id, record.extension)

    def _sidecar(self, media_id: str) -> Path:
        return self.root / f"{media_id}.json"

    # -- write / read -------------------------------------------------------
    def save(
        self,
        *,
        data: bytes,
        filename: str,
        extension: str,
        kind: str,
        content_type: str,
        duration_ms: int | None,
        sample_rate: int | None,
        channels: int | None,
        warnings: list[str] | None = None,
        source_media_id: str | None = None,
    ) -> MediaRecord:
        media_id = uuid.uuid4().hex
        record = MediaRecord(
            media_id=media_id,
            filename=filename,
            extension=extension,
            kind=kind,
            content_type=content_type,
            size_bytes=len(data),
            duration_ms=duration_ms,
            sample_rate=sample_rate,
            channels=channels,
            warnings=list(warnings or []),
            source_media_id=source_media_id,
        )
        target = self._resolve(media_id, extension)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        self._sidecar(media_id).write_text(
            json.dumps(asdict(record), ensure_ascii=False), encoding="utf-8"
        )
        logger.info(
            "media stored id=%s kind=%s format=%s bytes=%s duration_ms=%s",
            media_id, kind, extension, len(data), duration_ms,
        )
        return record

    def load(self, media_id: str) -> MediaRecord:
        sidecar = self._sidecar(media_id)
        if not sidecar.is_file():
            raise AppError(
                ErrorCode.MEDIA_NOT_FOUND,
                f"媒体文件不存在或已过期（{media_id[:8]}…）。请重新上传。",
                detail={"media_id": media_id, "suggestion": "请重新上传参考音频/视频。"},
                http_status=404,
            )
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AppError(
                ErrorCode.MEDIA_NOT_FOUND,
                "媒体元数据损坏，请重新上传。",
                detail={"media_id": media_id},
                http_status=404,
            ) from exc
        payload.setdefault("warnings", [])
        return MediaRecord(**payload)

    def read_bytes(self, record: MediaRecord) -> bytes:
        path = self.path_for(record)
        if not path.is_file():
            raise AppError(
                ErrorCode.MEDIA_NOT_FOUND,
                "媒体文件已丢失，请重新上传。",
                detail={"media_id": record.media_id},
                http_status=404,
            )
        return path.read_bytes()

    def delete(self, media_id: str) -> bool:
        try:
            record = self.load(media_id)
        except AppError:
            return False
        removed = False
        path = self.path_for(record)
        if path.is_file():
            path.unlink()
            removed = True
        self._sidecar(media_id).unlink(missing_ok=True)
        return removed

    def purge_older_than(self, days: int) -> int:
        """Retention job for scratch uploads."""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86_400
        removed = 0
        for sidecar in self.root.glob("*.json"):
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
                created = float(payload.get("created_at", 0))
            except (OSError, ValueError):
                continue
            if created and created < cutoff:
                media_id = str(payload.get("media_id", ""))
                extension = str(payload.get("extension", ""))
                if media_id and extension:
                    self._resolve(media_id, extension).unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.info("media retention purge removed=%s older_than_days=%s", removed, days)
        return removed


media_store = MediaStore()


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------
def inspect_upload(data: bytes, filename: str) -> MediaRecord:
    """Validate and classify an uploaded file.

    Raises ``MEDIA_UNSUPPORTED_FORMAT`` / ``MEDIA_TOO_LARGE`` with actionable
    messages rather than letting the problem surface later.
    """
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise AppError(
            ErrorCode.MEDIA_TOO_LARGE,
            f"文件过大（{len(data) / 1048576:.1f} MB），上限 {settings.max_upload_mb} MB。",
            detail={
                "size_bytes": len(data),
                "max_bytes": max_bytes,
                "suggestion": "请裁剪出 5–30 秒的高质量片段后再上传，过长的参考音频对克隆效果没有帮助。",
            },
        )
    if not data:
        raise AppError(
            ErrorCode.MEDIA_UNSUPPORTED_FORMAT,
            "上传的文件是空的，请重新选择。",
            detail={"suggestion": "请确认选择的文件内容完整。"},
        )

    extension = Path(filename or "").suffix.lstrip(".").lower()
    if extension not in ACCEPTED_FORMATS:
        raise AppError(
            ErrorCode.MEDIA_UNSUPPORTED_FORMAT,
            f"不支持的文件格式 “{extension or '未知'}”。",
            detail={
                "extension": extension,
                "supported": accepted_extensions(),
                "suggestion": (
                    f"支持以下格式：{', '.join(accepted_extensions())}。"
                    "如果只有其他格式，请先转换为 WAV 或 MP3 再上传。"
                ),
            },
        )

    kind, content_type = ACCEPTED_FORMATS[extension]
    warnings: list[str] = []
    sample_rate: int | None = None
    channels: int | None = None
    duration_ms: int | None = None

    if extension == "wav":
        probed = probe_wav(data)
        sample_rate = probed["sample_rate"]
        channels = probed["channels"]
        duration_ms = probed["duration_ms"]
        if duration_ms is None:
            warnings.append("无法解析 WAV 头部信息，文件可能已损坏。")
    else:
        duration_ms = estimate_duration_ms(data, extension)
        if kind == "video":
            ffmpeg = ffmpeg_status()
            if not ffmpeg.available:
                warnings.append(
                    "视频需要 ffmpeg 才能提取音轨与精确时长；当前环境未安装，"
                    "仍然可以上传，但部分功能受限。"
                )
            elif duration_ms is None:
                # We have ffmpeg but the bytes are not a parseable audio stream:
                # write the file first so ffprobe can read it.
                duration_ms = None
        if duration_ms is None:
            warnings.append("无法自动识别时长，如需精确裁剪请先转为 WAV。")

    return media_store.save(
        data=data,
        filename=filename or f"upload.{extension}",
        extension=extension,
        kind=kind,
        content_type=content_type,
        duration_ms=duration_ms,
        sample_rate=sample_rate,
        channels=channels,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------
def _trim_wav_native(data: bytes, start_ms: int, end_ms: int) -> tuple[bytes, int]:
    """Trim a WAV payload with the standard library."""
    import io

    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            params = source.getparams()
            rate = source.getframerate()
            total = source.getnframes()
            start_frame = max(0, int(rate * start_ms / 1000))
            end_frame = min(total, int(rate * end_ms / 1000))
            if end_frame <= start_frame:
                raise AppError(
                    ErrorCode.MEDIA_INVALID_RANGE,
                    "裁剪区间为空，请重新选择起止时间。",
                    detail={"start_ms": start_ms, "end_ms": end_ms},
                )
            source.setpos(start_frame)
            frames = source.readframes(end_frame - start_frame)
    except wave.Error as exc:
        raise AppError(
            ErrorCode.MEDIA_UNSUPPORTED_FORMAT,
            "WAV 文件解析失败，请确认文件未损坏。",
            detail={"suggestion": "请重新导出为标准的 PCM WAV 文件后再上传。"},
        ) from exc

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setparams(params)
        target.writeframes(frames)
    payload = buffer.getvalue()
    duration_ms = int(round((end_frame - start_frame) / rate * 1000)) if rate else 0
    return payload, duration_ms


def _trim_with_ffmpeg(source: Path, start_ms: int, end_ms: int) -> bytes:
    """Transcode an arbitrary container to a trimmed 16 kHz mono WAV."""
    status = ffmpeg_status()
    if not status.available or not status.path:
        raise AppError(
            ErrorCode.MEDIA_TRIM_UNSUPPORTED,
            f"当前环境未安装 ffmpeg，无法裁剪 .{source.suffix.lstrip('.')} 文件。",
            detail={
                "install_hint": INSTALL_HINT,
                "suggestion": (
                    "请改用 WAV 格式的参考音频（可直接裁剪），"
                    f"或安装 ffmpeg 后重试：{INSTALL_HINT}"
                ),
            },
        )

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        output = Path(handle.name)

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                status.path,
                "-y",
                "-loglevel", "error",
                "-ss", f"{start_ms / 1000:.3f}",
                "-to", f"{end_ms / 1000:.3f}",
                "-i", str(source),
                "-vn",
                "-acodec", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0 or not output.is_file():
            logger.error("ffmpeg trim failed rc=%s stderr=%s", completed.returncode, completed.stderr[:400])
            raise AppError(
                ErrorCode.MEDIA_TRIM_UNSUPPORTED,
                "裁剪失败：ffmpeg 无法处理该文件。",
                detail={
                    "returncode": completed.returncode,
                    "suggestion": "请确认文件未损坏，或先转换为 WAV 后重试。",
                },
            )
        return output.read_bytes()
    except subprocess.TimeoutExpired as exc:
        raise AppError(
            ErrorCode.MEDIA_TRIM_UNSUPPORTED,
            "裁剪超时，文件可能过大。",
            detail={"suggestion": "请上传更短的片段（建议 5–30 秒）。"},
        ) from exc
    finally:
        output.unlink(missing_ok=True)


def trim_media(media_id: str, start_ms: int, end_ms: int) -> MediaRecord:
    """Cut ``[start_ms, end_ms)`` out of an uploaded file into a new WAV."""
    record = media_store.load(media_id)

    if start_ms < 0 or end_ms <= start_ms:
        raise AppError(
            ErrorCode.MEDIA_INVALID_RANGE,
            "裁剪区间不合法：结束时间必须大于开始时间。",
            detail={"start_ms": start_ms, "end_ms": end_ms, "suggestion": "请重新拖动滑块选择区间。"},
        )
    if end_ms - start_ms > MAX_TRIM_WINDOW_MS:
        raise AppError(
            ErrorCode.MEDIA_INVALID_RANGE,
            f"裁剪区间过长（上限 {MAX_TRIM_WINDOW_MS // 60000} 分钟）。",
            detail={"start_ms": start_ms, "end_ms": end_ms},
        )
    if record.duration_ms is not None and start_ms >= record.duration_ms:
        raise AppError(
            ErrorCode.MEDIA_INVALID_RANGE,
            "开始时间超出了文件时长。",
            detail={
                "start_ms": start_ms,
                "duration_ms": record.duration_ms,
                "suggestion": "请把开始时间拖到文件时长以内。",
            },
        )

    data = media_store.read_bytes(record)

    if record.extension in NATIVE_TRIM_FORMATS:
        trimmed, duration_ms = _trim_wav_native(data, start_ms, end_ms)
    else:
        trimmed = _trim_with_ffmpeg(media_store.path_for(record), start_ms, end_ms)
        probed = probe_wav(trimmed)
        duration_ms = probed["duration_ms"] or (end_ms - start_ms)

    new_record = media_store.save(
        data=trimmed,
        filename=f"trimmed_{Path(record.filename).stem}.wav",
        extension="wav",
        kind="audio",
        content_type="audio/wav",
        duration_ms=duration_ms,
        sample_rate=16000,
        channels=1,
        warnings=[],
        source_media_id=record.media_id,
    )
    return new_record