"""Audio streaming route.

Serves the generated audio for a history record. Two lookup paths:

1. the normal one - the record exists and stores ``audio_path``;
2. the ``auto_save=false`` case - no row was written, so the file is located by
   its ``<record_id>_*`` prefix inside the audio directory.

Range requests are honoured so browsers can seek inside long clips.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import RateLimitDefault, SessionDep
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.models import GenerationRecord
from app.services.audio import EXTENSION_BY_FORMAT, audio_store

logger = get_logger(__name__)

router = APIRouter(tags=["音频 Audio"])

CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "pcm": "audio/L16",
    "webm": "audio/webm",
    "ulaw": "audio/basic",
}

CHUNK_SIZE = 64 * 1024


def _content_type(audio_format: str, path: Path) -> str:
    mapped = CONTENT_TYPES.get((audio_format or "").lower())
    if mapped:
        return mapped
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _resolve_audio(record: GenerationRecord | None, record_id: int) -> tuple[Path, str]:
    """Return ``(path, content_type)`` or raise ``AUDIO_NOT_FOUND``."""
    if record is not None and record.audio_path:
        path = audio_store.path_for(record.audio_path)
        if path is not None:
            return path, _content_type(record.audio_format, path)

    # Fallback: locate by prefix (unsaved generations, or a row whose file was
    # written but whose flush failed).
    candidates = sorted(audio_store.root.glob(f"{record_id}_*"))
    for candidate in candidates:
        if candidate.is_file():
            fmt = candidate.suffix.lstrip(".").lower()
            return candidate, _content_type(fmt, candidate)

    raise AppError(
        ErrorCode.AUDIO_NOT_FOUND,
        f"音频文件不存在（记录 {record_id}）。",
        detail={"record_id": record_id},
        http_status=404,
    )


@router.get(
    "/audio/{record_id}",
    summary="获取/下载音频",
    description=(
        "返回原始音频字节流，可直接作为 `<audio src>` 使用。\n\n"
        "加 `?download=1` 会附带 `Content-Disposition: attachment` 触发浏览器下载。\n\n"
        "支持 HTTP Range 请求（断点续传 / 拖动进度条）。"
    ),
    response_class=Response,
    responses={
        200: {"description": "音频字节流", "content": {"audio/mpeg": {}}},
        404: {"description": "音频不存在"},
        416: {"description": "Range 不合法"},
    },
)
async def get_audio(
    record_id: int,
    request: Request,
    session: SessionDep,
    _: RateLimitDefault,
    download: Annotated[bool, Query(description="是否作为附件下载")] = False,
) -> Response:
    record = await session.get(GenerationRecord, record_id)
    path, content_type = _resolve_audio(record, record_id)
    size = path.stat().st_size
    filename = f"text2voice_{record_id}{path.suffix}"
    disposition = "attachment" if download else "inline"

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
                status_code=416,
                headers={"Content-Range": f"bytes */{size}"},
            )
        end = min(end, size - 1)

        def _iter_range() -> "object":
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
                "Content-Disposition": f'{disposition}; filename="{filename}"',
                "Cache-Control": "private, max-age=3600",
            },
        )

    return FileResponse(
        path,
        media_type=content_type,
        filename=filename,
        content_disposition_type=disposition,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(size),
            "Cache-Control": "private, max-age=3600",
        },
    )