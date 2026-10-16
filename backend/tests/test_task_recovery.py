"""Restart recovery: orphaned tasks must become ``interrupted``, never zombies.

The task pool is process-local and the real ``GenerateRequest`` (which carries
the API keys) is only ever held in memory, so a restart makes everything that
was ``pending``/``running`` unrecoverable. Rehydrating those rows verbatim used
to resurrect them as permanent zombies: ``status='running'`` with no worker
behind it, forever counted by ``active_count()`` and forever polled by the UI.

These tests pin the intended contract: on boot the row is rewritten to the
terminal ``interrupted`` status *in the database too*, with a
``TASK_INTERRUPTED`` payload telling the user to resubmit.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.db.models import TaskModel
from app.services.tasks import INTERRUPTED_STATUS, TERMINAL_STATUSES, TaskManager, TaskState


async def add_row(session, **overrides) -> TaskModel:
    """Insert a raw ``tasks`` row, as a killed process would have left it."""
    values = {
        "task_id": overrides.pop("task_id", "row-1"),
        "status": "running",
        "stage": "tts",
        "progress": 60,
        "message": "正在合成语音…",
        "created_at": datetime.now(timezone.utc),
        "started_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    row = TaskModel(**values)
    session.add(row)
    await session.commit()
    return row


async def reload_row(session, task_id: str) -> TaskModel | None:
    """Read the row back through a fresh query (no identity-map shortcut)."""
    session.expire_all()
    return (
        await session.execute(select(TaskModel).where(TaskModel.task_id == task_id))
    ).scalar_one_or_none()


class TestRestoreOrphanedRows:
    async def test_running_row_becomes_interrupted(self, session):
        """The reproduced bug: a 'running' row with no worker must not survive."""
        await add_row(session, task_id="zombie", status="running", stage="tts", progress=60)

        manager = TaskManager()
        await manager.restore_from_db()

        state = manager.get("zombie")
        assert state.status == INTERRUPTED_STATUS
        assert state.status != "running"
        assert state.stage == "done"
        # Progress is evidence of how far the lost run got; keep it.
        assert state.progress == 60
        assert state.finished_at is not None
        assert state.handle is None

    async def test_running_row_error_payload_is_task_interrupted(self, session):
        await add_row(session, task_id="zombie-payload", status="running", progress=60)

        manager = TaskManager()
        await manager.restore_from_db()
        error = manager.get("zombie-payload").error

        assert error is not None
        assert error["code"] == ErrorCode.TASK_INTERRUPTED.value
        assert "重启" in error["message"]
        assert error["detail"]["suggestion"]
        assert error["detail"]["retryable"] is True

    async def test_pending_row_becomes_interrupted(self, session):
        """A queued task lost its payload with the process, so it is gone too."""
        await add_row(
            session,
            task_id="queued",
            status="pending",
            stage="queued",
            progress=0,
            started_at=None,
        )

        manager = TaskManager()
        await manager.restore_from_db()

        state = manager.get("queued")
        assert state.status == INTERRUPTED_STATUS
        assert state.finished_at is not None
        assert (state.error or {})["code"] == ErrorCode.TASK_INTERRUPTED.value
        # It never started, so there is no elapsed time to invent.
        assert state.elapsed_ms is None

    async def test_succeeded_row_is_restored_unchanged(self, session):
        """Regression guard: only orphaned rows may be rewritten."""
        await add_row(
            session,
            task_id="done-ok",
            status="succeeded",
            stage="done",
            progress=100,
            message="生成完成",
            finished_at=datetime.now(timezone.utc),
            result={"reply_text": "你好"},
        )

        manager = TaskManager()
        await manager.restore_from_db()

        state = manager.get("done-ok")
        assert state.status == "succeeded"
        assert state.stage == "done"
        assert state.progress == 100
        assert state.message == "生成完成"
        assert state.error is None
        assert state.result == {"reply_text": "你好"}

    async def test_failed_row_is_not_remarked(self, session):
        """A finished-with-error task already has its own error payload."""
        original = {"code": "LLM_TIMEOUT", "message": "模型超时。", "detail": {}}
        await add_row(
            session,
            task_id="failed-row",
            status="failed",
            stage="done",
            progress=15,
            error=original,
            finished_at=datetime.now(timezone.utc),
        )

        manager = TaskManager()
        await manager.restore_from_db()

        state = manager.get("failed-row")
        assert state.status == "failed"
        assert state.error == original
        assert INTERRUPTED_STATUS in TERMINAL_STATUSES

    async def test_restore_persists_the_rewrite(self, session):
        """The rewrite must reach the DB, or every boot re-marks the same row."""
        await add_row(session, task_id="persisted", status="running", progress=42)

        manager = TaskManager()
        await manager.restore_from_db()

        row = await reload_row(session, "persisted")
        assert row is not None
        assert row.status == INTERRUPTED_STATUS
        assert row.stage == "done"
        assert row.finished_at is not None
        assert (row.error or {})["code"] == ErrorCode.TASK_INTERRUPTED.value
        assert row.message

    async def test_restore_is_idempotent(self, session):
        """Booting twice must not error and must not re-mark the row."""
        await add_row(session, task_id="twice", status="running", progress=30)

        first = TaskManager()
        await first.restore_from_db()
        first_finished_at = first.get("twice").finished_at
        first_error = first.get("twice").error

        # Second boot: the row is terminal now, so it is restored verbatim.
        second = TaskManager()
        await second.restore_from_db()

        state = second.get("twice")
        assert state.status == INTERRUPTED_STATUS
        assert state.finished_at == first_finished_at
        assert state.error == first_error

        row = await reload_row(session, "twice")
        assert row is not None
        assert row.status == INTERRUPTED_STATUS
        assert row.finished_at is not None

    async def test_restore_without_orphans_is_a_noop(self, session):
        """Nothing pending/running in the table -> nothing is rewritten."""
        await add_row(
            session,
            task_id="only-done",
            status="succeeded",
            stage="done",
            progress=100,
            finished_at=datetime.now(timezone.utc),
        )

        manager = TaskManager()
        await manager.restore_from_db()

        assert manager.get("only-done").status == "succeeded"

    async def test_restored_timestamps_stay_aware(self, session):
        """Restored rows keep tz-aware datetimes (the 2026-10 sweep crash)."""
        await add_row(session, task_id="tz", status="running")

        manager = TaskManager()
        await manager.restore_from_db()

        state = manager.get("tz")
        assert state.created_at.tzinfo is not None
        assert state.finished_at is not None
        assert state.finished_at.tzinfo is not None


class TestInterruptedIsTerminal:
    async def test_active_count_ignores_interrupted(self, session):
        """The zombie used to keep active_count() at 1 forever."""
        await add_row(session, task_id="orphan", status="running")

        manager = TaskManager()
        assert manager.active_count() == 0
        await manager.restore_from_db()
        assert manager.active_count() == 0

    async def test_active_count_still_counts_real_work(self, session):
        manager = TaskManager()
        manager._tasks["live"] = TaskState(task_id="live", status="running")
        manager._tasks["queued"] = TaskState(task_id="queued", status="pending")
        manager._tasks["dead"] = TaskState(task_id="dead", status=INTERRUPTED_STATUS)

        assert manager.active_count() == 2

    async def test_interrupted_is_in_terminal_statuses(self):
        assert INTERRUPTED_STATUS in TERMINAL_STATUSES

    async def test_result_raises_task_interrupted_not_internal_error(self, session):
        """The bug report: result() fell through to 任务结果丢失 / 500."""
        await add_row(session, task_id="result-interrupted", status="running")

        manager = TaskManager()
        await manager.restore_from_db()

        with pytest.raises(AppError) as excinfo:
            manager.result("result-interrupted")

        error = excinfo.value
        assert error.code is ErrorCode.TASK_INTERRUPTED
        assert error.code is not ErrorCode.INTERNAL_ERROR
        assert error.http_status == 409
        assert error.retryable is True
        assert error.detail["suggestion"]
        assert error.detail["retryable"] is True
        assert "重新提交" in error.message

    async def test_result_error_payload_is_frontend_renderable(self, session):
        """The FE consumes error.code + error.detail.suggestion verbatim."""
        await add_row(session, task_id="result-shape", status="running")

        manager = TaskManager()
        await manager.restore_from_db()

        with pytest.raises(AppError) as excinfo:
            manager.result("result-shape")

        payload = excinfo.value.to_payload()
        assert payload["code"] == "TASK_INTERRUPTED"
        assert payload["detail"]["suggestion"]
        assert payload["detail"]["task_id"] == "result-shape"

    async def test_cancel_rejects_interrupted_as_already_finished(self, session):
        await add_row(session, task_id="cancel-interrupted", status="running")

        manager = TaskManager()
        await manager.restore_from_db()

        with pytest.raises(AppError) as excinfo:
            await manager.cancel("cancel-interrupted")

        error = excinfo.value
        assert error.code is ErrorCode.TASK_ALREADY_FINISHED
        assert error.http_status == 409
        # A rejected cancel must not mutate the row.
        assert manager.get("cancel-interrupted").status == INTERRUPTED_STATUS

    async def test_status_payload_ends_polling(self, session):
        """`interrupted` must not look like pending/running to the poller."""
        await add_row(session, task_id="poll-me", status="running")

        manager = TaskManager()
        await manager.restore_from_db()
        payload = manager.get("poll-me").to_dict()

        assert payload["status"] == INTERRUPTED_STATUS
        assert payload["status"] not in {"pending", "running"}
        assert payload["error"]["code"] == "TASK_INTERRUPTED"
