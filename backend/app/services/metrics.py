"""In-process metrics counters.

Deliberately dependency free (no Prometheus client): the goal is a cheap
``GET /meta/metrics`` endpoint and useful log lines, not a scraping stack.
Swap in ``prometheus_client`` here if you need histograms.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any


class MetricsCollector:
    """Thread-safe counters for the process lifetime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._counters: Counter[str] = Counter()
        self._provider_calls: Counter[str] = Counter()

    # -- recording ----------------------------------------------------------
    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def record_provider_call(self, kind: str, provider: str, *, ok: bool) -> None:
        key = f"{kind}:{provider}:{'ok' if ok else 'error'}"
        with self._lock:
            self._provider_calls[key] += 1

    # -- reading ------------------------------------------------------------
    @property
    def uptime_seconds(self) -> float:
        return round(time.time() - self._started_at, 3)

    def get(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            providers = dict(self._provider_calls)
        return {
            "uptime_seconds": self.uptime_seconds,
            "requests_total": counters.get("requests_total", 0),
            "generations_total": counters.get("generations_total", 0),
            "generations_succeeded": counters.get("generations_succeeded", 0),
            "generations_failed": counters.get("generations_failed", 0),
            "generations_degraded": counters.get("generations_degraded", 0),
            "active_tasks": counters.get("tasks_active", 0),
            "provider_calls": providers,
        }

    def reset(self) -> None:
        """Used by tests to keep assertions deterministic."""
        with self._lock:
            self._counters.clear()
            self._provider_calls.clear()


metrics = MetricsCollector()