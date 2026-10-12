"""Exception handlers: one error envelope for every failure mode.

Registered handlers cover:

* :class:`AppError` - the application taxonomy;
* ``RequestValidationError`` - pydantic/FastAPI 422s, reshaped so the client
  sees the same ``{code, message, detail, request_id}`` envelope;
* ``HTTPException`` - 404/405 raised by the framework;
* ``Exception`` - the last resort; logs a traceback and returns
  ``INTERNAL_ERROR`` without leaking internals.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, request_id_var

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or request_id_var.get()


def _envelope(
    code: str,
    message: str,
    detail: dict[str, Any] | None,
    request_id: str,
    status_code: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "detail": detail or {},
            "request_id": request_id,
        },
        headers={REQUEST_ID_HEADER: request_id},
    )


_STATUS_TO_CODE = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.MISSING_CREDENTIAL,
    403: ErrorCode.CORS_REJECTED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def _humanize_validation_error(error: dict[str, Any]) -> str:
    """Turn a pydantic error into a readable Chinese message."""
    location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
    error_type = error.get("type", "")
    ctx = error.get("ctx") or {}

    if error_type == "missing":
        return f"缺少必填字段：{location}"
    if error_type == "string_too_long":
        return f"字段 {location} 长度超限（最多 {ctx.get('max_length')} 个字符）"
    if error_type == "string_too_short":
        return f"字段 {location} 长度不足（至少 {ctx.get('min_length')} 个字符）"
    if error_type in {"greater_than_equal", "less_than_equal"}:
        return f"字段 {location} 超出允许范围（{error.get('msg', '')}）"
    if error_type == "extra_forbidden":
        return f"不支持的字段：{location}"
    message = error.get("msg", "参数不合法")
    return f"{location}: {message}" if location else message


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler to the FastAPI app."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = _request_id(request)
        # 5xx are our fault -> log loudly; 4xx are client input -> log quietly.
        if exc.http_status >= 500:
            logger.error(
                "AppError %s: %s detail=%s", exc.code.value, exc.message, exc.detail
            )
        else:
            logger.info(
                "AppError %s: %s detail=%s", exc.code.value, exc.message, exc.detail
            )
        return _envelope(
            exc.code.value, exc.message, exc.detail, request_id, exc.http_status
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = _request_id(request)
        errors = exc.errors()
        messages = [_humanize_validation_error(item) for item in errors]
        detail = {
            "errors": [
                {
                    "field": ".".join(str(part) for part in item.get("loc", ())),
                    "type": item.get("type", ""),
                    "message": _humanize_validation_error(item),
                }
                for item in errors
            ]
        }
        logger.info("validation error path=%s errors=%s", request.url.path, messages)
        return _envelope(
            ErrorCode.VALIDATION_ERROR.value,
            "；".join(messages[:3]) or "请求参数校验失败。",
            detail,
            request_id,
            422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = _request_id(request)
        code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.BAD_REQUEST)
        message = str(exc.detail) if exc.detail else f"HTTP {exc.status_code}"
        if exc.status_code == 404:
            message = f"接口不存在：{request.method} {request.url.path}"
        return _envelope(code.value, message, {"path": request.url.path}, request_id, exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        # ``logger.exception`` includes the traceback, which the RedactionFilter
        # scrubs before it reaches the handler.
        logger.exception(
            "unhandled exception path=%s type=%s", request.url.path, type(exc).__name__
        )
        return _envelope(
            ErrorCode.INTERNAL_ERROR.value,
            "服务器内部错误，请稍后重试或联系管理员。",
            {"type": type(exc).__name__, "path": request.url.path},
            request_id,
            500,
        )