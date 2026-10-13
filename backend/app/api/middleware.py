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
        #
        # Uploads get their own, larger budget: the media endpoints advertise
        # `max_upload_mb` (50 MB by default), so applying the 2 MB JSON limit to
        # them made every video upload fail with 413 while the UI promised 50 MB.
        limit = self._body_limit_for(request.url.path)
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > limit:
            # Drain the body before responding. Returning immediately leaves the
            # client still sending megabytes into a closed socket, which surfaces
            # in the browser as ERR_CONNECTION_RESET instead of a usable 413.
            await self._drain(request)
            error = AppError(
                ErrorCode.PAYLOAD_TOO_LARGE,
                f"请求体过大（上限 {self._format_limit(limit)}）。",
                detail={
                    "max_bytes": limit,
                    "suggestion": (
                        f"请上传小于 {self._format_limit(limit)} 的文件，"
                        "或先用「参考音频」裁剪出较短的片段。"
                    ),
                },
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

    #: Paths that accept large binary uploads and therefore get their own limit.
    _UPLOAD_PATHS = ("/media/inspect",)

    @classmethod
    def _body_limit_for(cls, path: str) -> int:
        """Return the request-body budget for ``path``.

        Upload routes use ``max_upload_mb``; everything else uses the much
        smaller ``max_request_bytes``, which is sized for JSON payloads.
        """
        if any(path.endswith(suffix) for suffix in cls._UPLOAD_PATHS):
            return settings.max_upload_mb * 1024 * 1024
        return settings.max_request_bytes

    @staticmethod
    def _format_limit(limit: int) -> str:
        if limit >= 1024 * 1024:
            return f"{limit // (1024 * 1024)} MB"
        return f"{limit // 1024} KB"

    @staticmethod
    async def _drain(request: Request) -> None:
        """Consume any remaining request body so the socket closes cleanly."""
        try:
            async for _chunk in request.stream():
                pass
        except Exception:  # noqa: BLE001 - draining is best effort
            logger.debug("failed to drain oversized request body")

    @staticmethod
    def _error_response(error: AppError, request_id: str, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=error.http_status,
            content=error.to_payload(request_id),
            headers={REQUEST_ID_HEADER: request_id},
        )