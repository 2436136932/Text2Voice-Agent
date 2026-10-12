"""Retry policy and graceful-degradation helpers for upstream provider calls.

Policy
------
* Only :class:`AppError` instances flagged ``retryable`` are retried
  (timeouts, 429, 5xx). Authentication and validation failures fail fast -
  retrying a bad key only burns the rate-limit budget.
* Backoff is exponential with full jitter, capped by configuration.
* A :class:`RetryBudget` lets the caller stop retrying a provider for a while
  after repeated failures, so a dead upstream does not slow every request.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


async def call_with_retry(
    func: Callable[[int], Awaitable[T]],
    *,
    provider: str,
    kind: str,
    max_retries: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    on_retry: Callable[[int, AppError], None] | None = None,
) -> T:
    """Invoke ``func(attempt)`` with bounded retries.

    ``func`` receives the 1-based attempt number and must raise
    :class:`AppError` on failure.
    """
    retries = settings.upstream_max_retries if max_retries is None else max_retries
    base = settings.upstream_backoff_base_seconds if base_delay is None else base_delay
    cap = settings.upstream_backoff_max_seconds if max_delay is None else max_delay
    attempts = max(1, retries + 1)
    last_error: AppError | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await func(attempt)
        except AppError as exc:
            last_error = exc
            if not exc.retryable or attempt >= attempts:
                if attempt > 1:
                    logger.warning(
                        "%s/%s gave up after %s attempt(s): %s",
                        kind, provider, attempt, exc.code.value,
                    )
                raise
            delay = min(cap, base * (2 ** (attempt - 1)))
            delay = random.uniform(0, delay)  # full jitter
            logger.warning(
                "%s/%s attempt %s/%s failed (%s), retrying in %.2fs",
                kind, provider, attempt, attempts, exc.code.value, delay,
            )
            if on_retry is not None:
                on_retry(attempt, exc)
            await asyncio.sleep(delay)

    # Unreachable: the loop either returns or raises.
    raise last_error or AppError(ErrorCode.INTERNAL_ERROR, "重试逻辑异常。")  # pragma: no cover


@dataclass
class RetryBudget:
    """Circuit-breaker-lite: short-circuits a provider after N failures.

    Not a full circuit breaker (no half-open probing window beyond the cooldown)
    but enough to keep a hard-down provider from adding latency to every call.
    """

    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)

    def is_open(self, provider: str) -> bool:
        opened = self._opened_at.get(provider)
        if opened is None:
            return False
        if time.monotonic() - opened >= self.cooldown_seconds:
            # Cooldown elapsed: allow a probe and reset the counter.
            self._opened_at.pop(provider, None)
            self._failures.pop(provider, None)
            return False
        return True

    def record_failure(self, provider: str) -> None:
        count = self._failures.get(provider, 0) + 1
        self._failures[provider] = count
        if count >= self.failure_threshold:
            self._opened_at[provider] = time.monotonic()
            logger.error(
                "provider %s short-circuited for %.0fs after %s failures",
                provider, self.cooldown_seconds, count,
            )

    def record_success(self, provider: str) -> None:
        self._failures.pop(provider, None)
        self._opened_at.pop(provider, None)

    def retry_after(self, provider: str) -> float:
        opened = self._opened_at.get(provider)
        if opened is None:
            return 0.0
        return max(0.0, self.cooldown_seconds - (time.monotonic() - opened))


#: One budget per provider *kind* to avoid cross-talk between LLM and TTS.
llm_budget = RetryBudget()
tts_budget = RetryBudget()