"""Datetime utilities for API serialization.

SQLite drops ``tzinfo`` on the way in, so every datetime read back from the
database is naive-but-UTC. Serialized bare (``2026-10-13T10:01:22``), the
browser's ``new Date()`` parses that as *local* time and every timestamp shows
8 hours off for UTC+8 users. :class:`AwareUTC` is a drop-in annotated type that
marks naive values as UTC **at the validation boundary** (not serialization),
so the marker survives every serialization path — ``model_dump_json``,
FastAPI response rendering, and ``model_dump(mode="json")`` alike.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer


def _mark_naive_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime; aware values pass through untouched."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _serialize_utc(value: datetime) -> str:
    """Always render in UTC with an explicit offset."""
    aware = _mark_naive_utc(value)
    return aware.astimezone(timezone.utc).isoformat()


#: Annotated datetime type: naive input is treated as UTC, and output always
#: carries the ``+00:00`` marker. Use it for every API-facing datetime field.
AwareUTC = Annotated[
    datetime,
    AfterValidator(_mark_naive_utc),
    PlainSerializer(_serialize_utc, return_type=str),
]
