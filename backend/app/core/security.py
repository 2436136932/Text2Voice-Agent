"""Security helpers: credential hygiene, request ids, safe filenames.

Design rule enforced by this module and by the adapters: **an API key lives
only for the duration of one request**. It is read from the request body,
handed to the adapter, and never written to the database, the filesystem or a
log sink. The only thing that may be persisted is a non-reversible fingerprint
so the UI can show "the key ending in …cdef was used".
"""

from __future__ import annotations

import hashlib
import re
import uuid

from app.core.errors import ErrorCode, AppError
from app.core.logging import mask_key  # re-exported for convenience

__all__ = [
    "new_request_id",
    "key_fingerprint",
    "mask_key",
    "sanitize_filename",
    "assert_credential",
    "scrub_headers",
]

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-.:]{1,128}$")


def new_request_id() -> str:
    """Generate a collision-free request id."""
    return uuid.uuid4().hex


def sanitize_request_id(candidate: str | None) -> str:
    """Accept a client supplied ``X-Request-ID`` when it is well formed."""
    if candidate and _REQUEST_ID_RE.match(candidate):
        return candidate
    return new_request_id()


def key_fingerprint(api_key: str) -> str:
    """Non-reversible short fingerprint used for audit lines.

    ``sha256`` is truncated; the full key can never be recovered from it.
    """
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def assert_credential(api_key: str | None, *, kind: str, provider: str) -> str:
    """Return a usable credential or raise :class:`AppError`."""
    if not api_key or not api_key.strip():
        raise AppError(
            ErrorCode.MISSING_CREDENTIAL,
            f"缺少 {kind.upper()} 服务商 “{provider}” 的 API 密钥，请在页面中填写后重试。",
            detail={"kind": kind, "provider": provider},
        )
    key = api_key.strip()
    if len(key) < 4:
        raise AppError(
            ErrorCode.MISSING_CREDENTIAL,
            f"{kind.upper()} 服务商 “{provider}” 的 API 密钥看起来不完整。",
            detail={"kind": kind, "provider": provider, "length": len(key)},
        )
    return key


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str, *, fallback: str = "audio", max_length: int = 80) -> str:
    """Strip path traversal and unsafe characters from a filename.

    ``..`` is collapsed because it is meaningless in a generated name and is
    the one sequence that could still be interpreted as traversal.
    """
    candidate = _UNSAFE_FILENAME.sub("_", (name or "").strip())
    while ".." in candidate:
        candidate = candidate.replace("..", "_")
    candidate = candidate.strip("._-") or fallback
    return candidate[:max_length]


#: Header names that must never be echoed back or logged verbatim.
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "ocp-apim-subscription-key",
        "proxy-authorization",
        "cookie",
        "set-cookie",
    }
)


def scrub_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with credential values masked."""
    return {
        key: ("***" if key.lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }