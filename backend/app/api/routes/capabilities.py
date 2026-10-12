"""Runtime capability probe (``GET /capabilities``).

The UI needs to know **before the user clicks anything** which features this
particular installation can actually deliver:

* whether ``ffmpeg`` is installed (video -> audio extraction, non-WAV trimming);
* which TTS providers can clone a voice, and which can take a reference clip;
* what the upload form should accept, and how big a file may be.

Answering those questions from the client would mean hard-coding deployment
assumptions into the frontend. Instead the backend probes once and reports the
truth, so the same frontend build behaves correctly on a laptop without ffmpeg
and on a server with it.

The probe result is cached for a few seconds (:mod:`app.services.tools`) because
it shells out to ``ffmpeg -version``; ``?refresh=true`` bypasses the cache for
the "重新检测" button in the settings page.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import RateLimitDefault
from app.core.config import settings
from app.core.logging import get_logger
from app.services.media import (
    ACCEPTED_FORMATS,
    NATIVE_DURATION_FORMATS,
    NATIVE_TRIM_FORMATS,
    accept_attribute,
    accepted_extensions,
)
from app.services.tools import INSTALL_HINT, ffmpeg_status
from app.services.voices import (
    CLONE_NOTE,
    REFERENCE_NOTE,
    clone_capable_ids,
    reference_capable_ids,
)

logger = get_logger(__name__)

router = APIRouter(tags=["系统 System"])


@router.get(
    "/capabilities",
    summary="探测当前运行环境的可用能力",
    description=(
        "返回本机实际支持的功能开关，前端据此隐藏或解释相关入口：\n\n"
        "* `ffmpeg` —— 是否检测到 ffmpeg。未安装时仅支持 WAV 的裁剪与转码，"
        "视频提取音轨不可用；响应中会附带中文安装指引。\n"
        "* `voice_clone` —— 支持声音克隆（`supported_providers`）与支持"
        "「参考音频直接合成」（`reference_audio_providers`）的服务商。\n"
        "* `upload` —— 上传表单应使用的 `accept` 属性、大小上限、"
        "可原生裁剪的格式，以及视频转音频是否可用。\n\n"
        "结果会被缓存约 30 秒；传 `?refresh=true` 可强制重新探测。"
    ),
    responses={
        200: {"description": "当前环境能力清单"},
        429: {"description": "触发限流"},
    },
)
async def capabilities(
    _: RateLimitDefault,
    refresh: Annotated[
        bool, Query(description="强制重新探测外部工具（忽略缓存）")
    ] = False,
) -> dict[str, Any]:
    ffmpeg = ffmpeg_status(force=refresh)

    video_extensions = sorted(
        ext for ext, (kind, _content_type) in ACCEPTED_FORMATS.items() if kind == "video"
    )
    can_extract_video = ffmpeg.available

    # Trimmable = the stdlib can cut it natively, or ffmpeg can transcode it.
    trimmable = sorted(NATIVE_TRIM_FORMATS) if not ffmpeg.available else sorted(
        NATIVE_TRIM_FORMATS | {ext for ext, (kind, _c) in ACCEPTED_FORMATS.items() if kind == "audio"}
    )

    upload_note = (
        "已检测到 ffmpeg：视频与任意格式音频均可裁剪、转码。"
        if can_extract_video
        else (
            "未检测到 ffmpeg：仅 WAV 可裁剪，视频无法提取音轨。"
            f"安装后自动启用：{INSTALL_HINT}"
        )
    )

    logger.debug(
        "capabilities probed ffmpeg=%s clone_providers=%s",
        ffmpeg.available,
        len(clone_capable_ids()),
    )

    return {
        "ffmpeg": {
            "available": ffmpeg.available,
            "path": ffmpeg.path,
            "version": ffmpeg.version,
            "message": ffmpeg.message,
        },
        "voice_clone": {
            "supported_providers": clone_capable_ids(),
            "reference_audio_providers": reference_capable_ids(),
            "note": f"{CLONE_NOTE} {REFERENCE_NOTE}",
        },
        "upload": {
            "accept": accept_attribute(),
            "max_size_mb": int(settings.max_upload_mb),
            "trimmable_formats": trimmable,
            "video_extraction_available": can_extract_video,
            "extensions": accepted_extensions(),
            "video_extensions": video_extensions,
            "native_duration_formats": sorted(NATIVE_DURATION_FORMATS),
            "note": upload_note,
        },
    }
