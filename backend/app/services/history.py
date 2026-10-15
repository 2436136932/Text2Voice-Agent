"""History service: query, inspect, delete and export generation records."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Text, cast, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.models import GenerationRecord
from app.schemas.generation import HistoryItem, HistoryListResponse, normalize_tags
from app.services.audio import audio_store

logger = get_logger(__name__)

MAX_PAGE_SIZE = 100


#: LIKE escape character used by :meth:`HistoryService.list`. Chosen because it
#: is not special in SQL LIKE and is unlikely to appear in search text.
LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    """Escape SQL ``LIKE`` wildcards so the user's text is matched literally.

    Without this, searching for ``%`` matches every row and ``_`` matches any
    single character — surprising, and it also makes the search box a cheap way
    to force a full table scan. Pairs with ``escape=LIKE_ESCAPE`` on the
    ``ilike`` call.
    """
    return value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", "\\%").replace("_", "\\_")


def tag_needles(tag: str) -> list[str]:
    """The serialized forms of ``tag`` as they appear inside the JSON column.

    ``json.dumps`` (what SQLAlchemy's JSON type writes) escapes non-ASCII
    characters, so a Chinese tag is physically stored as ``["\\u5de5\\u4f5c"]``.
    Both encodings are valid JSON and a non-ASCII-preserving writer would
    produce the literal form, so both are matched; ASCII tags have a single
    representation and yield one needle.
    """
    compact = json.dumps(tag, ensure_ascii=False, separators=(",", ":"))
    if compact.isascii():
        return [compact]
    return [compact, json.dumps(tag, separators=(",", ":"))]


class HistoryService:
    """CRUD over :class:`GenerationRecord`."""

    async def list(
        self,
        session: AsyncSession,
        *,
        page: int = 1,
        page_size: int = 20,
        query: str | None = None,
        status: str | None = None,
        provider: str | None = None,
        favorite: bool | None = None,
        tags: list[str] | None = None,
    ) -> HistoryListResponse:
        page = max(1, page)
        page_size = max(1, min(MAX_PAGE_SIZE, page_size))

        conditions = []
        if query:
            pattern = f"%{escape_like(query.strip())}%"
            conditions.append(
                or_(
                    GenerationRecord.input_text.ilike(pattern, escape=LIKE_ESCAPE),
                    GenerationRecord.reply_text.ilike(pattern, escape=LIKE_ESCAPE),
                )
            )
        if status:
            conditions.append(GenerationRecord.status == status)
        if provider:
            conditions.append(
                or_(
                    GenerationRecord.llm_provider == provider,
                    GenerationRecord.tts_provider == provider,
                )
            )
        if favorite is not None:
            conditions.append(GenerationRecord.favorite.is_(favorite))
        if tags:
            # ``tags`` is a JSON array column, so membership is tested by
            # matching the serialized element inside the array text. The quoted
            # needle keeps a tag like ``日报`` from also matching ``本周日报``.
            # Multiple tags are OR-ed: the UI sends the set of chips the user
            # ticked, and "any of the selected tags" is what a filter means.
            serialized = cast(GenerationRecord.tags, Text)
            tag_matches = [
                serialized.like(f"%{escape_like(needle)}%", escape=LIKE_ESCAPE)
                for tag in tags
                for needle in tag_needles(tag)
            ]
            conditions.append(or_(*tag_matches))

        base = select(GenerationRecord)
        counter = select(func.count()).select_from(GenerationRecord)
        for condition in conditions:
            base = base.where(condition)
            counter = counter.where(condition)

        total = int((await session.execute(counter)).scalar_one() or 0)
        result = await session.execute(
            base.order_by(GenerationRecord.created_at.desc(), GenerationRecord.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = result.scalars().all()
        pages = (total + page_size - 1) // page_size if total else 0

        return HistoryListResponse(
            items=[self._to_item(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
            pages=pages,
        )

    async def get(self, session: AsyncSession, record_id: int) -> GenerationRecord:
        row = await session.get(GenerationRecord, record_id)
        if row is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"历史记录 {record_id} 不存在。",
                detail={"record_id": record_id},
                http_status=404,
            )
        return row

    async def get_item(self, session: AsyncSession, record_id: int) -> HistoryItem:
        return self._to_item(await self.get(session, record_id))

    async def update(
        self,
        session: AsyncSession,
        record_id: int,
        *,
        favorite: bool | None = None,
        tags: list[str] | None = None,
    ) -> HistoryItem:
        """Set ``favorite`` and/or ``tags``; omitted fields are left untouched.

        ``tags`` is normalized here as well as in the schema so service-level
        callers (and future routes) get the same dedupe/trim/cap guarantees.
        """
        row = await self.get(session, record_id)
        if favorite is not None:
            row.favorite = bool(favorite)
        if tags is not None:
            row.tags = normalize_tags(tags)
        await session.flush()
        logger.info(
            "history record updated record_id=%s favorite=%s tags=%s",
            record_id,
            favorite,
            len(row.tag_list),
        )
        return self._to_item(row)

    async def delete(self, session: AsyncSession, record_id: int) -> bool:
        row = await session.get(GenerationRecord, record_id)
        if row is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"历史记录 {record_id} 不存在。",
                detail={"record_id": record_id},
                http_status=404,
            )
        audio_path = row.audio_path
        await session.delete(row)
        await session.flush()
        if audio_path:
            audio_store.delete(audio_path)
        logger.info("history record deleted record_id=%s", record_id)
        return True

    async def delete_many(
        self, session: AsyncSession, *, ids: list[int] | None = None, delete_all: bool = False
    ) -> int:
        statement = select(GenerationRecord.id, GenerationRecord.audio_path)
        if not delete_all:
            if not ids:
                return 0
            statement = statement.where(GenerationRecord.id.in_(ids))
        rows = (await session.execute(statement)).all()
        if not rows:
            return 0

        record_ids = [row[0] for row in rows]
        audio_paths = [row[1] for row in rows if row[1]]
        await session.execute(
            delete(GenerationRecord).where(GenerationRecord.id.in_(record_ids))
        )
        await session.flush()
        for path in audio_paths:
            audio_store.delete(path)
        logger.info("history records deleted count=%s all=%s", len(record_ids), delete_all)
        return len(record_ids)

    async def purge_older_than(self, session: AsyncSession, *, days: int) -> int:
        """Retention job: drop records (and audio) older than ``days``."""
        if days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = (
            await session.execute(
                select(GenerationRecord.id, GenerationRecord.audio_path).where(
                    GenerationRecord.created_at < cutoff
                )
            )
        ).all()
        if not rows:
            return 0
        record_ids = [row[0] for row in rows]
        await session.execute(delete(GenerationRecord).where(GenerationRecord.id.in_(record_ids)))
        await session.flush()
        for _, path in rows:
            if path:
                audio_store.delete(path)
        logger.info("retention purge removed records=%s older_than_days=%s", len(record_ids), days)
        return len(record_ids)

    async def stats(self, session: AsyncSession) -> dict[str, Any]:
        total = int(
            (await session.execute(select(func.count()).select_from(GenerationRecord))).scalar_one()
            or 0
        )
        succeeded = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(GenerationRecord)
                    .where(GenerationRecord.status == "succeeded")
                )
            ).scalar_one()
            or 0
        )
        duration_sum = (
            await session.execute(select(func.coalesce(func.sum(GenerationRecord.duration_ms), 0)))
        ).scalar_one()
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            "total_audio_ms": int(duration_sum or 0),
            "audio_files": audio_store.count(),
            "audio_bytes": audio_store.total_bytes(),
        }

    async def usage(
        self,
        session: AsyncSession,
        *,
        days: int = 30,
        group_by: str = "llm_provider",
    ) -> dict[str, Any]:
        """Token/cost-oriented aggregation for the usage dashboard.

        Sums the per-row usage columns over the window and groups by one of
        the record dimensions. Token data is stored on every row already;
        this endpoint is the missing read side.
        """
        allowed = {"llm_provider", "llm_model", "tts_provider", "tts_voice", "day"}
        if group_by not in allowed:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"不支持的分组维度 “{group_by}”，可用：{', '.join(sorted(allowed))}。",
                detail={"group_by": group_by, "supported": sorted(allowed)},
                http_status=400,
            )

        window_start = datetime.now(timezone.utc) - timedelta(days=max(1, days))

        def _column(name: str):  # noqa: ANN202 - tiny local mapper
            if name == "day":
                return func.date(GenerationRecord.created_at)
            return getattr(GenerationRecord, name)

        group_col = _column(group_by)
        rows = (
            await session.execute(
                select(
                    group_col.label("group"),
                    func.count().label("generations"),
                    func.coalesce(func.sum(GenerationRecord.prompt_tokens), 0).label("prompt_tokens"),
                    func.coalesce(func.sum(GenerationRecord.completion_tokens), 0).label("completion_tokens"),
                    func.coalesce(func.sum(GenerationRecord.total_tokens), 0).label("total_tokens"),
                    func.coalesce(func.sum(GenerationRecord.duration_ms), 0).label("audio_ms"),
                    func.coalesce(func.sum(GenerationRecord.audio_bytes), 0).label("audio_bytes"),
                    func.coalesce(func.sum(GenerationRecord.llm_latency_ms), 0).label("llm_ms"),
                    func.coalesce(func.sum(GenerationRecord.tts_latency_ms), 0).label("tts_ms"),
                )
                .where(
                    GenerationRecord.created_at >= window_start,
                    GenerationRecord.status == "succeeded",
                )
                .group_by(group_col)
                .order_by(group_col)
            )
        ).all()

        items = [
            {
                "group": str(row.group) if row.group is not None else "—",
                "generations": int(row.generations),
                "prompt_tokens": int(row.prompt_tokens),
                "completion_tokens": int(row.completion_tokens),
                "total_tokens": int(row.total_tokens),
                "audio_ms": int(row.audio_ms),
                "audio_bytes": int(row.audio_bytes),
                "llm_ms": int(row.llm_ms),
                "tts_ms": int(row.tts_ms),
            }
            for row in rows
        ]
        return {
            "days": days,
            "group_by": group_by,
            "items": items,
            "totals": {
                "generations": sum(item["generations"] for item in items),
                "total_tokens": sum(item["total_tokens"] for item in items),
                "audio_bytes": sum(item["audio_bytes"] for item in items),
            },
        }

    async def export_csv(self, session: AsyncSession, *, limit: int = 5000) -> str:
        """Export the history as CSV (text fields escaped by the csv module)."""
        rows = (
            await session.execute(
                select(GenerationRecord)
                .order_by(GenerationRecord.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "id", "created_at", "status", "llm_provider", "llm_model",
                "tts_provider", "tts_voice", "duration_ms", "audio_bytes",
                "degraded", "input_text", "reply_text",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.id,
                    row.created_at.astimezone(timezone.utc).isoformat()
                    if row.created_at
                    else "",
                    row.status,
                    row.llm_provider,
                    row.llm_model,
                    row.tts_provider,
                    row.tts_voice,
                    row.duration_ms or "",
                    row.audio_bytes,
                    "yes" if row.degraded else "no",
                    row.input_text.replace("\n", " "),
                    row.reply_text.replace("\n", " "),
                ]
            )
        return buffer.getvalue()

    # -- mapping ------------------------------------------------------------
    @staticmethod
    def _to_item(row: GenerationRecord) -> HistoryItem:
        return HistoryItem(**row.to_dict())


history_service = HistoryService()