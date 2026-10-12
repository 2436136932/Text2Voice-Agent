"""Voice-cloning routes: clone a voice, list them, delete one.

``POST /voices/clone`` is the interesting one. It chains together everything
built for media handling:

1. load the uploaded reference clip (``media_id`` -> bytes);
2. hand it to the provider's clone API through the adapter;
3. persist a :class:`~app.db.models.ClonedVoice` row so the returned
   ``voice_id`` becomes selectable in ``GET /providers``.

The ``api_key`` travels in the request body, is forwarded to the vendor exactly
once, and is never persisted - ``cloned_voices`` has no credential column.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RateLimitDefault, RateLimitGenerate, SessionDep
from app.core.logging import get_logger
from app.schemas.generation import (
    ClonedVoiceResponse,
    VoiceCloneRequestSchema,
    VoiceListResponse,
)
from app.services.voices import voice_service

logger = get_logger(__name__)

router = APIRouter(prefix="/voices", tags=["声音克隆 Voices"])


@router.post(
    "/clone",
    response_model=ClonedVoiceResponse,
    summary="用参考音频克隆一个音色",
    description=(
        "把 `POST /media/inspect` 返回的 `media_id` 提交给指定服务商，"
        "创建一个可复用的音色。克隆成功后该音色会立即出现在 "
        "`GET /providers` 对应服务商的 `voices` 列表中（`cloned=true`），"
        "可以直接用于生成语音。\n\n"
        "**支持克隆的服务商**：ElevenLabs（`POST /voices/add`）、"
        "MiniMax（先上传文件再克隆，两步自动完成）。\n\n"
        "**不支持克隆时**：返回 400 `VOICE_CLONE_UNSUPPORTED`，"
        "`detail.suggestion` 会列出可用的替代服务商。\n\n"
        "**密钥安全**：`api_key` 仅用于本次克隆调用，不会写入数据库。"
    ),
    responses={
        200: {"description": "克隆成功"},
        400: {"description": "服务商不支持克隆，或参数不合法"},
        404: {"description": "media_id 不存在或已过期"},
        413: {"description": "参考音频过大"},
        429: {"description": "触发限流"},
        502: {"description": "上游克隆失败（VOICE_CLONE_FAILED）"},
    },
)
async def clone_voice(
    payload: VoiceCloneRequestSchema,
    session: SessionDep,
    _: RateLimitGenerate,
) -> ClonedVoiceResponse:
    api_key = payload.api_key.get_secret_value() if payload.api_key else ""
    outcome = await voice_service.clone(
        session,
        provider=payload.provider,
        api_key=api_key,
        base_url=payload.base_url,
        name=payload.name,
        media_id=payload.media_id,
        model=payload.model or "",
        extra=payload.extra,
    )
    return ClonedVoiceResponse(
        id=outcome.id,
        voice_id=outcome.voice_id,
        provider=outcome.provider,
        name=outcome.name,
        model=outcome.model,
        created_at=outcome.created_at,
        duration_ms=outcome.duration_ms,
    )


@router.get(
    "",
    response_model=VoiceListResponse,
    summary="列出已克隆的音色",
    description=(
        "返回本机数据库中的克隆音色，按创建时间倒序（最新的在最前面）。\n\n"
        "可选 `provider` 参数按服务商过滤。注意这里只返回**元数据**，"
        "不会调用服务商接口——服务商侧若已删除该音色，这里仍会列出。"
    ),
    responses={200: {"description": "克隆音色列表"}, 429: {"description": "触发限流"}},
)
async def list_voices(
    _: RateLimitDefault,
    session: SessionDep,
    provider: str | None = None,
) -> VoiceListResponse:
    items = await voice_service.list(session, provider=provider)
    return VoiceListResponse(items=items)


@router.delete(
    "/{voice_pk}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除本地克隆音色记录",
    description=(
        "从本机列表中移除该音色记录。**不会**调用服务商接口，"
        "因此服务商侧的音色依然存在（如需要请到服务商控制台删除）。\n\n"
        "`voice_pk` 是列表中的 `id`（整数主键），不是 `voice_id`。"
    ),
    responses={
        204: {"description": "已删除"},
        404: {"description": "音色不存在（VOICE_NOT_FOUND）"},
        429: {"description": "触发限流"},
    },
)
async def delete_voice(
    voice_pk: int,
    session: SessionDep,
    _: RateLimitDefault,
) -> Response:
    await voice_service.delete(session, voice_pk)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
