"""Error taxonomy for the whole application.

Every failure surfaced to a client uses the :class:`ErrorCode` enum and is
rendered through the single envelope defined in :class:`AppError`:

.. code-block:: json

    {"code": "TTS_AUTH_FAILED", "message": "...", "detail": {...}, "request_id": "..."}

Code ranges (stable, documented contract):

======  ===========================================================
Range   Meaning
======  ===========================================================
1xxx    Generic / request validation / routing
2xxx    LLM (chat completion) adapter failures
3xxx    TTS (speech synthesis) adapter failures
4xxx    Async task lifecycle failures
5xxx    Rate limiting, payload limits, storage and internal errors
======  ===========================================================
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import status


class ErrorCode(str, Enum):
    """Machine readable error codes returned by the API."""

    # -- 1xxx generic -------------------------------------------------------
    OK = "OK"
    BAD_REQUEST = "BAD_REQUEST"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    TEXT_TOO_LONG = "TEXT_TOO_LONG"
    TEXT_EMPTY = "TEXT_EMPTY"
    INVALID_PROVIDER = "INVALID_PROVIDER"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    MISSING_CREDENTIAL = "MISSING_CREDENTIAL"
    CORS_REJECTED = "CORS_REJECTED"

    # -- 2xxx LLM -----------------------------------------------------------
    LLM_ERROR = "LLM_ERROR"
    LLM_AUTH_FAILED = "LLM_AUTH_FAILED"
    LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_UPSTREAM_ERROR = "LLM_UPSTREAM_ERROR"
    LLM_EMPTY_RESPONSE = "LLM_EMPTY_RESPONSE"
    LLM_UNSUPPORTED_PROVIDER = "LLM_UNSUPPORTED_PROVIDER"

    # -- 3xxx TTS -----------------------------------------------------------
    TTS_ERROR = "TTS_ERROR"
    TTS_AUTH_FAILED = "TTS_AUTH_FAILED"
    TTS_RATE_LIMITED = "TTS_RATE_LIMITED"
    TTS_TIMEOUT = "TTS_TIMEOUT"
    TTS_UPSTREAM_ERROR = "TTS_UPSTREAM_ERROR"
    TTS_INVALID_VOICE = "TTS_INVALID_VOICE"
    TTS_UNSUPPORTED_FORMAT = "TTS_UNSUPPORTED_FORMAT"
    TTS_UNSUPPORTED_PROVIDER = "TTS_UNSUPPORTED_PROVIDER"
    TTS_TEXT_TOO_LONG = "TTS_TEXT_TOO_LONG"
    #: The chosen provider cannot consume a reference clip for cloning.
    TTS_REFERENCE_UNSUPPORTED = "TTS_REFERENCE_UNSUPPORTED"

    # -- 4xxx tasks ---------------------------------------------------------
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_NOT_FINISHED = "TASK_NOT_FINISHED"
    TASK_QUEUE_FULL = "TASK_QUEUE_FULL"
    TASK_CANCELED = "TASK_CANCELED"
    TASK_ALREADY_FINISHED = "TASK_ALREADY_FINISHED"

    # -- 6xxx media (upload / trim) -----------------------------------------
    MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
    MEDIA_UNSUPPORTED_FORMAT = "MEDIA_UNSUPPORTED_FORMAT"
    MEDIA_TOO_LARGE = "MEDIA_TOO_LARGE"
    MEDIA_TRIM_UNSUPPORTED = "MEDIA_TRIM_UNSUPPORTED"
    MEDIA_INVALID_RANGE = "MEDIA_INVALID_RANGE"
    FFMPEG_REQUIRED = "FFMPEG_REQUIRED"

    # -- 7xxx voice cloning --------------------------------------------------
    VOICE_CLONE_UNSUPPORTED = "VOICE_CLONE_UNSUPPORTED"
    VOICE_CLONE_FAILED = "VOICE_CLONE_FAILED"
    VOICE_NOT_FOUND = "VOICE_NOT_FOUND"

    # -- 5xxx platform ------------------------------------------------------
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    AUDIO_NOT_FOUND = "AUDIO_NOT_FOUND"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


#: Default HTTP status for each code. Routes may override when a code is
#: reused in a different context (e.g. TASK_NOT_FINISHED -> 409).
DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.OK: status.HTTP_200_OK,
    ErrorCode.BAD_REQUEST: status.HTTP_400_BAD_REQUEST,
    ErrorCode.VALIDATION_ERROR: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.METHOD_NOT_ALLOWED: status.HTTP_405_METHOD_NOT_ALLOWED,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    ErrorCode.PAYLOAD_TOO_LARGE: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    ErrorCode.TEXT_TOO_LONG: status.HTTP_400_BAD_REQUEST,
    ErrorCode.TEXT_EMPTY: status.HTTP_400_BAD_REQUEST,
    ErrorCode.INVALID_PROVIDER: status.HTTP_400_BAD_REQUEST,
    ErrorCode.INVALID_PARAMETER: status.HTTP_400_BAD_REQUEST,
    ErrorCode.MISSING_CREDENTIAL: status.HTTP_400_BAD_REQUEST,
    ErrorCode.CORS_REJECTED: status.HTTP_403_FORBIDDEN,
    ErrorCode.LLM_ERROR: status.HTTP_502_BAD_GATEWAY,
    ErrorCode.LLM_AUTH_FAILED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.LLM_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.LLM_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    ErrorCode.LLM_UPSTREAM_ERROR: status.HTTP_502_BAD_GATEWAY,
    ErrorCode.LLM_EMPTY_RESPONSE: status.HTTP_502_BAD_GATEWAY,
    ErrorCode.LLM_UNSUPPORTED_PROVIDER: status.HTTP_400_BAD_REQUEST,
    ErrorCode.TTS_ERROR: status.HTTP_502_BAD_GATEWAY,
    ErrorCode.TTS_AUTH_FAILED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.TTS_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.TTS_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    ErrorCode.TTS_UPSTREAM_ERROR: status.HTTP_502_BAD_GATEWAY,
    ErrorCode.TTS_INVALID_VOICE: status.HTTP_400_BAD_REQUEST,
    ErrorCode.TTS_UNSUPPORTED_FORMAT: status.HTTP_400_BAD_REQUEST,
    ErrorCode.TTS_UNSUPPORTED_PROVIDER: status.HTTP_400_BAD_REQUEST,
    ErrorCode.TTS_TEXT_TOO_LONG: status.HTTP_400_BAD_REQUEST,
    ErrorCode.TTS_REFERENCE_UNSUPPORTED: status.HTTP_400_BAD_REQUEST,
    ErrorCode.TASK_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.TASK_NOT_FINISHED: status.HTTP_409_CONFLICT,
    ErrorCode.TASK_QUEUE_FULL: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.TASK_CANCELED: status.HTTP_409_CONFLICT,
    ErrorCode.TASK_ALREADY_FINISHED: status.HTTP_409_CONFLICT,
    ErrorCode.MEDIA_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.MEDIA_UNSUPPORTED_FORMAT: status.HTTP_400_BAD_REQUEST,
    ErrorCode.MEDIA_TOO_LARGE: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    ErrorCode.MEDIA_TRIM_UNSUPPORTED: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ErrorCode.MEDIA_INVALID_RANGE: status.HTTP_400_BAD_REQUEST,
    ErrorCode.FFMPEG_REQUIRED: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ErrorCode.VOICE_CLONE_UNSUPPORTED: status.HTTP_400_BAD_REQUEST,
    ErrorCode.VOICE_CLONE_FAILED: status.HTTP_502_BAD_GATEWAY,
    ErrorCode.VOICE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.STORAGE_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.AUDIO_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.SERVICE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


class AppError(Exception):
    """Base exception rendered as the API error envelope.

    Parameters
    ----------
    code:
        Stable machine readable code.
    message:
        Human readable message (safe to display in the UI).
    detail:
        Optional structured context; never contains credentials.
    http_status:
        Overrides :data:`DEFAULT_STATUS` when provided.
    retryable:
        Hint for the retry/degradation layer.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.http_status = http_status or DEFAULT_STATUS.get(
            code, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        self.retryable = retryable

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "detail": self.detail,
        }
        if request_id:
            payload["request_id"] = request_id
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"AppError(code={self.code.value!r}, message={self.message!r})"


# ---------------------------------------------------------------------------
# Convenience factories - keep the taxonomy consistent across adapters.
# ---------------------------------------------------------------------------
def validation_error(message: str, **detail: Any) -> AppError:
    return AppError(ErrorCode.VALIDATION_ERROR, message, detail=detail)


def invalid_parameter(message: str, **detail: Any) -> AppError:
    return AppError(ErrorCode.INVALID_PARAMETER, message, detail=detail)


def missing_credential(kind: str, provider: str) -> AppError:
    return AppError(
        ErrorCode.MISSING_CREDENTIAL,
        f"{kind.upper()} 服务商 {provider} 缺少 API 密钥，请在界面中填写。",
        detail={"kind": kind, "provider": provider},
    )


def not_found(what: str, **detail: Any) -> AppError:
    return AppError(ErrorCode.NOT_FOUND, f"{what} 不存在。", detail=detail)


def internal_error(message: str = "服务器内部错误。", **detail: Any) -> AppError:
    return AppError(ErrorCode.INTERNAL_ERROR, message, detail=detail)