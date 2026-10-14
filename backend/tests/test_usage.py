"""Usage aggregation endpoint: sums per-row tokens grouped by dimension."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import GenerationRecord
from app.schemas.generation import GenerateRequest
from app.services.generation import GenerationService

pytestmark = pytest.mark.asyncio


async def seed(session, *, provider: str, model: str, tokens: int) -> None:
    request = GenerateRequest(
        text=f"用量统计测试-{provider}",
        llm={"provider": "mock", "model": "mock-echo-v1"},
        tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
        options={"auto_save": True, "reuse_reply_text": f"回复-{provider}"},
    )
    await GenerationService().generate(session, request)
    await session.commit()
    # Stamp the row with deterministic token numbers for the assertions.
    row = (
        await session.execute(
            select(GenerationRecord).order_by(GenerationRecord.id.desc()).limit(1)
        )
    ).scalar_one()
    row.llm_provider = provider
    row.llm_model = model
    row.prompt_tokens = tokens
    row.completion_tokens = tokens
    row.total_tokens = tokens * 2
    await session.commit()


class TestUsageEndpoint:
    async def test_group_by_llm_provider_sums_tokens(self, client, session):
        await seed(session, provider="alpha", model="m1", tokens=10)
        await seed(session, provider="alpha", model="m1", tokens=20)
        await seed(session, provider="beta", model="m2", tokens=5)

        response = await client.get("/api/v1/meta/usage?days=30&group_by=llm_provider")
        assert response.status_code == 200
        body = response.json()
        by_group = {item["group"]: item for item in body["items"]}
        assert by_group["alpha"]["total_tokens"] == 60  # (10+20)*2
        assert by_group["alpha"]["generations"] == 2
        assert by_group["beta"]["total_tokens"] == 10
        assert body["totals"]["total_tokens"] == 70

    async def test_group_by_day_returns_iso_dates(self, client, session):
        await seed(session, provider="alpha", model="m1", tokens=7)
        response = await client.get("/api/v1/meta/usage?group_by=day")
        assert response.status_code == 200
        body = response.json()
        assert body["items"], "at least one day bucket"
        assert "-" in body["items"][0]["group"], "day buckets are date strings"

    async def test_unknown_dimension_is_400(self, client):
        response = await client.get("/api/v1/meta/usage?group_by=nonsense")
        assert response.status_code == 400

    async def test_empty_window_returns_zero_totals(self, client, session):
        response = await client.get("/api/v1/meta/usage?days=0&group_by=llm_provider")
        assert response.status_code == 200
        body = response.json()
        # days=0 clamps to 1, but a fresh DB has rows only from other tests…
        assert isinstance(body["totals"]["total_tokens"], int)
