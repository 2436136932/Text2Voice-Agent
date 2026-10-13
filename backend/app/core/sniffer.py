"""Outbound request/response sniffer for provider debugging.

Diagnosing a third-party gateway from the outside is guesswork when its
documentation is unreachable. This module adds an opt-in *debug capture* mode:
when enabled, every outbound provider call records the method, URL, redacted
headers, redacted request body and a truncated response body into a bounded
in-memory ring buffer that ``GET /api/v1/meta/last-calls`` exposes.

Security
--------
The capture reuses the same redaction pipeline as the log system
(:func:`app.core.logging.redact_mapping`), so credentials never appear even in
debug output. It is **off by default** and must be explicitly enabled.

This is deliberately a diagnostic aid rather than a permanent feature: it is
meant to answer "what exactly did we send, and what came back?" in one round
trip instead of adding print statements.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import redact_mapping, redact_text

#: Records kept before the oldest is dropped.
MAX_RECORDS = 30
#: Response bodies are truncated to this many characters.
MAX_BODY_CHARS = 1500

_enabled = False
_records: deque["CallRecord"] = deque(maxlen=MAX_RECORDS)


@dataclass(slots=True)
class CallRecord:
    """One captured outbound call."""

    seq: int
    timestamp: float
    provider: str
    kind: str
    method: str
    url: str
    request_body: Any = None
    status: int | None = None
    response_body: str = ""
    error: str | None = None
    elapsed_ms: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "provider": self.provider,
            "kind": self.kind,
            "method": self.method,
            "url": self.url,
            "request_body": self.request_body,
            "headers": self.headers,
            "status": self.status,
            "response_body": self.response_body,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


def is_enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    """Turn capture on/off. Clearing on enable keeps reports focused."""
    global _enabled
    _enabled = bool(value)
    if _enabled:
        _records.clear()


def clear() -> None:
    _records.clear()


def record(
    *,
    provider: str,
    kind: str,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    request_body: Any = None,
) -> CallRecord | None:
    """Start recording a call; returns the record to complete afterwards."""
    if not _enabled:
        return None
    item = CallRecord(
        seq=len(_records) + 1,
        timestamp=time.time(),
        provider=provider,
        kind=kind,
        method=method,
        url=url,
        request_body=_safe_body(request_body),
        headers=_safe_headers(headers or {}),
    )
    _records.append(item)
    return item


def complete(
    item: CallRecord | None,
    *,
    status: int | None = None,
    response_body: Any = None,
    error: str | None = None,
    elapsed_ms: int = 0,
) -> None:
    """Finish a previously started record."""
    if item is None:
        return
    item.status = status
    item.error = error
    item.elapsed_ms = elapsed_ms
    if response_body is not None:
        item.response_body = _safe_text(response_body)


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Mask credential headers, keep everything else for inspection."""
    sensitive = {"authorization", "x-api-key", "api-key", "ocp-apim-subscription-key", "xi-api-key"}
    return {
        key: ("***" if key.lower() in sensitive else value)
        for key, value in headers.items()
    }


def _safe_body(body: Any) -> Any:
    """Redact a request body for display, and clip binary payloads."""
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        return f"<binary {len(body)} bytes>"
    if isinstance(body, str):
        return redact_text(body, limit=MAX_BODY_CHARS)
    if isinstance(body, dict):
        # ``input``/``text`` can be long; keep them but clipped.
        cleaned = redact_mapping(body)
        for key in ("input", "text", "prompt"):
            value = cleaned.get(key)
            if isinstance(value, str) and len(value) > 300:
                cleaned[key] = f"{value[:300]}…(+{len(value) - 300})"
        # Encoded audio blobs would flood the report.
        for key in ("reference_audio", "prompt_audio", "audio"):
            value = cleaned.get(key)
            if isinstance(value, str) and len(value) > 120:
                cleaned[key] = f"<base64 {len(value)} chars>"
        return cleaned
    return redact_text(str(body), limit=MAX_BODY_CHARS)


def _safe_text(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    return redact_text(text, limit=MAX_BODY_CHARS)


def snapshot(*, limit: int = MAX_RECORDS) -> list[dict[str, Any]]:
    """Most recent records, newest last."""
    items = list(_records)[-limit:]
    return [item.to_dict() for item in items]