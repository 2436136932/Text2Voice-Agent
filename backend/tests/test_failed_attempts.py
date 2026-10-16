"""Failed generations must be visible in history.

A record used to be written only after the LLM *and* the TTS step succeeded, so
a failed attempt left no trace at all: the user saw nothing in history, could
not tell what went wrong, and had nothing to retry. The schema, the API
serialization (``audio_url`` is None for anything but ``succeeded``) and the
history UI's status labels/filters were all already built for non-successful
rows -- only the write path was missing.

These tests pin that write path, including the two things that make it
non-obvious: it must survive the caller's rollback, and it must not fire for a
deliberate cancellation.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.core.errors import AppError
from app.db.base import get_sessionmaker, session_scope
from app.db.models import GenerationRecord
from app.schemas.generation import GenerateRequest
from app.services.generation import GenerationService
from app.services.history import history_service


async def _count(session) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(GenerationRecord))).scalar_one()
    )


class TestFailedAttemptIsRecorded:
    async def test_llm_failure_writes_a_failed_row(self, session):
        """The LLM step fails: a row with the error code must still appear."""
        request = GenerateRequest(
            text="失败也要留痕",
            llm={"provider": "not-a-real-llm-provider"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True},
        )

        with pytest.raises(AppError):
            await GenerationService().generate(session, request, request_id="boom-1")

        async with get_sessionmaker()() as fresh:
            rows = (await fresh.execute(select(GenerationRecord))).scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.status == "failed"
        assert row.error_code, "the failure must carry a machine-readable code"
        assert row.error_message, "the failure must carry a readable message"
        assert row.input_text == "失败也要留痕"
        assert row.request_id == "boom-1"

    async def test_tts_failure_keeps_the_reply_the_llm_already_wrote(self, session):
        """TTS breaks after the LLM succeeded, so the reply is worth keeping."""
        # mock TTS only supports wav, so mp3 fails deterministically *after* the
        # LLM step has already produced a reply.
        request = GenerateRequest(
            text="语音合成失败",
            llm={"provider": "mock"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "mp3"},
            options={"auto_save": True},
        )

        with pytest.raises(AppError):
            await GenerationService().generate(session, request, request_id="boom-2")

        async with get_sessionmaker()() as fresh:
            rows = (await fresh.execute(select(GenerationRecord))).scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.status == "failed"
        assert row.error_code == "TTS_UNSUPPORTED_FORMAT"
        assert row.reply_text, "the reply already existed and must be preserved"

    async def test_the_row_survives_the_callers_rollback(self, session):
        """The critical mechanism.

        The caller is on its way to raising, so its session is rolled back. If
        the failure row were written on that session it would vanish, which is
        why `record_failed_attempt` uses its own transaction. Drive it through
        `session_scope` exactly like the routes and the task worker do.
        """
        request = GenerateRequest(
            text="回滚也要留存",
            llm={"provider": "not-a-real-llm-provider"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True},
        )

        with pytest.raises(AppError):
            async with session_scope() as scoped:
                await GenerationService().generate(scoped, request)
                # Unreachable: generate() raises. The context manager rolls back.

        async with get_sessionmaker()() as fresh:
            count = await _count(fresh)
        assert count == 1, "the failure row must survive the caller's rollback"

    async def test_credentials_are_not_written_to_a_failed_row(self, session):
        secret = "sk-proj-THISKEYMUSTNOTBELANDINTHE123456"
        request = GenerateRequest(
            text="失败行的密钥检查",
            llm={"provider": "not-a-real-llm-provider", "api_key": secret},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True},
        )

        with pytest.raises(AppError):
            await GenerationService().generate(session, request)

        async with get_sessionmaker()() as fresh:
            rows = (await fresh.execute(select(GenerationRecord))).scalars().all()

        assert len(rows) == 1
        for column in GenerationRecord.__table__.columns:
            value = getattr(rows[0], column.name)
            assert secret not in str(value), f"secret leaked into {column.name}"


class TestAutoSaveIsHonoured:
    async def test_auto_save_false_writes_no_failure_row(self, session):
        """A user who opted out of history must not get rows for failures."""
        request = GenerateRequest(
            text="不保存历史",
            llm={"provider": "not-a-real-llm-provider"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": False},
        )

        with pytest.raises(AppError):
            await GenerationService().generate(session, request)

        async with get_sessionmaker()() as fresh:
            assert await _count(fresh) == 0


class TestCancellationIsNotAFailure:
    async def test_cancelling_writes_no_row(self, session, monkeypatch):
        """A deliberate stop is not something to report as a failed attempt.

        Equally important: the cancellation path must not await any database
        work. Awaiting inside a cancellation is what used to hang shutdown,
        because an aiosqlite write does not observe task cancellation.
        """
        service = GenerationService()

        async def cancelled(*_args, **_kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(service, "_run_llm", cancelled)

        request = GenerateRequest(
            text="取消不记录",
            llm={"provider": "mock"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True},
        )

        with pytest.raises(asyncio.CancelledError):
            await service.generate(session, request)

        async with get_sessionmaker()() as fresh:
            assert await _count(fresh) == 0


class TestFailedRowsAreQueryable:
    async def test_visible_in_history_and_filterable_by_status(self, session):
        request = GenerateRequest(
            text="历史里要能看到",
            llm={"provider": "not-a-real-llm-provider"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True},
        )

        with pytest.raises(AppError):
            await GenerationService().generate(session, request)

        async with get_sessionmaker()() as fresh:
            listing = await history_service.list(fresh, page=1, page_size=20)
            filtered = await history_service.list(
                fresh, page=1, page_size=20, status="failed"
            )

        assert listing.total == 1, "the failed attempt must show up in history"
        assert filtered.total == 1, "status=failed must select it"

        item = listing.items[0]
        assert item.status == "failed"
        assert item.audio_url is None, "a failed attempt has no audio"
        # Everything a retry needs to rebuild the request is present.
        assert item.input_text == "历史里要能看到"
        assert item.llm_provider == "not-a-real-llm-provider"
        assert item.tts_provider == "mock"

    async def test_a_failed_row_never_exposes_audio(self, session):
        """`audio_url` is derived from status, not from the (empty) path."""
        request = GenerateRequest(
            text="无音频",
            llm={"provider": "not-a-real-llm-provider"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True},
        )

        with pytest.raises(AppError):
            await GenerationService().generate(session, request)

        async with get_sessionmaker()() as fresh:
            row = (await fresh.execute(select(GenerationRecord))).scalars().one()

        assert row.status != "succeeded"
        assert row.to_dict()["audio_url"] is None
        assert row.audio_bytes == 0


class TestInterruptedAttemptsBecomeVisible:
    """A task lost to a restart must also appear in history.

    Task rows are never shown in the UI, so without a history row an interrupted
    attempt is invisible and there is nothing to retry.
    """

    @staticmethod
    def _payload(*, auto_save: bool = True) -> dict:
        return {
            "text": "被重启打断的文本",
            "llm": {"provider": "mock", "model": "mock-llm"},
            "tts": {"provider": "mimo", "voice": "冰糖", "model": "mimo-v2.5-tts", "format": "mp3"},
            "options": {"auto_save": auto_save},
        }

    async def test_restore_records_an_interrupted_history_row(self, session):
        from app.db.models import TaskModel
        from app.services.tasks import TaskManager

        session.add(
            TaskModel(
                task_id="lost-1",
                status="running",
                stage="tts",
                progress=60,
                message="正在合成语音",
                request_payload=self._payload(),
                request_id="req-lost-1",
            )
        )
        await session.commit()

        await TaskManager().restore_from_db()

        async with get_sessionmaker()() as fresh:
            rows = (await fresh.execute(select(GenerationRecord))).scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.status == "interrupted"
        assert row.error_code == "TASK_INTERRUPTED"
        assert row.input_text == "被重启打断的文本"
        # Enough to rebuild the request on retry.
        assert row.llm_provider == "mock"
        assert row.tts_provider == "mimo"
        assert row.tts_voice == "冰糖"
        assert row.to_dict()["audio_url"] is None

    async def test_restore_is_idempotent_for_the_history_row(self, session):
        """Booting twice must not duplicate the record."""
        from app.db.models import TaskModel
        from app.services.tasks import TaskManager

        session.add(
            TaskModel(
                task_id="lost-2",
                status="pending",
                stage="queued",
                progress=0,
                request_payload=self._payload(),
            )
        )
        await session.commit()

        await TaskManager().restore_from_db()
        await TaskManager().restore_from_db()

        async with get_sessionmaker()() as fresh:
            assert await _count(fresh) == 1

    async def test_auto_save_false_records_no_interrupted_history_row(self, session):
        from app.db.models import TaskModel
        from app.services.tasks import TaskManager

        session.add(
            TaskModel(
                task_id="lost-3",
                status="running",
                stage="llm",
                progress=10,
                request_payload=self._payload(auto_save=False),
            )
        )
        await session.commit()

        await TaskManager().restore_from_db()

        async with get_sessionmaker()() as fresh:
            assert await _count(fresh) == 0


class TestSkippingTheLlmDoesNotRequireAnLlmProvider:
    """A run that never calls the LLM must not fail on the LLM provider.

    `_run_llm` used to resolve the adapter before checking `reuse_reply_text`,
    so a pure TTS run (直接配音, or re-synthesising an existing reply) failed
    with LLM_UNSUPPORTED_PROVIDER whenever `llm_provider` was stale or unknown --
    even though no LLM call is made. The frontend's `canGenerate` already treats
    the LLM as unnecessary in those modes, so the backend was contradicting it.
    """

    async def test_reuse_reply_text_works_with_an_unknown_llm_provider(self, session):
        request = GenerateRequest(
            text="直接配音文本",
            llm={"provider": "no-such-llm-provider"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True, "reuse_reply_text": "直接配音文本"},
        )

        response = await GenerationService().generate(session, request)
        await session.commit()

        assert response.reply_text == "直接配音文本"
        row = await session.get(GenerationRecord, response.record_id)
        assert row is not None
        assert row.status == "succeeded"

    async def test_unknown_llm_provider_still_fails_when_the_llm_is_needed(self, session):
        """The guard must not be weakened for runs that really do call the LLM."""
        request = GenerateRequest(
            text="需要大模型",
            llm={"provider": "no-such-llm-provider"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True},
        )

        with pytest.raises(AppError) as excinfo:
            await GenerationService().generate(session, request)
        assert excinfo.value.code.value == "LLM_UNSUPPORTED_PROVIDER"


class TestSucceededRowsAreUnaffected:
    async def test_a_successful_run_still_writes_exactly_one_row(self, session):
        request = GenerateRequest(
            text="成功路径不受影响",
            llm={"provider": "mock"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True},
        )

        response = await GenerationService().generate(session, request)
        await session.commit()

        async with get_sessionmaker()() as fresh:
            rows = (await fresh.execute(select(GenerationRecord))).scalars().all()

        assert len(rows) == 1, "a success must not also record a failure"
        assert rows[0].status == "succeeded"
        assert rows[0].id == response.record_id
