"""Pytest fixtures: isolated settings, temp database, async test client."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Make ``import app...`` work regardless of where pytest is invoked from.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# ---------------------------------------------------------------------------
# Environment must be set BEFORE ``app.core.config`` is imported anywhere, so
# that the settings singleton (an lru_cache) picks up the test values.
# ---------------------------------------------------------------------------
_TMP_DIR = Path(tempfile.mkdtemp(prefix="t2v-tests-"))
os.environ.update(
    {
        "T2V_ENVIRONMENT": "development",
        "T2V_DEBUG": "false",
        "T2V_DATABASE_URL": f"sqlite+aiosqlite:///{(_TMP_DIR / 'test.db').as_posix()}",
        "T2V_AUDIO_DIR": str(_TMP_DIR / "audio"),
        "T2V_LOG_LEVEL": "WARNING",
        "T2V_RATE_LIMIT_ENABLED": "false",
        "T2V_UPSTREAM_MAX_RETRIES": "1",
        "T2V_UPSTREAM_BACKOFF_BASE_SECONDS": "0.01",
        "T2V_UPSTREAM_BACKOFF_MAX_SECONDS": "0.05",
        "T2V_TASK_WORKERS": "2",
    }
)

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.api.app import create_app  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.base import dispose_db, get_engine, get_sessionmaker, init_db  # noqa: E402
from app.services.audio import AudioStore, audio_store  # noqa: E402
from app.services.tasks import task_manager  # noqa: E402


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped loop so the app lifespan and engine stay consistent."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest_asyncio.fixture(scope="function")
async def app(settings):
    """Fresh application + schema per test.

    Each test gets its own SQLite file in a unique temp directory. Deleting a
    shared file between tests is unreliable on Windows (the aiosqlite worker
    thread can still hold a handle), so isolation is achieved by using a new
    path instead of unlinking the old one.
    """
    await dispose_db()
    per_test_dir = Path(tempfile.mkdtemp(prefix="t2v-db-"))
    settings.database_url = f"sqlite+aiosqlite:///{(per_test_dir / 'test.db').as_posix()}"
    settings.audio_dir = str(per_test_dir / "audio")

    application = create_app()
    await init_db()
    await task_manager.start()
    try:
        yield application
    finally:
        await task_manager.stop()
        await dispose_db()
        shutil.rmtree(per_test_dir, ignore_errors=True)


@pytest_asyncio.fixture(scope="function")
async def client(app):
    """``httpx.AsyncClient`` wired to the ASGI app (no network, no server)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30.0
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture(scope="function")
async def session(app):
    """Direct DB session for service-level tests."""
    async with get_sessionmaker()() as db_session:
        yield db_session
        await db_session.rollback()


@pytest.fixture(scope="function")
def tmp_audio_store(tmp_path) -> AudioStore:
    return AudioStore(root=tmp_path / "audio")


@pytest.fixture(autouse=True, scope="function")
def _reset_audio_dir(settings):
    """Keep the audio directory clean between tests."""
    target = settings.audio_path
    if target.exists():
        for item in target.glob("*"):
            if item.is_file():
                item.unlink()
    yield


def pytest_configure(config):  # pragma: no cover - reporting helper
    config.addinivalue_line("markers", "slow: long running test")