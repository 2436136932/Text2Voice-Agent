"""External tool probing (currently: ffmpeg).

Video decoding and non-WAV transcoding need ffmpeg, which is **not** guaranteed
to be installed. Rather than failing deep inside a request with an opaque
error, the platform probes once and reports the result through
``GET /api/v1/capabilities`` so the UI can hide or explain the affected
features.

The probe is cached briefly because it shells out to ``ffmpeg -version``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

#: How long a probe result is trusted (seconds).
PROBE_TTL_SECONDS = 30

#: Common install locations checked when the binary is not on PATH. Windows
#: users frequently install via winget/scoop/choco or a portable zip without a
#: fresh shell, so PATH alone is not a reliable signal.
_FALLBACK_CANDIDATES = (
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
)

#: Globs relative to the user profile, covering the portable build layouts:
#:   %LOCALAPPDATA%\ffmpeg\bin\ffmpeg.exe                (flat extract)
#:   %LOCALAPPDATA%\ffmpeg\ffmpeg-<ver>-build\bin\...    (zip keeps its folder)
#: plus scoop's shim and app directories.
_USER_RELATIVE_GLOBS = (
    "AppData/Local/ffmpeg/bin/ffmpeg.exe",
    "AppData/Local/ffmpeg/ffmpeg-*/bin/ffmpeg.exe",
    "ffmpeg/bin/ffmpeg.exe",
    "ffmpeg/ffmpeg-*/bin/ffmpeg.exe",
    "scoop/shims/ffmpeg.exe",
    "scoop/apps/ffmpeg/current/bin/ffmpeg.exe",
)

INSTALL_HINT = "winget install Gyan.FFmpeg"


@dataclass(slots=True)
class ToolStatus:
    """Result of probing an external binary."""

    available: bool
    path: str | None = None
    version: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "path": self.path,
            "version": self.version,
            "message": self.message,
        }


@dataclass(slots=True)
class MediaProbe:
    """Duration and stream kinds read from a container."""

    duration_ms: int | None = None
    streams: list[str] = field(default_factory=list)

    @property
    def has_audio(self) -> bool:
        return "audio" in self.streams

    @property
    def has_video(self) -> bool:
        return "video" in self.streams


_cache: dict[str, tuple[float, ToolStatus]] = {}


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if text and text.strip() else ""


def _from_user_globs(name: str) -> str | None:
    """Search the user profile for a portable install of ``name``.

    Covers the common "downloaded a zip and extracted it" case, which leaves no
    PATH entry and no registry footprint.
    """
    import glob
    import os

    home = Path(os.path.expanduser("~"))
    for pattern in _USER_RELATIVE_GLOBS:
        if name.lower() not in pattern.lower():
            continue
        for match in sorted(glob.glob(str(home / pattern)), reverse=True):
            if Path(match).is_file():
                return match
    return None


def _probe_binary(name: str) -> ToolStatus:
    """Locate a binary on PATH, in known locations, or in the user profile."""
    found = shutil.which(name)
    if not found:
        for candidate in _FALLBACK_CANDIDATES:
            if Path(candidate).name.lower().startswith(name.lower()) and Path(candidate).is_file():
                found = candidate
                break
    if not found:
        found = _from_user_globs(name)
    if not found:
        return ToolStatus(
            available=False,
            message=(
                f"未检测到 {name}，无法提取视频音轨或裁剪非 WAV 文件。"
                f"安装后自动启用：{INSTALL_HINT}。"
                "若已下载压缩包，解压到任意位置后重启服务即可自动识别。"
            ),
        )

    version: str | None = None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [found, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version = _first_line(completed.stdout) or _first_line(completed.stderr) or None
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env dependent
        logger.warning("failed to read %s version: %s", name, exc)

    return ToolStatus(
        available=True,
        path=found,
        version=version,
        message=f"{name} 可用：{version or '已安装'}",
    )


def ffmpeg_status(*, force: bool = False) -> ToolStatus:
    """Cached ffmpeg probe."""
    now = time.monotonic()
    cached = _cache.get("ffmpeg")
    if cached and not force and now - cached[0] < PROBE_TTL_SECONDS:
        return cached[1]
    status = _probe_binary("ffmpeg")
    _cache["ffmpeg"] = (now, status)
    if status.available:
        logger.info("ffmpeg detected path=%s", status.path)
    else:
        logger.info("ffmpeg not available; WAV-only media pipeline")
    return status


def ffprobe_path() -> str | None:
    """Path to ffprobe when present (used to read duration of exotic formats)."""
    found = shutil.which("ffprobe")
    if found:
        return found
    ffmpeg = ffmpeg_status()
    if ffmpeg.available and ffmpeg.path:
        # ffprobe normally sits next to ffmpeg.
        sibling = Path(ffmpeg.path).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"
        )
        if sibling.is_file():
            return str(sibling)
    return None


#: ``Duration: 00:01:34.30`` as printed by ``ffmpeg -i``.
_FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
#: ``Stream #0:1: Audio: aac …`` — proves the container has an audio track.
_FFMPEG_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?:\s*(Video|Audio):", re.IGNORECASE)


def ffmpeg_probe_media(path: Path) -> MediaProbe:
    """Read duration and stream kinds using ``ffmpeg -i`` alone.

    A portable ffmpeg build ships without ffprobe, and requiring the second
    binary just to learn a duration would be needlessly strict: ``ffmpeg -i``
    prints exactly what we need on stderr before exiting. This makes a
    single-binary install fully functional.
    """
    status = ffmpeg_status()
    if not status.available or not status.path:
        return MediaProbe()

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv
            [status.path, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg probe failed for %s: %s", path.name, exc)
        return MediaProbe()

    # `ffmpeg -i` writes its report to stderr and exits non-zero.
    output = f"{completed.stderr or ''}\n{completed.stdout or ''}"
    probe = MediaProbe()
    match = _FFMPEG_DURATION_RE.search(output)
    if match:
        hours, minutes, seconds = match.groups()
        total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        probe.duration_ms = int(round(total * 1000))

    probe.streams = [
        item.group(1).lower() for item in _FFMPEG_STREAM_RE.finditer(output)
    ]
    return probe


def reset_probe_cache() -> None:
    """Drop the cache (used by tests)."""
    _cache.clear()