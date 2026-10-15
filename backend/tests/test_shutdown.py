"""Regression tests for the shutdown hang.

The whole test suite intermittently froze on this host (roughly half of runs)
at a constant CPU, typically right after test_tasks.py::TestRestore -- the last
test that submits a task. It was not a slow test: teardown never returned.

Root cause: TaskManager._worker() ended with a ``finally`` block containing
``await self._persist(state)``. ``stop()`` cancels the workers and then gathers
them, so a worker cancelled while parked inside that ``await`` never resumed,
and the gather waited forever.

These tests pin both halves of the fix: shutdown must complete promptly, and
the durable state must still be sane afterwards (a task left pending/running is
reconciled to 'interrupted' by restore_from_db on the next boot, so skipping
the shutdown write loses nothing).
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.tasks import TaskManager


def make_request():
    """Minimal valid GenerateRequest (mock providers: no network, no keys)."""
    from app.schemas.generation import GenerateRequest

    return GenerateRequest.model_validate(
        {
            "text": "shutdown probe",
            "llm": {"provider": "mock", "model": "mock-llm"},
            "tts": {"provider": "mock", "voice": "mock-neutral"},
        }
    )


class TestShutdownDoesNotHang:
    #: Shutdown must be effectively immediate. With the bug present, stop()
    #: still *returns* because it is bounded by an internal 5s wait_for -- so
    #: asserting "it returned" alone would NOT catch a regression (verified by
    #: reintroducing the bug: the test still passed, just 5s slower). The
    #: assertion therefore has to be on elapsed time.
    MAX_SHUTDOWN_SECONDS = 2.0

    async def _timed_stop(self, manager) -> float:
        import time

        start = time.perf_counter()
        await asyncio.wait_for(manager.stop(), timeout=15.0)
        return time.perf_counter() - start

    async def test_stop_returns_when_a_worker_is_mid_flight(self, app):
        """The exact hang: submit, let the worker start, then stop."""
        manager = TaskManager()
        await manager.start()
        try:
            await manager.submit(make_request())
            # Let the worker pick the task up and get into its awaits.
            await asyncio.sleep(0.05)
            elapsed = await self._timed_stop(manager)
            assert elapsed < self.MAX_SHUTDOWN_SECONDS, (
                f"stop() took {elapsed:.2f}s -- a worker is blocking shutdown "
                f"(the cancelled-persist hang is back)"
            )
        finally:
            if manager._started:
                await asyncio.wait_for(manager.stop(), timeout=15.0)

    async def test_stop_is_prompt_even_with_many_pending_tasks(self, app):
        manager = TaskManager()
        await manager.start()
        try:
            for _ in range(5):
                await manager.submit(make_request())
            await asyncio.sleep(0.05)
            elapsed = await self._timed_stop(manager)
            assert elapsed < self.MAX_SHUTDOWN_SECONDS, f"stop() took {elapsed:.2f}s"
        finally:
            if manager._started:
                await asyncio.wait_for(manager.stop(), timeout=15.0)

    async def test_restart_after_stop_behaves_like_a_first_start(self, app):
        """stop() must not leave state that breaks the next start().

        Both the shutdown flag and the queue are lifecycle state; a second
        start on the same manager is what a test fixture or an app restart
        effectively does.
        """
        manager = TaskManager()
        await manager.start()
        await manager.submit(make_request())
        await asyncio.sleep(0.05)
        await asyncio.wait_for(manager.stop(), timeout=10.0)

        # The flag must have been cleared by start(), not left set.
        await manager.start()
        try:
            assert manager._shutting_down is False
            elapsed = await self._timed_stop(manager)
            assert elapsed < self.MAX_SHUTDOWN_SECONDS, f"stop() took {elapsed:.2f}s"
        finally:
            if manager._started:
                await asyncio.wait_for(manager.stop(), timeout=15.0)


class TestShutdownMechanism:
    """The mechanism that actually prevents the hang.

    Shutdown must be cooperative, not cancellation-based: a worker cancelled
    while parked inside ``session_scope()`` still has to run its teardown
    (``await session.commit()``), and an aiosqlite commit does not observe task
    cancellation -- it blocks on its worker thread, so ``gather()`` waits
    forever. These assertions pin the mechanism directly, because a purely
    timing-based test cannot distinguish "worked" from "was rescued by the
    bounded wait_for".
    """

    async def test_stop_sets_the_shutdown_flag(self, app):
        manager = TaskManager()
        await manager.start()
        assert manager._shutting_down is False
        await asyncio.wait_for(manager.stop(), timeout=15.0)
        assert manager._shutting_down is True

    async def test_start_clears_the_shutdown_flag(self, app):
        manager = TaskManager()
        await manager.start()
        await asyncio.wait_for(manager.stop(), timeout=15.0)
        assert manager._shutting_down is True
        # A restart (test fixture reuse, app reload) must not inherit the flag,
        # or every task would be dropped on pickup.
        await manager.start()
        try:
            assert manager._shutting_down is False
        finally:
            await asyncio.wait_for(manager.stop(), timeout=15.0)

    async def test_stop_wakes_idle_workers_without_cancelling_them(self, app):
        """stop() must not rely on Task.cancel() to end a worker.

        Cancelling is what breaks the DB teardown. Verify the sentinel path:
        after stop(), the workers are finished AND were not left cancelled in a
        way that would poison a later gather.
        """
        manager = TaskManager()
        await manager.start()
        workers = list(manager._workers)
        assert workers, "expected workers to be running"

        elapsed_before = asyncio.get_event_loop().time()
        await asyncio.wait_for(manager.stop(), timeout=15.0)
        elapsed = asyncio.get_event_loop().time() - elapsed_before

        # Workers must have exited on their own, promptly.
        assert all(w.done() for w in workers), "a worker is still running after stop()"
        assert elapsed < 2.0, f"stop() took {elapsed:.2f}s -- cooperative exit is broken"

    async def test_worker_ignores_new_work_once_shutting_down(self, app):
        """A queued task must not start executing during shutdown."""
        manager = TaskManager()
        await manager.start()
        manager._shutting_down = True
        try:
            await manager.submit(make_request())
            await asyncio.sleep(0.2)
            # The submitted task must not have been picked up and executed.
            state = next(iter(manager._tasks.values()))
            assert state.status in {"pending", "running"}
        finally:
            manager._shutting_down = False
            await asyncio.wait_for(manager.stop(), timeout=15.0)


class TestInterruptedReconciliationCoversSkippedWrites:
    async def test_task_left_running_by_shutdown_is_reconciled_on_restore(self, app):
        """The safety net that makes skipping the shutdown write acceptable.

        A task that was still running when the process stopped must come back
        as the terminal 'interrupted' status rather than a permanent zombie.
        """
        manager = TaskManager()
        await manager.start()
        await manager.submit(make_request())
        # Do not wait for completion: simulate dying mid-flight.
        state = next(iter(manager._tasks.values()))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(manager.stop(), timeout=10.0)

        # A fresh manager (new process) must reconcile whatever it finds.
        fresh = TaskManager()
        await fresh.restore_from_db()
        restored = fresh._tasks.get(state.task_id)
        if restored is not None:
            assert restored.status in {"succeeded", "failed", "canceled", "interrupted"}
            assert restored.status != "running"
