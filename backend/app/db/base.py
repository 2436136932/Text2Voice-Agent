"""Async SQLAlchemy engine, session factory and schema bootstrap.

The platform ships with SQLite by default (zero-config, single file) but the
URL is fully configurable, so pointing ``T2V_DATABASE_URL`` at PostgreSQL only
requires installing ``asyncpg`` - no code change.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _prepare_sqlite_directory(database_url: str) -> None:
    """SQLite will not create missing parent directories on its own."""
    prefix = "sqlite"
    if not database_url.startswith(prefix):
        return
    # sqlite+aiosqlite:///./data/text2voice.db  ->  ./data/text2voice.db
    _, _, raw_path = database_url.partition("///")
    if not raw_path or raw_path == ":memory:":
        return
    path = Path(raw_path)
    if not path.is_absolute():
        path = settings.resolve_path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> AsyncEngine:
    """Create (once) and return the async engine."""
    global _engine
    if _engine is None:
        database_url = settings.database_url
        _prepare_sqlite_directory(database_url)
        connect_args: dict[str, object] = {}
        kwargs: dict[str, object] = {"echo": False, "future": True}
        if database_url.startswith("sqlite"):
            # ``check_same_thread`` is required for SQLite + threadpool.
            connect_args["check_same_thread"] = False
            kwargs["pool_pre_ping"] = True
        else:
            kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True})
        _engine = create_async_engine(database_url, connect_args=connect_args, **kwargs)  # type: ignore[arg-type]
        logger.info("database engine created url=%s", _redact_url(database_url))
    return _engine


def _redact_url(url: str) -> str:
    """Hide the password inside a database URL before logging it."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    credentials, _, host = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    The session commits when the handler returns normally and rolls back on any
    exception, so a route can simply mutate ORM objects and return.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager for background workers (no FastAPI involved)."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables, indexes and the settings singleton row."""
    from app.db import models  # noqa: F401  (register metadata)

    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info("database schema ready tables=%s", sorted(Base.metadata.tables))

    async with session_scope() as session:
        await models.SettingModel.get_or_create(session)


async def dispose_db() -> None:
    """Close the connection pool (called on shutdown)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None