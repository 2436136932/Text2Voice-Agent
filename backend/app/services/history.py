"""History service: query, inspect, delete and export generation records."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.models import GenerationRecord
from app.schemas.generation import HistoryItem, HistoryListResponse
from app.services.audio import audio_store

logger = get_logger(__name__)

MAX_PAGE_SIZE = 100


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
    ) -> HistoryListResponse:
        page = max(1, page)
        page_size = max(1, min(MAX_PAGE_SIZE, page_size))

        conditions = []
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(
                or_(
                    GenerationRecord.input_text.ilike(pattern),
                    GenerationRecord.reply_text.ilike(pattern),
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
                    row.created_at.isoformat() if row.created_at else "",
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