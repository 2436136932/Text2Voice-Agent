"""Media routes: inspect an upload, trim it, stream it back, delete it.

These four endpoints are what makes "upload a reference clip and use it for
cloning" possible with **no external tooling**:

* ``POST /media/inspect`` stores the upload and reports what we could learn
  about it (kind, duration, sample rate, whether it can be trimmed);
* ``POST /media/trim`` cuts a WAV natively, or shells out to ffmpeg for other
  formats - and says exactly what to install when ffmpeg is missing;
* ``GET /media/{id}`` streams the bytes for ``<audio>`` / ``<video>`` preview,
  honouring HTTP Range so the browser can seek;
* ``DELETE /media/{id}`` removes the file *and* its metadata sidecar.

Uploads are scratch data: they are never logged as raw bytes, and
:meth:`app.services.media.MediaStore.purge_older_than` cleans them up after
``settings.media_retention_days``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import RateLimitDefault, RateLimitGenerate
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.schemas.generation import (
    MediaInspectResponse,
    MediaTrimRequest,
    MediaTrimResponse,
)
from app.services.media import (
    MAX_TRIM_WINDOW_MS,
    accepted_extensions,
    inspect_upload,
    media_store,
    trim_media,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/media", tags=["媒体 Media"])

#: Read size for range streaming; matches the audio route.
CHUNK_SIZE = 64 * 1024


@router.post(
    "/inspect",
    response_model=MediaInspectResponse,
    summary="上传并解析参考音频/视频",
    description=(
        "接收一个音频或视频文件（表单字段名 **file**），保存到服务器并返回其元信息，"
        "用于后续的声音克隆或参考音频合成。\n\n"
        "**能力说明**：\n"
        "* WAV：直接读取采样率、声道数与精确时长，可原生裁剪；\n"
        "* MP3/OGG/FLAC：可从帧头解析时长；\n"
        "* MP4/MOV/WebM 等视频：需要 ffmpeg 才能提取音轨，"
        "未安装时 `trimmable=false` 并在 `warnings` 中给出提示"
        "（可用 `GET /capabilities` 提前判断）。\n\n"
        "**限制**：仅接受 "
        + ", ".join(accepted_extensions())
        + "；大小上限见 `GET /capabilities`。上传内容不会被记录到日志。"
    ),
    responses={
        200: {"description": "解析成功"},
        400: {"description": "文件格式不支持（MEDIA_UNSUPPORTED_FORMAT）"},
        413: {"description": "文件过大（MEDIA_TOO_LARGE）"},
        422: {"description": "缺少文件字段"},
        429: {"description": "触发限流"},
    },
)
async def inspect_media(
    _: RateLimitGenerate,
    file: UploadFile = File(..., description="音频或视频文件"),
) -> MediaInspectResponse:
    # Read in full: the size cap is small (50 MB default) and duration probing
    # needs random access to the header, which UploadFile does not give us.
    data = await file.read()
    logger.info(
        "media inspect filename=%s bytes=%s", file.filename, len(data)
    )
    record = inspect_upload(data, file.filename or "upload.bin")
    return MediaInspectResponse(**record.to_dict())


@router.post(
    "/trim",
    response_model=MediaTrimResponse,
    summary="裁剪参考音频",
    description=(
        "按毫秒区间裁剪出一段新的参考音频（始终输出 16 kHz 单声道 WAV，"
        "这也是各家克隆接口最稳妥的输入格式）。\n\n"
        "**注意**：\n"
        "* 源文件为 WAV 时使用标准库裁剪，无需任何外部依赖；\n"
        "* 源文件为其他格式时依赖 ffmpeg，未安装会返回 422 "
        "`MEDIA_TRIM_UNSUPPORTED`，`detail.suggestion` 中会给出安装命令；\n"
        "* 单次最多裁剪 10 分钟，且 `end_ms` 必须大于 `start_ms`。\n\n"
        "裁剪结果是一个**新的** `media_id`，原文件保持不变。"
    ),
    responses={
        200: {"description": "裁剪成功"},
        400: {"description": "区间不合法（MEDIA_INVALID_RANGE）"},
        404: {"description": "源媒体不存在（MEDIA_NOT_FOUND）"},
        422: {"description": "该格式无法裁剪，或缺少 ffmpeg（MEDIA_TRIM_UNSUPPORTED / FFMPEG_REQUIRED）"},
        429: {"description": "触发限流"},
    },
)
async def trim(
    payload: MediaTrimRequest,
    _: RateLimitGenerate,
) -> MediaTrimResponse:
    logger.info(
        "media trim media_id=%s start_ms=%s end_ms=%s",
        payload.media_id, payload.start_ms, payload.end_ms,
    )
    record = trim_media(payload.media_id, payload.start_ms, payload.end_ms)
    return MediaTrimResponse(
        media_id=record.media_id,
        preview_url=record.preview_url,
        duration_ms=record.duration_ms,
        size_bytes=record.size_bytes,
        format=record.format,
    )


@router.get(
    "/{media_id}",
    response_class=Response,
    summary="预览/下载媒体文件",
    description=(
        "返回原始字节流，可直接用作 `<audio src>` 或 `<video src>`。\n\n"
        "支持 HTTP Range 请求（206），因此浏览器可以拖动进度条试听；"
        "图片/视频的 `Content-Type` 依据上传时的扩展名判定。"
    ),
    responses={
        200: {"description": "媒体字节流"},
        206: {"description": "部分内容（Range 请求）"},
        404: {"description": "媒体不存在（MEDIA_NOT_FOUND）"},
        416: {"description": "Range 不合法"},
        429: {"description": "触发限流"},
    },
)
async def get_media(
    media_id: str,
    request: Request,
    _: RateLimitDefault,
) -> Response:
    record = _load_or_404(media_id)
    path = media_store.path_for(record)
    if not path.is_file():
        raise _not_found(media_id)

    size = path.stat().st_size
    content_type = record.content_type or "application/octet-stream"
    filename = record.filename or f"{media_id}.{record.extension}"

    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            raw_start, _, raw_end = range_header.removeprefix("bytes=").partition("-")
            start = int(raw_start) if raw_start else 0
            end = int(raw_end) if raw_end else size - 1
        except ValueError:
            raise AppError(
                ErrorCode.INVALID_PARAMETER,
                "Range 头格式不正确。",
                detail={"range": range_header},
                http_status=416,
            ) from None
        if start >= size or end < start:
            return Response(
                status_code=416, headers={"Content-Range": f"bytes */{size}"}
            )
        end = min(end, size - 1)

        def _iter_range():
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = handle.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            _iter_range(),  # type: ignore[arg-type]
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
                "Content-Disposition": f'inline; filename="{_ascii(filename)}"',
                "Cache-Control": "private, max-age=3600",
            },
        )

    return FileResponse(
        path,
        media_type=content_type,
        filename=_ascii(filename),
        content_disposition_type="inline",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(size),
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.delete(
    "/{media_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除上传的媒体文件",
    description=(
        "删除文件本体与元数据 sidecar。已基于该文件克隆出的音色**不受影响**"
        "（克隆结果保存在服务商侧）。文件不存在时同样返回 204，便于前端幂等清理。"
    ),
    responses={
        204: {"description": "已删除（幂等）"},
        429: {"description": "触发限流"},
    },
)
async def delete_media(media_id: str, _: RateLimitDefault) -> Response:
    removed = media_store.delete(media_id)
    logger.info("media delete media_id=%s removed=%s", media_id, removed)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_or_404(media_id: str):
    try:
        return media_store.load(media_id)
    except AppError as exc:
        if exc.code is ErrorCode.MEDIA_NOT_FOUND:
            raise _not_found(media_id) from exc
        raise


def _not_found(media_id: str) -> AppError:
    return AppError(
        ErrorCode.MEDIA_NOT_FOUND,
        f"媒体文件不存在或已过期（{media_id[:8]}…）。",
        detail={
            "media_id": media_id,
            "suggestion": "请重新上传文件后再试；上传文件默认保留 7 天。",
        },
        http_status=404,
    )


def _ascii(value: str) -> str:
    """Keep non-ASCII filenames out of the raw header value.

    Starlette encodes headers as latin-1, so a Chinese filename would raise.
    """
    return Path(value).name.encode("ascii", "ignore").decode("ascii") or "media"
