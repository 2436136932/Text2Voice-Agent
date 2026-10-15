"""Async SQLAlchemy engine, session factory and schema bootstrap.

The platform ships with SQLite by default (zero-config, single file) but the
URL is fully configurable, so pointing ``T2V_DATABASE_URL`` at PostgreSQL only
requires installing ``asyncpg`` - no code change.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
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


# ---------------------------------------------------------------------------
# Lightweight forward migration
# ---------------------------------------------------------------------------
#: Columns added to a pre-existing table *after* it shipped. ``create_all``
#: only emits ``CREATE TABLE`` for tables that do not exist yet, so a table
#: created by an older build keeps its old shape and every query touching a new
#: column fails with ``sqlite3.OperationalError: no such column``. A real
#: ``data/text2voice.db`` therefore needs an explicit ``ALTER TABLE``.
#:
#: Keep this list append-only: ``(table, column, DDL type + default)``. Every
#: entry must be nullable or carry a SQL ``DEFAULT`` that matches the ORM
#: default, because SQLite backfills existing rows from that default.
_COLUMN_UPGRADES: tuple[tuple[str, str, str], ...] = (
    ("generation_records", "favorite", "BOOLEAN NOT NULL DEFAULT 0"),
    ("generation_records", "tags", "JSON NOT NULL DEFAULT '[]'"),
)


def _quote_sqlite_identifier(name: str) -> str:
    """Quote an identifier for DDL interpolation.

    Names come from the hard-coded list above, never from user input; the
    guard exists so a future typo fails loudly instead of producing invalid SQL.
    """
    if not name or not all(char.isalnum() or char == "_" for char in name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def _ensure_columns_sync(connection: Connection) -> list[str]:
    """Add every missing column to SQLite tables. Returns ``table.column`` names added.

    Must run inside a transaction *after* ``create_all``: for a brand-new
    database the tables already have the columns and this is a no-op; for an
    existing one the ``ALTER TABLE`` is what keeps the data readable. Nothing
    is ever dropped, recreated or deleted here.
    """
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    for table, column, ddl in _COLUMN_UPGRADES:
        if table not in existing_tables:
            continue  # create_all already emitted the current shape
        current = {item["name"] for item in inspector.get_columns(table)}
        if column in current:
            continue
        connection.exec_driver_sql(
            f"ALTER TABLE {_quote_sqlite_identifier(table)} "
            f"ADD COLUMN {_quote_sqlite_identifier(column)} {ddl}"
        )
        logger.info("schema migration: added column %s.%s", table, column)
        added.append(f"{table}.{column}")

    return added


async def ensure_schema_columns(engine: AsyncEngine | None = None) -> list[str]:
    """Forward-migrate an existing database so old tables gain new columns.

    Safe to call on every startup: it only ever issues ``ALTER TABLE ADD
    COLUMN`` for columns that are genuinely absent.
    """
    target = engine or get_engine()
    async with target.begin() as connection:
        # SQLite is the only backend that needs this today (PostgreSQL would
        # need ``IF NOT EXISTS``); the inspector call is portable either way.
        return await connection.run_sync(_ensure_columns_sync)


async def init_db() -> None:
    """Create tables, indexes and the settings singleton row."""
    from app.db import models  # noqa: F401  (register metadata)

    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    # Backfill columns on tables that predate them (see _COLUMN_UPGRADES).
    added = await ensure_schema_columns(engine)
    logger.info(
        "database schema ready tables=%s migrated_columns=%s",
        sorted(Base.metadata.tables),
        added,
    )

    async with session_scope() as session:
        await models.SettingModel.get_or_create(session)


async def dispose_db() -> None:
    """Close the connection pool (called on shutdown)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None