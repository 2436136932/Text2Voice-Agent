"""History favourites, tags and text search.

Covers the additions from the "history metadata" work:

* ``GET /history?q=`` matching both text columns, with ``%``/``_`` treated as
  literal characters rather than SQL wildcards;
* the ``favorite`` and ``tags`` filters;
* ``PATCH /history/{id}`` with its tag normalization and error contract;
* ``favorite``/``tags`` present in both list and detail payloads;
* the **startup column migration** that keeps an existing database (one created
  by an older build, without the new columns) working. That last test builds a
  real on-disk SQLite file in the old shape with data in it, runs the actual
  startup path against it and asserts the columns appear *and* the data
  survives -- the exact scenario the user's ``data/text2voice.db`` is in.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import ensure_schema_columns, init_db
from app.schemas.generation import MAX_TAG_COUNT, MAX_TAG_LENGTH, normalize_tags
#: Applied per class below rather than module-wide: the sync helper tests would
#: otherwise carry an asyncio mark, which pytest-asyncio warns about.
asyncio_mark = pytest.mark.asyncio

MOCK_LLM = {"provider": "mock", "model": "mock-echo-v1"}
MOCK_TTS = {"provider": "mock", "voice": "mock-neutral", "format": "wav"}

#: The columns an *older* ``generation_records`` had, i.e. everything the model
#: declared before favourites/tags were introduced.
LEGACY_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("input_text", "TEXT NOT NULL"),
    ("reply_text", "TEXT NOT NULL DEFAULT ''"),
    ("llm_provider", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("llm_model", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ("tts_provider", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("tts_voice", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ("tts_model", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ("key_fingerprint", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("audio_format", "VARCHAR(16) NOT NULL DEFAULT 'mp3'"),
    ("audio_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("audio_path", "VARCHAR(512) NOT NULL DEFAULT ''"),
    ("duration_ms", "INTEGER"),
    ("llm_latency_ms", "INTEGER"),
    ("tts_latency_ms", "INTEGER"),
    ("total_latency_ms", "INTEGER"),
    ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("total_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("status", "VARCHAR(16) NOT NULL DEFAULT 'succeeded'"),
    ("degraded", "BOOLEAN NOT NULL DEFAULT 0"),
    ("degraded_reason", "VARCHAR(512)"),
    ("error_code", "VARCHAR(64)"),
    ("error_message", "TEXT"),
    ("request_id", "VARCHAR(64)"),
    ("created_at", "DATETIME NOT NULL"),
]


def generate_body(**overrides) -> dict:
    body = {
        "text": "请用一句话介绍杭州西湖。",
        "llm": dict(MOCK_LLM),
        "tts": dict(MOCK_TTS),
        "options": {"auto_save": True, "fallback_enabled": True},
    }
    body.update(overrides)
    return body


async def _create(client, text: str = "测试文本") -> dict:
    response = await client.post("/api/v1/generate", json=generate_body(text=text))
    assert response.status_code == 200, response.text
    return response.json()


def _make_legacy_db(path: Path, rows: int = 3) -> None:
    """Create ``path`` with the pre-favourites table shape and some data."""
    columns = ", ".join(f"{name} {ddl}" for name, ddl in LEGACY_COLUMNS)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"CREATE TABLE generation_records ({columns})")
        for index in range(rows):
            conn.execute(
                "INSERT INTO generation_records "
                "(input_text, reply_text, status, audio_bytes, created_at, total_tokens) "
                "VALUES (?, ?, 'succeeded', ?, ?, ?)",
                (f"旧记录 {index}", f"旧回复 {index}", 1000 + index, "2026-01-01 00:00:00", index),
            )
        conn.commit()
        # Sanity-check the fixture really is the old shape.
        current = {row[1] for row in conn.execute("PRAGMA table_info(generation_records)")}
        assert "favorite" not in current
        assert "tags" not in current
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pure helper (sync: the module-level asyncio mark is applied class by class
# below, because pytest-asyncio warns on a mark that targets sync functions).
# ---------------------------------------------------------------------------
class TestNormalizeTags:
    def test_trims_and_drops_empties(self):
        assert normalize_tags(["  工作 ", "", "   ", "日报"]) == ["工作", "日报"]

    def test_dedupes_keeping_first_order(self):
        assert normalize_tags(["b", "a", "b", " a "]) == ["b", "a"]

    def test_caps_each_tag_length(self):
        with pytest.raises(ValueError):
            normalize_tags(["x" * (MAX_TAG_LENGTH + 1)])

    def test_allows_max_length_tag(self):
        assert normalize_tags(["x" * MAX_TAG_LENGTH]) == ["x" * MAX_TAG_LENGTH]

    def test_caps_tag_count(self):
        with pytest.raises(ValueError):
            normalize_tags([f"t{index}" for index in range(MAX_TAG_COUNT + 1)])

    def test_rejects_non_list(self):
        with pytest.raises(ValueError):
            normalize_tags("工作")

    def test_rejects_non_string_entries(self):
        with pytest.raises(ValueError):
            normalize_tags(["ok", 3])


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
@asyncio_mark
class TestHistorySearch:
    async def test_matches_input_text(self, client):
        await _create(client, "关于杭州西湖的介绍")
        await _create(client, "关于北京故宫的介绍")
        payload = (await client.get("/api/v1/history?q=西湖")).json()
        assert payload["total"] == 1
        assert "西湖" in payload["items"][0]["input_text"]

    async def test_matches_reply_text(self, client):
        """The mock LLM echoes the prompt, so search must cover reply_text too."""
        created = await _create(client, "独一无二的回复关键字")
        detail = (await client.get(f"/api/v1/history/{created['record_id']}")).json()
        assert "独一无二的回复关键字" in detail["reply_text"]

        payload = (await client.get("/api/v1/history?q=独一无二的回复关键字")).json()
        assert payload["total"] == 1

    async def test_search_is_case_insensitive(self, client):
        await _create(client, "Hello World Topic")
        assert (await client.get("/api/v1/history?q=hello world")).json()["total"] == 1
        assert (await client.get("/api/v1/history?q=HELLO WORLD")).json()["total"] == 1

    async def test_literal_percent_is_not_a_wildcard(self, client):
        await _create(client, "折扣 100% 优惠")
        await _create(client, "没有百分号的记录")
        payload = (await client.get("/api/v1/history", params={"q": "%"})).json()
        assert payload["total"] == 1
        assert "%" in payload["items"][0]["input_text"]

    async def test_literal_underscore_is_not_a_wildcard(self, client):
        await _create(client, "变量名 snake_case 示例")
        await _create(client, "没有下划线")
        payload = (await client.get("/api/v1/history", params={"q": "_"})).json()
        assert payload["total"] == 1
        assert "_" in payload["items"][0]["input_text"]

    async def test_literal_percent_in_phrase(self, client):
        await _create(client, "增长 50% 同比")
        await _create(client, "增长 50 同比")
        payload = (await client.get("/api/v1/history", params={"q": "50%"})).json()
        assert payload["total"] == 1
        assert "50%" in payload["items"][0]["input_text"]

    async def test_search_without_match_returns_empty(self, client):
        await _create(client, "一条普通记录")
        payload = (await client.get("/api/v1/history?q=绝对不存在的词")).json()
        assert payload["total"] == 0
        assert payload["items"] == []


# ---------------------------------------------------------------------------
# Favourites
# ---------------------------------------------------------------------------
@asyncio_mark
class TestHistoryFavorite:
    async def test_defaults_to_false_and_empty_tags(self, client):
        created = await _create(client)
        payload = (await client.get(f"/api/v1/history/{created['record_id']}")).json()
        assert payload["favorite"] is False
        assert payload["tags"] == []

    async def test_items_include_metadata_fields(self, client):
        await _create(client)
        item = (await client.get("/api/v1/history")).json()["items"][0]
        assert "favorite" in item
        assert "tags" in item

    async def test_patch_sets_favorite(self, client):
        created = await _create(client)
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}", json={"favorite": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["favorite"] is True

        again = (await client.get(f"/api/v1/history/{created['record_id']}")).json()
        assert again["favorite"] is True

    async def test_patch_clears_favorite(self, client):
        created = await _create(client)
        await client.patch(f"/api/v1/history/{created['record_id']}", json={"favorite": True})
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}", json={"favorite": False}
        )
        assert response.json()["favorite"] is False

    async def test_favorite_filter(self, client):
        first = await _create(client, "第一条")
        await _create(client, "第二条")
        await client.patch(f"/api/v1/history/{first['record_id']}", json={"favorite": True})

        only_favorites = (await client.get("/api/v1/history?favorite=true")).json()
        assert only_favorites["total"] == 1
        assert only_favorites["items"][0]["id"] == first["record_id"]

        others = (await client.get("/api/v1/history?favorite=false")).json()
        assert others["total"] == 1
        assert others["items"][0]["id"] != first["record_id"]

    async def test_favorite_absent_means_no_filter(self, client):
        await _create(client, "第一条")
        await _create(client, "第二条")
        assert (await client.get("/api/v1/history")).json()["total"] == 2


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
@asyncio_mark
class TestHistoryTags:
    async def test_patch_sets_tags(self, client):
        created = await _create(client)
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}",
            json={"tags": ["工作", "日报"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["tags"] == ["工作", "日报"]

        again = (await client.get(f"/api/v1/history/{created['record_id']}")).json()
        assert again["tags"] == ["工作", "日报"]

    async def test_patch_normalizes_tags(self, client):
        created = await _create(client)
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}",
            json={"tags": ["  工作 ", "工作", "", "   ", "日报"]},
        )
        assert response.json()["tags"] == ["工作", "日报"]

    async def test_patch_replaces_tag_list(self, client):
        created = await _create(client)
        await client.patch(f"/api/v1/history/{created['record_id']}", json={"tags": ["a"]})
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}", json={"tags": ["b", "c"]}
        )
        assert response.json()["tags"] == ["b", "c"]

    async def test_favorite_and_tags_together(self, client):
        created = await _create(client)
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}",
            json={"favorite": True, "tags": ["重点"]},
        )
        payload = response.json()
        assert payload["favorite"] is True
        assert payload["tags"] == ["重点"]

    async def test_partial_update_leaves_the_other_field_alone(self, client):
        created = await _create(client)
        await client.patch(f"/api/v1/history/{created['record_id']}", json={"tags": ["保留"]})
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}", json={"favorite": True}
        )
        payload = response.json()
        assert payload["favorite"] is True
        assert payload["tags"] == ["保留"]

    async def test_tag_over_length_is_rejected(self, client):
        created = await _create(client)
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}",
            json={"tags": ["x" * (MAX_TAG_LENGTH + 1)]},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_too_many_tags_is_rejected(self, client):
        created = await _create(client)
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}",
            json={"tags": [f"t{index}" for index in range(MAX_TAG_COUNT + 1)]},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_non_list_tags_is_rejected(self, client):
        created = await _create(client)
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}", json={"tags": "工作"}
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_non_string_tag_is_rejected(self, client):
        created = await _create(client)
        response = await client.patch(
            f"/api/v1/history/{created['record_id']}", json={"tags": ["ok", 3]}
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_empty_patch_is_rejected(self, client):
        created = await _create(client)
        response = await client.patch(f"/api/v1/history/{created['record_id']}", json={})
        assert response.status_code == 422

    async def test_tags_filter_repeated_param(self, client):
        first = await _create(client, "第一条")
        second = await _create(client, "第二条")
        await client.patch(f"/api/v1/history/{first['record_id']}", json={"tags": ["工作"]})
        await client.patch(f"/api/v1/history/{second['record_id']}", json={"tags": ["生活"]})

        payload = (await client.get("/api/v1/history?tags=工作&tags=生活")).json()
        assert payload["total"] == 2

        only_work = (await client.get("/api/v1/history?tags=工作")).json()
        assert only_work["total"] == 1
        assert only_work["items"][0]["id"] == first["record_id"]

    async def test_tags_filter_comma_separated(self, client):
        first = await _create(client, "第一条")
        second = await _create(client, "第二条")
        await client.patch(f"/api/v1/history/{first['record_id']}", json={"tags": ["工作"]})
        await client.patch(f"/api/v1/history/{second['record_id']}", json={"tags": ["生活"]})

        payload = (await client.get("/api/v1/history", params={"tags": "工作,生活"})).json()
        assert payload["total"] == 2

    async def test_tags_filter_matches_whole_tag_only(self, client):
        """A tag must not match a longer tag that merely contains it."""
        first = await _create(client, "第一条")
        await client.patch(f"/api/v1/history/{first['record_id']}", json={"tags": ["本周日报"]})
        assert (await client.get("/api/v1/history?tags=日报")).json()["total"] == 0
        assert (await client.get("/api/v1/history?tags=本周日报")).json()["total"] == 1

    async def test_tags_filter_combines_with_favorite(self, client):
        first = await _create(client, "第一条")
        second = await _create(client, "第二条")
        await client.patch(
            f"/api/v1/history/{first['record_id']}", json={"favorite": True, "tags": ["工作"]}
        )
        await client.patch(f"/api/v1/history/{second['record_id']}", json={"tags": ["工作"]})

        payload = (await client.get("/api/v1/history?favorite=true&tags=工作")).json()
        assert payload["total"] == 1
        assert payload["items"][0]["id"] == first["record_id"]

    async def test_tags_containing_wildcards_are_literal(self, client):
        first = await _create(client, "第一条")
        second = await _create(client, "第二条")
        await client.patch(f"/api/v1/history/{first['record_id']}", json={"tags": ["100%"]})
        await client.patch(f"/api/v1/history/{second['record_id']}", json={"tags": ["100分"]})

        payload = (await client.get("/api/v1/history", params={"tags": "100%"})).json()
        assert payload["total"] == 1
        assert payload["items"][0]["id"] == first["record_id"]


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------
@asyncio_mark
class TestHistoryUpdateErrors:
    async def test_patch_unknown_id_is_404(self, client):
        response = await client.patch("/api/v1/history/999999", json={"favorite": True})
        assert response.status_code == 404
        # Reuses the existing history not-found code rather than inventing one.
        assert response.json()["code"] == "NOT_FOUND"

    async def test_patch_unknown_id_does_not_create_a_row(self, client):
        await client.patch("/api/v1/history/424242", json={"tags": ["x"]})
        assert (await client.get("/api/v1/history")).json()["total"] == 0


# ---------------------------------------------------------------------------
# Retention / bulk delete must be unaffected
# ---------------------------------------------------------------------------
@asyncio_mark
class TestMetadataDoesNotBreakMaintenance:
    async def test_bulk_delete_still_works_with_metadata(self, client):
        first = await _create(client, "第一条")
        second = await _create(client, "第二条")
        await client.patch(
            f"/api/v1/history/{first['record_id']}", json={"favorite": True, "tags": ["工作"]}
        )
        response = await client.request(
            "DELETE",
            "/api/v1/history",
            json={"ids": [first["record_id"], second["record_id"]]},
        )
        assert response.status_code == 200
        assert response.json()["deleted"] == 2
        assert (await client.get("/api/v1/history")).json()["total"] == 0

    async def test_stats_and_export_ignore_new_columns(self, client):
        created = await _create(client)
        await client.patch(
            f"/api/v1/history/{created['record_id']}",
            json={"favorite": True, "tags": ["工作"]},
        )
        stats = (await client.get("/api/v1/history/stats")).json()
        assert stats["total"] == 1
        export = await client.get("/api/v1/history/export")
        assert export.status_code == 200
        # CSV shape is unchanged by the new columns (same original header).
        header = export.text.lstrip("\ufeff").splitlines()[0]
        assert header.split(",")[:4] == ["id", "created_at", "status", "llm_provider"]


# ---------------------------------------------------------------------------
# Existing-database safety: the startup column migration
# ---------------------------------------------------------------------------
@asyncio_mark
class TestLegacyDatabaseMigration:
    async def test_columns_are_added_and_data_survives(self, tmp_path, monkeypatch):
        """A DB created *without* the new columns must keep working.

        This mirrors the user's real ``data/text2voice.db``: a table in the old
        shape holding real rows. The startup path (``init_db`` -> ``ALTER TABLE``)
        must add ``favorite``/``tags`` without losing anything.
        """
        db_path = tmp_path / "legacy.db"
        _make_legacy_db(db_path, rows=3)

        # Point the app's settings at the legacy file, exactly like a restart
        # against the user's existing database.
        from app.core.config import get_settings

        monkeypatch.setattr(
            get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path.as_posix()}"
        )

        # A fresh engine, since the shared one is cached and bound elsewhere.
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True)
        try:
            added = await ensure_schema_columns(engine)
            assert sorted(added) == ["generation_records.favorite", "generation_records.tags"]

            # Running it twice must be a no-op (idempotent startup).
            assert await ensure_schema_columns(engine) == []
        finally:
            await engine.dispose()

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_records)")}
            assert {"favorite", "tags"} <= columns
            # The pre-existing rows are still there, with sane defaults.
            rows = conn.execute(
                "SELECT input_text, reply_text, favorite, tags FROM generation_records "
                "ORDER BY id"
            ).fetchall()
            assert len(rows) == 3
            assert rows[0][0] == "旧记录 0"
            assert rows[0][1] == "旧回复 0"
            assert rows[0][2] == 0
            assert rows[0][3] in ("[]", None)
        finally:
            conn.close()

    async def test_init_db_migrates_legacy_db_end_to_end(self, tmp_path, monkeypatch):
        """``init_db`` (the real startup entry point) migrates an old DB.

        Uses the global engine that ``init_db`` builds from ``settings``, so the
        test exercises the exact code path a server restart triggers.
        """
        db_path = tmp_path / "legacy-init.db"
        _make_legacy_db(db_path, rows=2)

        from app.core.config import get_settings
        from app.db.base import dispose_db

        await dispose_db()
        monkeypatch.setattr(
            get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path.as_posix()}"
        )
        try:
            await init_db()
        finally:
            await dispose_db()

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_records)")}
            assert {"favorite", "tags"} <= columns
            assert (
                conn.execute("SELECT COUNT(*) FROM generation_records").fetchone()[0] == 2
            )
        finally:
            conn.close()

    async def test_legacy_rows_are_readable_and_updatable(self, tmp_path):
        """After migration, migrated rows accept metadata writes through the ORM."""
        db_path = tmp_path / "legacy-read.db"
        _make_legacy_db(db_path, rows=1)

        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True)
        try:
            await ensure_schema_columns(engine)
        finally:
            await engine.dispose()

        from sqlalchemy import select

        from app.db.base import get_sessionmaker, dispose_db
        from app.db.models import GenerationRecord

        # Point the global session factory at the migrated legacy file so the
        # ORM reads the *stored* row (not a freshly inserted one).
        from app.core.config import get_settings

        original_url = get_settings().database_url
        await dispose_db()
        get_settings().database_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
        try:
            async with get_sessionmaker()() as session:
                row = (await session.execute(select(GenerationRecord))).scalars().one()
                assert row.input_text == "旧记录 0"
                assert row.favorite is False
                assert row.tag_list == []

                row.favorite = True
                row.tags = ["工作"]
                await session.commit()
        finally:
            await dispose_db()
            get_settings().database_url = original_url

        conn = sqlite3.connect(db_path)
        try:
            stored = conn.execute(
                "SELECT favorite, tags FROM generation_records WHERE id = 1"
            ).fetchone()
            assert stored[0] == 1
            # SQLite gives a JSON column TEXT affinity, which strips the
            # backslashes from \uXXXX escapes: a Chinese tag lands here as raw
            # UTF-8. Compare through json.loads so either form is accepted.
            assert json.loads(stored[1]) == ["工作"]
        finally:
            conn.close()
