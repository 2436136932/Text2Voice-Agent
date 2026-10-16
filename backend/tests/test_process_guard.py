"""Multi-process deployment must be refused, not silently broken.

The task manager, rate limiter and metrics are in-process state. With
``workers > 1`` each process gets its own copy: tasks vanish from
``GET /tasks/{id}``, ``restore_from_db`` re-runs the same jobs, and rate
limits multiply. A loud refusal is the only safe behaviour.
"""

from __future__ import annotations

import pytest

from app.core.config import settings


class TestSingleProcessGuard:
    def test_single_worker_is_allowed(self, monkeypatch):
        from app.api.app import _assert_single_process

        monkeypatch.setattr(settings, "workers", 1)
        _assert_single_process()  # must not raise

    def test_multiple_workers_is_refused(self, monkeypatch):
        from app.api.app import _assert_single_process

        monkeypatch.setattr(settings, "workers", 4)
        with pytest.raises(RuntimeError) as excinfo:
            _assert_single_process()
        message = str(excinfo.value)
        # The error must name the setting to change, not just say "invalid".
        assert "T2V_WORKERS" in message
        assert "T2V_TASK_WORKERS" in message

    def test_zero_workers_is_treated_as_single(self, monkeypatch):
        """A misconfigured 0 must not trip the guard (it means 'one')."""
        from app.api.app import _assert_single_process

        monkeypatch.setattr(settings, "workers", 0)
        _assert_single_process()

    async def test_lifespan_refuses_before_touching_state(self, monkeypatch):
        """Startup aborts before the DB/task manager are initialised."""
        from app.api import app as app_module

        monkeypatch.setattr(settings, "workers", 3)
        started = {"db": False}

        async def fake_init_db():
            started["db"] = True

        monkeypatch.setattr(app_module, "init_db", fake_init_db)

        # Entering the context manager runs the startup half of lifespan.
        with pytest.raises(RuntimeError):
            async with app_module.lifespan(app_module.create_app()):
                pass

        assert started["db"] is False, "guard must run before any shared state"
