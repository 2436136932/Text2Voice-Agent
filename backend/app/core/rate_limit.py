"""In-process token-bucket rate limiting.

A single-process deployment (the default) does not need Redis, so the limiter
is a thread-safe/async-safe in-memory token bucket keyed by
``(bucket_name, client_identity)``.

For a multi-worker deployment the class exposes a pluggable ``store`` so a
Redis implementation can be dropped in without touching the routes. The
interface is deliberately tiny: ``check()`` returns the remaining budget and
raises :class:`AppError` with code ``RATE_LIMITED`` when exhausted.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Bucket:
    """A classic token bucket with continuous refill."""

    capacity: float
    refill_per_second: float
    tokens: float = field(default=0.0)
    updated_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        if self.tokens == 0.0 and self.updated_at == 0.0:
            self.tokens = self.capacity
            self.updated_at = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated_at)
        if elapsed:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
            self.updated_at = now

    def consume(self, amount: float = 1.0) -> tuple[bool, float, float]:
        """Try to consume ``amount`` tokens.

        Returns ``(allowed, remaining, retry_after_seconds)``.
        """
        now = time.monotonic()
        self._refill(now)
        if self.tokens >= amount:
            self.tokens -= amount
            return True, self.tokens, 0.0
        missing = amount - self.tokens
        retry_after = missing / self.refill_per_second if self.refill_per_second > 0 else 60.0
        return False, self.tokens, max(1.0, retry_after)


class RateLimitStore(Protocol):
    """Storage backend contract (memory today, Redis tomorrow)."""

    async def consume(
        self, key: str, *, capacity: float, refill_per_second: float, amount: float = 1.0
    ) -> tuple[bool, float, float]: ...

    async def reset(self, key: str | None = None) -> None: ...


class InMemoryRateLimitStore:
    """Default store: a dict of buckets guarded by an ``asyncio.Lock``."""

    def __init__(self, *, max_keys: int = 10_000) -> None:
        self._buckets: dict[str, Bucket] = {}
        self._lock = asyncio.Lock()
        self._max_keys = max_keys

    async def consume(
        self, key: str, *, capacity: float, refill_per_second: float, amount: float = 1.0
    ) -> tuple[bool, float, float]:
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    self._evict_locked()
                bucket = Bucket(capacity=capacity, refill_per_second=refill_per_second)
                self._buckets[key] = bucket
            else:
                # Configuration may change at runtime (tests, hot reload).
                bucket.capacity = capacity
                bucket.refill_per_second = refill_per_second
            return bucket.consume(amount)

    def _evict_locked(self) -> None:
        """Drop the least recently touched third of the buckets."""
        now = time.monotonic()
        stale = sorted(self._buckets.items(), key=lambda item: item[1].updated_at)
        for key, _ in stale[: max(1, len(stale) // 3)]:
            self._buckets.pop(key, None)
        logger.debug("rate-limit store evicted stale buckets at %s", now)

    async def reset(self, key: str | None = None) -> None:
        async with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


class RateLimiter:
    """Facade used by routes and middleware."""

    def __init__(self, store: RateLimitStore | None = None) -> None:
        self.store: RateLimitStore = store or InMemoryRateLimitStore()

    async def check(
        self,
        *,
        bucket: str,
        identity: str,
        capacity: int,
        window_seconds: int,
        amount: float = 1.0,
        enabled: bool | None = None,
    ) -> float:
        """Consume one unit of budget, raising ``RATE_LIMITED`` when empty.

        Returns the number of remaining tokens (for ``X-RateLimit-Remaining``).
        """
        if enabled is None:
            enabled = settings.rate_limit_enabled
        if not enabled or capacity <= 0 or window_seconds <= 0:
            return float(capacity)

        key = f"{bucket}:{identity}"
        refill = capacity / float(window_seconds)
        allowed, remaining, retry_after = await self.store.consume(
            key, capacity=float(capacity), refill_per_second=refill, amount=amount
        )
        if not allowed:
            logger.warning(
                "rate limit hit bucket=%s identity=%s retry_after=%.1fs", bucket, identity, retry_after
            )
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"请求过于频繁，请在 {retry_after:.0f} 秒后重试。",
                detail={
                    "bucket": bucket,
                    "limit": capacity,
                    "window_seconds": window_seconds,
                    "retry_after": round(retry_after, 2),
                },
                http_status=429,
                retryable=True,
            )
        return remaining


#: Process-wide limiter. Tests may replace ``limiter.store``.
limiter = RateLimiter()