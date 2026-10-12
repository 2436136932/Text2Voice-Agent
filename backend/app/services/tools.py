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
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

#: How long a probe result is trusted (seconds).
PROBE_TTL_SECONDS = 30

#: Common install locations checked when the binary is not on PATH. Windows
#: users frequently install via winget/scoop/choco without a fresh shell.
_FALLBACK_CANDIDATES = (
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
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


_cache: dict[str, tuple[float, ToolStatus]] = {}


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if text and text.strip() else ""


def _probe_binary(name: str) -> ToolStatus:
    """Locate a binary on PATH or in the known fallback locations."""
    found = shutil.which(name)
    if not found:
        for candidate in _FALLBACK_CANDIDATES:
            if Path(candidate).name.lower().startswith(name.lower()) and Path(candidate).is_file():
                found = candidate
                break
    if not found:
        return ToolStatus(
            available=False,
            message=(
                f"未检测到 {name}，仅支持 WAV 格式的裁剪与视频转音频。"
                f"安装后自动启用：{INSTALL_HINT}"
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


def reset_probe_cache() -> None:
    """Drop the cache (used by tests)."""
    _cache.clear()