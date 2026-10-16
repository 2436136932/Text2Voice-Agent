"""A failed attempt that has already been retried successfully must not look
retryable forever.

A retry re-submits the request and creates a NEW record rather than rewriting
history, which is right for the audit trail. The side effect was that the batch
action kept offering rows the user had already recovered, and taking it up would
spend money a second time.

`superseded_by` is derived on read: a failed/interrupted row is superseded when
a LATER successful record shares its text, TTS provider and voice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import GenerationRecord
from app.services.history import history_service


def make_record(
    *,
    seq: int,
    status: str = "succeeded",
    text: str = "相同的文本",
    provider: str = "mock",
    voice: str = "mock-neutral",
    audio_format: str = "wav",
) -> GenerationRecord:
    """A record with an explicit id so ordering is unambiguous."""
    return GenerationRecord(
        id=seq,
        input_text=text,
        reply_text="回复",
        llm_provider="mock",
        llm_model="mock-llm",
        tts_provider=provider,
        tts_voice=voice,
        tts_model="",
        audio_format=audio_format,
        audio_bytes=0,
        audio_path="",
        status=status,
        created_at=datetime.now(timezone.utc) + timedelta(seconds=seq),
    )


async def seed(session, records):
    for record in records:
        session.add(record)
    await session.commit()


class TestSupersededBy:
    async def test_a_later_success_supersedes_an_earlier_failure(self, session):
        await seed(
            session,
            [
                make_record(seq=1, status="failed"),
                make_record(seq=2, status="succeeded"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        by_id = {item.id: item for item in listing.items}

        assert by_id[1].superseded_by == 2, "the failure was recovered by run 2"
        assert by_id[2].superseded_by is None, "a success is never superseded"

    async def test_a_failure_is_not_superseded_by_an_EARLIER_success(self, session):
        """Only a later run can replace it; otherwise the retry order is wrong."""
        await seed(
            session,
            [
                make_record(seq=1, status="succeeded"),
                make_record(seq=2, status="failed"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        by_id = {item.id: item for item in listing.items}

        assert by_id[2].superseded_by is None

    async def test_a_different_voice_does_not_supersede(self, session):
        """Re-running the same text in ANOTHER voice is a different request."""
        await seed(
            session,
            [
                make_record(seq=1, status="failed", voice="mock-neutral"),
                make_record(seq=2, status="succeeded", voice="冰糖"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        by_id = {item.id: item for item in listing.items}
        assert by_id[1].superseded_by is None

    async def test_a_different_text_does_not_supersede(self, session):
        await seed(
            session,
            [
                make_record(seq=1, status="failed", text="第一段"),
                make_record(seq=2, status="succeeded", text="第二段"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        by_id = {item.id: item for item in listing.items}
        assert by_id[1].superseded_by is None

    async def test_a_different_provider_does_not_supersede(self, session):
        await seed(
            session,
            [
                make_record(seq=1, status="failed", provider="mimo"),
                make_record(seq=2, status="succeeded", provider="mock"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        by_id = {item.id: item for item in listing.items}
        assert by_id[1].superseded_by is None

    async def test_a_still_failing_row_is_not_superseded(self, session):
        """Two failures in a row: nothing was recovered, so both stay retryable."""
        await seed(
            session,
            [
                make_record(seq=1, status="failed"),
                make_record(seq=2, status="failed"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        assert all(item.superseded_by is None for item in listing.items)

    async def test_interrupted_rows_are_superseded_too(self, session):
        await seed(
            session,
            [
                make_record(seq=1, status="interrupted"),
                make_record(seq=2, status="succeeded"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        by_id = {item.id: item for item in listing.items}
        assert by_id[1].superseded_by == 2

    async def test_repeated_retries_report_the_first_success(self, session):
        """Retrying twice must not report the latest one; the first is the one
        that recovered the row, and it keeps the value stable."""
        await seed(
            session,
            [
                make_record(seq=1, status="failed"),
                make_record(seq=2, status="succeeded"),
                make_record(seq=3, status="succeeded"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        by_id = {item.id: item for item in listing.items}
        assert by_id[1].superseded_by == 2

    async def test_independent_texts_do_not_cross_supersede(self, session):
        """A success for one text must not clear the failure of another."""
        await seed(
            session,
            [
                make_record(seq=1, status="failed", text="甲"),
                make_record(seq=2, status="failed", text="乙"),
                make_record(seq=3, status="succeeded", text="甲"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        by_id = {item.id: item for item in listing.items}
        assert by_id[1].superseded_by == 3
        assert by_id[2].superseded_by is None

    async def test_the_detail_endpoint_reports_it_too(self, session):
        await seed(
            session,
            [
                make_record(seq=1, status="failed"),
                make_record(seq=2, status="succeeded"),
            ],
        )

        item = await history_service.get_item(session, 1)
        assert item.superseded_by == 2

    async def test_a_successful_row_is_never_superseded(self, session):
        await seed(
            session,
            [
                make_record(seq=1, status="succeeded"),
                make_record(seq=2, status="succeeded"),
            ],
        )

        listing = await history_service.list(session, page=1, page_size=10)
        assert all(item.superseded_by is None for item in listing.items)

    async def test_the_field_is_derived_not_stored(self, session):
        """It must not become a column: it is a property of the row set."""
        assert "superseded_by" not in {
            column.name for column in GenerationRecord.__table__.columns
        }
