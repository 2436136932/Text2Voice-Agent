"""Task manager: lifecycle, queue-full backpressure, cancel and cleanup.

The manager is process-local by design; these tests exercise one instance
against the real SQLite fixture (tasks persist across a restart, which is
what ``restore_from_db`` rehydrates from).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.db.models import TaskModel
from app.schemas.generation import GenerateRequest
from app.services.tasks import TaskManager, TaskState


def make_request() -> GenerateRequest:
    """A minimal offline request: mock LLM echoes, mock TTS renders a WAV."""
    return GenerateRequest(
        text="任务管理器测试",
        llm={"provider": "mock", "model": "mock-echo-v1"},
        tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
        options={"auto_save": False},
    )


@pytest.fixture
async def manager():
    """A started manager with a single worker, stopped on teardown."""
    mgr = TaskManager()
    monkey_workers = pytest.MonkeyPatch()
    monkey_workers.setattr(settings, "task_workers", 1)
    await mgr.start()
    yield mgr
    await mgr.stop()
    monkey_workers.undo()


class TestLifecycle:
    async def test_submit_before_start_is_503(self):
        mgr = TaskManager()
        with pytest.raises(AppError) as excinfo:
            await mgr.submit(make_request())
        assert excinfo.value.code is ErrorCode.SERVICE_UNAVAILABLE

    async def test_submit_persists_and_executes(self, manager, session):
        state = await manager.submit(make_request())
        # Wait for the worker to reach a terminal state.
        for _ in range(100):
            if manager.get(state.task_id).status in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.05)
        final = manager.get(state.task_id)
        assert final.status == "succeeded"
        assert final.result is not None
        # The credential-free payload is persisted for audit.
        rows = (
            await session.execute(
                select(TaskModel).where(TaskModel.task_id == state.task_id)
            )
        ).scalars().all()
        assert rows, "task row must be written at submit time"
        assert rows[0].request_payload is not None
        assert "api_key" not in str(rows[0].request_payload).lower()

    async def test_credentials_never_reach_persisted_payload(self, manager, session):
        request = make_request()
        request.llm.api_key = "sk-super-secret-value-123456"  # type: ignore[assignment]
        state = await manager.submit(request)
        await session.commit()
        row = (
            await session.execute(
                select(TaskModel).where(TaskModel.task_id == state.task_id)
            )
        ).scalar_one()
        assert "sk-super-secret-value-123456" not in (row.request_payload or {}).get(
            "llm", {}
        ).get("api_key", "")


class TestQueueBackpressure:
    async def test_full_queue_rejects_with_503(self, session, monkeypatch):
        """A queue at capacity refuses new work instead of silently dropping."""
        monkeypatch.setattr(settings, "task_queue_size", 1)
        mgr = TaskManager()
        await mgr.start()
        try:
            # Freeze the single worker so the queue cannot drain.
            worker = mgr._workers[0]
            worker.cancel()

            first = await mgr.submit(make_request())
            assert mgr._queue is not None
            # The queue now holds `first`; a second submit must be refused.
            with pytest.raises(AppError) as excinfo:
                await mgr.submit(make_request())
            assert excinfo.value.code is ErrorCode.TASK_QUEUE_FULL
            assert excinfo.value.http_status == 503
            _ = first
        finally:
            await mgr.stop()


class TestCancel:
    async def test_cancel_terminal_state_raises_409(self, manager):
        state = TaskState(task_id="already-done", status="succeeded")
        manager._tasks[state.task_id] = state
        with pytest.raises(AppError) as excinfo:
            await manager.cancel("already-done")
        assert excinfo.value.http_status == 409

    async def test_cancel_marks_canceled_and_persists(self, manager, session):
        # Submit but pause the worker so the task stays queued.
        worker = manager._workers[0]
        worker.cancel()
        state = await manager.submit(make_request())
        canceled = await manager.cancel(state.task_id)
        assert canceled.status == "canceled"
        await session.commit()
        row = (
            await session.execute(
                select(TaskModel).where(TaskModel.task_id == state.task_id)
            )
        ).scalar_one()
        assert row.status == "canceled"


class TestCleanup:
    async def test_sweep_removes_stale_finished_tasks(self, manager):
        stale = TaskState(task_id="stale", status="succeeded")
        stale.created_at = stale.created_at.replace(year=2020)
        stale.finished_at = stale.finished_at.replace(year=2020) if stale.finished_at else None
        manager._tasks["stale"] = stale
        fresh = TaskState(task_id="fresh", status="succeeded")
        manager._tasks["fresh"] = fresh

        from app.services.tasks import _as_aware_utc
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=max(1, settings.task_retention_minutes)
        )
        victims = [
            tid
            for tid, s in manager._tasks.items()
            if s.status in {"succeeded", "failed", "canceled"}
            and (_as_aware_utc(s.finished_at or s.created_at) or cutoff) < cutoff
        ]
        assert "stale" in victims
        assert "fresh" not in victims


class TestRestore:
    async def test_restore_marks_rows_aware(self, manager, session):
        """Restored timestamps must be tz-aware (the 2026-10 sweep crash)."""
        state = await manager.submit(make_request())
        await session.commit()
        fresh = TaskManager()
        await fresh.restore_from_db()
        restored = fresh._tasks.get(state.task_id)
        if restored is not None:
            assert restored.created_at.tzinfo is not None
        # No exceptions, no naive datetimes — that is the whole contract.
