"""HTTP middleware: request ids, redacted access logs, body size limits."""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, set_request_context
from app.core.security import sanitize_request_id

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
#: Paths that are polled frequently and would drown the log.
_QUIET_PATHS = frozenset({"/health", "/api/v1/health", "/metrics"})


def client_identity(request: Request) -> str:
    """Best-effort client identity for rate limiting.

    Honours ``X-Forwarded-For`` (behind a reverse proxy) and falls back to the
    socket peer. The *first* hop is used, which is what nginx sets.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, log a redacted access line, and enforce size."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        identity = client_identity(request)
        set_request_context(request_id, identity)
        request.state.request_id = request_id
        request.state.client_identity = identity

        # Cheap body-size guard before the body is parsed into memory.
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > settings.max_request_bytes:
                error = AppError(
                    ErrorCode.PAYLOAD_TOO_LARGE,
                    f"请求体过大（上限 {settings.max_request_bytes // 1024} KB）。",
                    detail={"max_bytes": settings.max_request_bytes},
                    http_status=413,
                )
                return self._error_response(error, request_id, request)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except AppError as exc:  # raised by middleware-level dependencies
            return self._error_response(exc, request_id, request)
        duration_ms = int((time.perf_counter() - started) * 1000)

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["X-Response-Time"] = f"{duration_ms}ms"

        if request.url.path not in _QUIET_PATHS:
            level = logger.info if response.status_code < 400 else logger.warning
            level(
                "%s %s -> %s (%sms)",
                request.method,
                # Query strings may carry credentials - log the path only.
                request.url.path,
                response.status_code,
                duration_ms,
            )
        return response

    @staticmethod
    def _error_response(error: AppError, request_id: str, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=error.http_status,
            content=error.to_payload(request_id),
            headers={REQUEST_ID_HEADER: request_id},
        )