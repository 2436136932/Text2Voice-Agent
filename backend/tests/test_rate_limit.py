"""Rate limiter: bucket refill, identity isolation, eviction and the 429 path."""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import AppError, ErrorCode
from app.core.rate_limit import Bucket, InMemoryRateLimitStore, RateLimiter

pytestmark = pytest.mark.asyncio


class TestBucket:
    def test_starts_full(self):
        bucket = Bucket(capacity=3, refill_per_second=1.0)
        assert bucket.tokens == 3

    def test_consume_drains_then_refuses(self):
        bucket = Bucket(capacity=2, refill_per_second=0.0)
        allowed, remaining, _ = bucket.consume(1)
        assert allowed and remaining == 1
        allowed, remaining, retry = bucket.consume(1)
        assert allowed and remaining == 0
        # Empty + zero refill: refused with a sane retry hint.
        allowed, remaining, retry = bucket.consume(1)
        assert not allowed
        assert retry >= 1.0

    def test_refill_recovers_capacity(self):
        bucket = Bucket(capacity=1, refill_per_second=1000.0)
        assert bucket.consume(1)[0]
        # 1 ms at 1000/s refills ~1 token.
        import time

        time.sleep(0.002)
        allowed, _, _ = bucket.consume(1)
        assert allowed

    def test_burst_never_exceeds_capacity(self):
        bucket = Bucket(capacity=2, refill_per_second=1000.0)
        bucket.updated_at = bucket.updated_at - 3600  # pretend 1h passed without consuming
        bucket._refill(bucket.updated_at + 3600)
        assert bucket.tokens == 2  # capped at capacity, not accumulated


class TestStore:
    async def test_keys_are_isolated(self):
        store = InMemoryRateLimitStore()
        await store.consume("a:x", capacity=1, refill_per_second=0.0)
        # Exhausted for x, untouched for y.
        allowed_x, _, _ = await store.consume("a:x", capacity=1, refill_per_second=0.0)
        allowed_y, _, _ = await store.consume("a:y", capacity=1, refill_per_second=0.0)
        assert not allowed_x
        assert allowed_y

    async def test_reset_clears_one_or_all(self):
        store = InMemoryRateLimitStore()
        await store.consume("a:x", capacity=1, refill_per_second=0.0)
        await store.consume("a:y", capacity=1, refill_per_second=0.0)
        await store.reset("a:x")
        assert (await store.consume("a:x", capacity=1, refill_per_second=0.0))[0]
        # y still exhausted.
        assert not (await store.consume("a:y", capacity=1, refill_per_second=0.0))[0]
        await store.reset()
        assert (await store.consume("a:y", capacity=1, refill_per_second=0.0))[0]

    async def test_eviction_under_key_pressure(self):
        store = InMemoryRateLimitStore(max_keys=6)
        for i in range(10):
            await store.consume(f"b:key{i}", capacity=1, refill_per_second=0.0)
        # Must not raise: oldest third gets evicted to make room.
        assert (await store.consume("b:new", capacity=1, refill_per_second=0.0))[0]

    async def test_concurrent_consumers_never_oversubscribe(self):
        """The lock must make N simultaneous consumes respect capacity."""
        store = InMemoryRateLimitStore()
        results = await asyncio.gather(
            *[
                store.consume("shared", capacity=3, refill_per_second=0.0)
                for _ in range(6)
            ]
        )
        allowed = sum(1 for ok, _, _ in results if ok)
        assert allowed == 3


class TestRateLimiter:
    async def test_disabled_limiter_alows_everything(self, monkeypatch):
        monkeypatch.setattr("app.core.rate_limit.settings.rate_limit_enabled", False)
        limiter = RateLimiter()
        for _ in range(10):
            await limiter.check(bucket="b", identity="i", capacity=1, window_seconds=60)

    async def test_exhaustion_raises_rate_limited_429(self, monkeypatch):
        monkeypatch.setattr("app.core.rate_limit.settings.rate_limit_enabled", True)
        limiter = RateLimiter()
        for _ in range(2):
            await limiter.check(bucket="b", identity="i", capacity=2, window_seconds=60)
        with pytest.raises(AppError) as excinfo:
            await limiter.check(bucket="b", identity="i", capacity=2, window_seconds=60)
        assert excinfo.value.code is ErrorCode.RATE_LIMITED
        assert excinfo.value.http_status == 429
        assert excinfo.value.retryable is True

    async def test_nonpositive_config_bypasses(self):
        limiter = RateLimiter()
        assert await limiter.check(bucket="b", identity="i", capacity=0, window_seconds=60) == 0
        assert await limiter.check(bucket="b", identity="i", capacity=5, window_seconds=0) == 5
