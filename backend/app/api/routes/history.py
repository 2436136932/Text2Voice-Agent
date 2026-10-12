"""History routes: list, detail, delete, stats, CSV export."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import PaginationDep, RateLimitDefault, SessionDep
from app.schemas.generation import (
    HistoryDeleteRequest,
    HistoryDeleteResponse,
    HistoryItem,
    HistoryListResponse,
)
from app.services.history import history_service

router = APIRouter(prefix="/history", tags=["历史记录 History"])


@router.get(
    "",
    response_model=HistoryListResponse,
    summary="分页查询历史记录",
    description=(
        "支持全文搜索（`q` 同时匹配输入文本与回复文本）与状态过滤。"
        "`audio_url` 可直接用于 `<audio>` 播放或下载。"
    ),
)
async def list_history(
    session: SessionDep,
    pagination: PaginationDep,
    _: RateLimitDefault,
    q: Annotated[str | None, Query(max_length=200, description="搜索关键字")] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern="^(pending|running|succeeded|failed|canceled)$"),
    ] = None,
    provider: Annotated[str | None, Query(max_length=32, description="按服务商过滤")] = None,
) -> HistoryListResponse:
    return await history_service.list(
        session,
        page=pagination.page,
        page_size=pagination.page_size,
        query=q,
        status=status_filter,
        provider=provider,
    )


@router.get(
    "/stats",
    summary="历史统计概览",
    description="记录总数、成功/失败数、音频总时长与磁盘占用。",
)
async def history_stats(session: SessionDep, _: RateLimitDefault) -> dict:
    return await history_service.stats(session)


@router.get(
    "/export",
    summary="导出历史记录为 CSV",
    description="返回 `text/csv`，便于离线分析。",
    response_class=Response,
)
async def export_history(session: SessionDep, _: RateLimitDefault) -> Response:
    csv_text = await history_service.export_csv(session)
    return Response(
        content=csv_text.encode("utf-8-sig"),  # BOM so Excel detects UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="text2voice-history.csv"'},
    )


@router.delete(
    "",
    response_model=HistoryDeleteResponse,
    summary="批量删除历史记录",
    description="请求体传 `ids` 数组，或传 `{\"all\": true}` 清空全部（同时删除音频文件）。",
)
async def delete_history(
    payload: HistoryDeleteRequest,
    session: SessionDep,
    _: RateLimitDefault,
) -> HistoryDeleteResponse:
    deleted = await history_service.delete_many(
        session, ids=payload.ids, delete_all=payload.all
    )
    return HistoryDeleteResponse(deleted=deleted)


@router.get(
    "/{record_id}",
    response_model=HistoryItem,
    summary="获取单条历史记录详情",
    responses={404: {"description": "记录不存在"}},
)
async def get_history(record_id: int, session: SessionDep, _: RateLimitDefault) -> HistoryItem:
    return await history_service.get_item(session, record_id)


@router.delete(
    "/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除单条历史记录",
    responses={404: {"description": "记录不存在"}},
)
async def delete_history_item(
    record_id: int, session: SessionDep, _: RateLimitDefault
) -> Response:
    await history_service.delete(session, record_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)