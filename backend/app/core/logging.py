"""Structured logging with mandatory credential redaction.

Two guarantees are implemented here:

1. **No secret ever reaches a log sink.** :func:`redact_text` masks API keys,
   bearer tokens, signed URLs and long random-looking strings before the
   record is formatted. It is applied to *every* log message, every extra
   field and every exception traceback, so a careless ``logger.info(body)``
   cannot leak a key.
2. **Every log line carries a request id**, so a user-visible ``request_id``
   can be correlated with server side diagnostics.

The module is dependency free (stdlib ``logging``) to stay importable from
tests and workers alike.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from typing import Any, Iterable, Mapping

from app.core.config import settings

# ---------------------------------------------------------------------------
# Request scoped context
# ---------------------------------------------------------------------------
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
client_ip_var: ContextVar[str] = ContextVar("client_ip", default="-")


def get_request_id() -> str:
    return request_id_var.get()


def set_request_context(request_id: str, client_ip: str = "-") -> None:
    request_id_var.set(request_id)
    client_ip_var.set(client_ip)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
REDACTED = "***REDACTED***"

#: Keys whose *values* are always replaced, matched case-insensitively and
#: ignoring separators (``apiKey``, ``api_key``, ``API-KEY`` all match).
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|[_\-.])?"
    r"(?:api[_\-. ]?key|apikey|access[_\-. ]?token|auth[_\-. ]?token|"
    r"authorization|bearer|secret|password|passwd|credential|client[_\-. ]?secret|"
    r"subscription[_\-. ]?key|app[_\-. ]?key|token|session[_\-. ]?id|x-api-key)"
    r"(?:$|[_\-.])?",
    re.IGNORECASE,
)

#: Value-level patterns. Order matters: the most specific first.
VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI / Anthropic / DashScope / DeepSeek style keys. ``sk-ant-`` and
    # ``sk-proj-`` are intentionally covered by this single rule so every
    # vendor gets the same readable placeholder.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), "sk-***"),
    # Google
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"), "AIza***"),
    # Hugging Face
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"), "hf_***"),
    # Explicit ``Bearer <token>`` / ``Basic <token>`` schemes. This must not
    # require a ``:`` or ``=`` separator, which is the common header form.
    (
        re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-+/=]{6,})"),
        r"\1 " + REDACTED,
    ),
    # ElevenLabs / Azure Speech subscription keys are 32 hex characters.
    (re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE), "***REDACTED-HEX32***"),
    # ``key=value`` / ``"key": "value"`` forms. ``&`` and ``=`` are excluded
    # from the value class so a query string's *other* parameters survive.
    (
        re.compile(
            r"(?i)\b(bearer|basic|token|api[_-]?key|access[_-]?token|key)\b\s*[:=]\s*"
            r"[\"']?([^\s\"',;}&=]{6,})[\"']?"
        ),
        r"\1=" + REDACTED,
    ),
    # Query-string credentials.
    (
        re.compile(
            r"(?i)([?&](?:api[_-]?key|key|token|access_token|sig|signature)=)"
            r"([^&\s\"']{4,})"
        ),
        r"\1" + REDACTED,
    ),
    # Opaque UUID-shaped tokens (Volcengine request ids, session ids…).
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "***REDACTED-UUID***",
    ),
)

#: Long base64 / opaque blobs are truncated rather than fully printed.
#: ``=`` is excluded from the body so a ``key=value`` separator is not
#: swallowed into the match (padding is still allowed at the end).
LONG_BLOB_PATTERN = re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")

#: Personal data kept out of logs.
EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _extra_patterns() -> Iterable[re.Pattern[str]]:
    for key in settings.redact_extra_keys:
        if key:
            yield re.compile(re.escape(key), re.IGNORECASE)


#: A token that may safely be logged: no credential alphabet (``-``/``_``)
#: and no long unbroken run of letters/digits, so it can never resemble an API
#: key or a base64 blob. Applied to ``request_id`` / ``client_ip``.
_SAFE_LOG_TOKEN = re.compile(r"[^A-Za-z0-9.:]{1,128}\Z")


def _scrub_token(value: object, fallback: str = "-") -> str:
    """Keep correlation ids readable while refusing anything key-shaped."""
    text = str(value or "").strip()
    if not text or not _SAFE_LOG_TOKEN.match(text):
        return fallback
    if any(len(run) >= 16 for run in re.findall(r"[A-Za-z0-9]+", text)):
        return fallback
    return text


def redact_text(value: str, *, limit: int | None = None) -> str:
    """Mask every credential-like substring inside ``value``.

    The function is intentionally aggressive: false positives only cost log
    readability, a false negative leaks a customer key.
    """
    if not value:
        return value
    result = value
    for pattern, replacement in VALUE_PATTERNS:
        result = pattern.sub(replacement, result)
    for pattern in _extra_patterns():
        result = pattern.sub(REDACTED, result)
    result = EMAIL_PATTERN.sub("***@***", result)
    result = redact_blob(result)
    if limit is not None and len(result) > limit:
        result = f"{result[:limit]}…[+{len(result) - limit} chars]"
    return result


def redact_blob(value: str) -> str:
    """Truncate long opaque runs (base64 audio, HTML error pages…)."""
    return LONG_BLOB_PATTERN.sub(
        lambda match: f"{match.group(0)[:12]}…[{len(match.group(0))} chars]", value
    )


def is_sensitive_key(key: str) -> bool:
    """True when a mapping key implies a secret value.

    Handles both flat (``api_key``) and dotted (``llm.api_key``) paths.
    """
    normalized = key.strip()
    if SENSITIVE_KEY_PATTERN.search(normalized):
        return True
    # ``llm.api_key`` style dotted paths: recurse only on genuine segments.
    if "." in normalized:
        return any(is_sensitive_key(part) for part in normalized.split(".") if part)
    return False


def redact_mapping(data: Mapping[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Recursively redact a mapping for logging."""
    if depth > 6:
        return {"_truncated": "max depth"}
    out: dict[str, Any] = {}
    for key, value in data.items():
        key_str = str(key)
        if is_sensitive_key(key_str):
            out[key_str] = REDACTED if value else value
        elif isinstance(value, Mapping):
            out[key_str] = redact_mapping(value, depth=depth + 1)
        elif isinstance(value, (list, tuple)):
            out[key_str] = [
                redact_mapping(item, depth=depth + 1)
                if isinstance(item, Mapping)
                else (redact_text(item) if isinstance(item, str) else item)
                for item in value[:50]
            ]
        elif isinstance(value, str):
            out[key_str] = redact_blob(redact_text(value, limit=500))
        else:
            out[key_str] = value
    return out


def mask_key(key: str | None) -> str:
    """Return a display-safe fingerprint of a key: ``sk-1234…cdef``."""
    if not key:
        return "(empty)"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]} (len={len(key)})"


# ---------------------------------------------------------------------------
# Filters & formatters
# ---------------------------------------------------------------------------
class RedactionFilter(logging.Filter):
    """Redact the message, args and every ``extra`` field of a record."""

    _SAFE_ATTRS = frozenset(
        {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "taskName", "message", "asctime",
            # Correlation fields. ``request_id`` is validated against
            # ``^[A-Za-z0-9_\-.:]{1,128}$`` in ``sanitize_request_id`` before it
            # is ever stored, so it cannot smuggle a credential - and it must
            # survive redaction or logs become uncorrelatable (a bare uuid4 hex
            # would otherwise match the 32-hex rule).
            "request_id", "client_ip",
        }
    )

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, Mapping):
                record.args = redact_mapping(record.args)  # type: ignore[assignment]
            elif isinstance(record.args, tuple):
                record.args = tuple(  # type: ignore[assignment]
                    redact_text(a) if isinstance(a, str) else a for a in record.args
                )
        if record.exc_text:
            record.exc_text = redact_blob(redact_text(record.exc_text))
        if record.exc_info:
            # Pre-render the traceback so the formatter cannot emit the raw one.
            record.exc_text = redact_blob(redact_text(self._format_exc(record)))
            record.exc_info = None
        for key, value in list(record.__dict__.items()):
            if key in self._SAFE_ATTRS or key.startswith("_"):
                continue
            if is_sensitive_key(key):
                record.__dict__[key] = REDACTED
            elif isinstance(value, Mapping):
                record.__dict__[key] = redact_mapping(value)
            elif isinstance(value, str):
                record.__dict__[key] = redact_blob(redact_text(value, limit=1000))
        return True

    @staticmethod
    def _format_exc(record: logging.LogRecord) -> str:
        """Render a traceback without relying on ``Formatter.formatException``."""
        import traceback

        if record.exc_info is None:
            return record.exc_text or ""
        return "".join(traceback.format_exception(*record.exc_info))


class RequestContextFilter(logging.Filter):
    """Attach ``request_id`` / ``client_ip`` to every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not getattr(record, "request_id", None):
            record.request_id = _scrub_token(request_id_var.get())
        if not getattr(record, "client_ip", None):
            record.client_ip = _scrub_token(client_ip_var.get())
        return True


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


class JsonFormatter(logging.Formatter):
    """One JSON object per line - friendly for log collectors."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "client_ip": getattr(record, "client_ip", "-"),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in RedactionFilter._SAFE_ATTRS or key.startswith("_") or key in payload:
                continue
            if key in {"request_id", "client_ip"}:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


_CONFIGURED = False


def configure_logging(level: str | None = None) -> None:
    """Install the root logger configuration (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestContextFilter())
    handler.addFilter(RedactionFilter())
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else ConsoleFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel((level or settings.log_level).upper())

    # Uvicorn's access log duplicates our middleware log and prints raw query
    # strings (which may embed credentials) - disable it and rely on ours.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that is guaranteed to be redaction-safe."""
    configure_logging()
    return logging.getLogger(name)


def log_upstream_call(
    logger: logging.Logger,
    *,
    provider: str,
    endpoint: str,
    payload: Mapping[str, Any] | None = None,
    attempt: int = 1,
) -> None:
    """Log an outbound provider call with the payload fully redacted."""
    logger.debug(
        "upstream call provider=%s endpoint=%s attempt=%s payload=%s",
        provider,
        endpoint,
        attempt,
        json.dumps(redact_mapping(payload or {}), ensure_ascii=False),
    )