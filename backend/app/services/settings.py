"""Settings (user preferences) service.

Only *preferences* live here - never credentials. The endpoint accepts no
``api_key`` field at all, and the request schema uses ``extra="forbid"``, so a
client that tries to persist a key receives a 422.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import SettingModel
from app.schemas.generation import SettingsSchema, SettingsUpdateRequest

logger = get_logger(__name__)

#: Fields a client is allowed to change. Anything else is rejected by the
#: pydantic schema before it reaches this service.
MUTABLE_FIELDS = frozenset(SettingsUpdateRequest.model_fields)


class SettingsService:
    """Read/write the single-row settings document."""

    async def get(self, session: AsyncSession) -> SettingsSchema:
        data = await SettingModel.load(session)
        row = await SettingModel.get_or_create(session)
        return SettingsSchema(
            **{key: data[key] for key in SettingsSchema.model_fields if key in data},
            updated_at=row.updated_at,
        )

    async def update(
        self, session: AsyncSession, payload: SettingsUpdateRequest
    ) -> SettingsSchema:
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        unknown = set(changes) - MUTABLE_FIELDS
        if unknown:  # pragma: no cover - pydantic already blocks this
            logger.warning("ignoring unknown settings keys keys=%s", sorted(unknown))
            changes = {k: v for k, v in changes.items() if k in MUTABLE_FIELDS}

        # ``updated_at`` must move even when the payload is empty so the UI can
        # confirm the save round-trip.
        merged = await SettingModel.save(session, changes)
        row = await SettingModel.get_or_create(session)
        row.updated_at = datetime.now(timezone.utc)
        await session.flush()

        logger.info("settings updated keys=%s", sorted(changes))
        return SettingsSchema(
            **{key: merged[key] for key in SettingsSchema.model_fields if key in merged},
            updated_at=row.updated_at,
        )

    async def raw(self, session: AsyncSession) -> dict[str, Any]:
        return await SettingModel.load(session)


settings_service = SettingsService()