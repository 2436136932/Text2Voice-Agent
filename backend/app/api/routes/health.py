"""Health and observability routes."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

from app.adapters.registry import registry
from app.api.deps import SessionDep
from app.core.config import settings
from app.core.errors import DEFAULT_STATUS, ErrorCode
from app.core.logging import get_logger
from app.schemas.generation import (
    ErrorCodeCatalogResponse,
    ErrorCodeItem,
    HealthResponse,
    MetricsResponse,
)
from app.services.metrics import metrics
from app.services.tasks import task_manager

logger = get_logger(__name__)

router = APIRouter(tags=["系统 System"])

_STARTED_AT = time.time()

#: Human readable description per error code, surfaced by /meta/error-codes.
_ERROR_DESCRIPTIONS: dict[ErrorCode, str] = {
    ErrorCode.BAD_REQUEST: "请求格式错误",
    ErrorCode.VALIDATION_ERROR: "请求参数校验失败",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.METHOD_NOT_ALLOWED: "HTTP 方法不被允许",
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: "不支持的 Content-Type",
    ErrorCode.PAYLOAD_TOO_LARGE: "请求体超过大小限制",
    ErrorCode.TEXT_TOO_LONG: "输入文本超过长度限制",
    ErrorCode.TEXT_EMPTY: "输入文本为空",
    ErrorCode.INVALID_PROVIDER: "服务商标识不合法",
    ErrorCode.INVALID_PARAMETER: "参数不合法",
    ErrorCode.MISSING_CREDENTIAL: "缺少必要的 API 密钥",
    ErrorCode.CORS_REJECTED: "跨域请求被拒绝",
    ErrorCode.LLM_ERROR: "LLM 调用失败",
    ErrorCode.LLM_AUTH_FAILED: "LLM 鉴权失败（密钥错误或未开通）",
    ErrorCode.LLM_RATE_LIMITED: "LLM 触发限流",
    ErrorCode.LLM_TIMEOUT: "LLM 响应超时",
    ErrorCode.LLM_UPSTREAM_ERROR: "LLM 上游服务异常",
    ErrorCode.LLM_EMPTY_RESPONSE: "LLM 返回空回复",
    ErrorCode.LLM_UNSUPPORTED_PROVIDER: "不支持的 LLM 服务商",
    ErrorCode.TTS_ERROR: "TTS 调用失败",
    ErrorCode.TTS_AUTH_FAILED: "TTS 鉴权失败（密钥错误或未开通）",
    ErrorCode.TTS_RATE_LIMITED: "TTS 触发限流",
    ErrorCode.TTS_TIMEOUT: "TTS 响应超时",
    ErrorCode.TTS_UPSTREAM_ERROR: "TTS 上游服务异常",
    ErrorCode.TTS_INVALID_VOICE: "音色不存在或不被支持",
    ErrorCode.TTS_UNSUPPORTED_FORMAT: "不支持的音频格式",
    ErrorCode.TTS_UNSUPPORTED_PROVIDER: "不支持的 TTS 服务商",
    ErrorCode.TTS_TEXT_TOO_LONG: "文本超过服务商单次合成上限",
    ErrorCode.TASK_NOT_FOUND: "任务不存在或已过期",
    ErrorCode.TASK_NOT_FINISHED: "任务尚未完成",
    ErrorCode.TASK_QUEUE_FULL: "任务队列已满",
    ErrorCode.TASK_CANCELED: "任务已被取消",
    ErrorCode.TASK_ALREADY_FINISHED: "任务已结束",
    ErrorCode.RATE_LIMITED: "请求过于频繁",
    ErrorCode.INTERNAL_ERROR: "服务器内部错误",
    ErrorCode.STORAGE_ERROR: "存储读写失败",
    ErrorCode.AUDIO_NOT_FOUND: "音频文件不存在",
    ErrorCode.SERVICE_UNAVAILABLE: "服务暂不可用",
}


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="健康检查",
    description="用于进程守护（systemd 等）的存活/就绪探针，也用于前端顶栏的状态指示灯。",
    tags=["系统 System"],
)
async def health(session: SessionDep) -> HealthResponse:
    database = "ok"
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        database = f"error: {type(exc).__name__}"
        logger.error("health check database failure error=%s", exc)

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        version=settings.version,
        uptime_seconds=round(time.time() - _STARTED_AT, 3),
        environment=settings.environment,
        database=database,
        providers={
            "llm": len(registry.llm_ids()),
            "tts": len(registry.tts_ids()),
        },
        timestamp=datetime.now(timezone.utc),
    )


@router.get(
    "/meta/error-codes",
    response_model=ErrorCodeCatalogResponse,
    summary="错误码字典",
    description="返回全部错误码、对应的 HTTP 状态码与含义，便于前端做统一提示映射。",
    tags=["元信息 Meta"],
)
async def error_codes() -> ErrorCodeCatalogResponse:
    codes = [
        ErrorCodeItem(
            code=code.value,
            http_status=DEFAULT_STATUS.get(code, 500),
            description=_ERROR_DESCRIPTIONS.get(code, ""),
        )
        for code in ErrorCode
        if code is not ErrorCode.OK
    ]
    codes.sort(key=lambda item: item.code)
    return ErrorCodeCatalogResponse(total=len(codes), codes=codes)


@router.get(
    "/meta/metrics",
    response_model=MetricsResponse,
    summary="运行指标",
    description="进程级计数器（请求数、生成数、降级数、活跃任务、各服务商调用次数）。",
    tags=["元信息 Meta"],
)
async def metrics_endpoint() -> MetricsResponse:
    snapshot = metrics.snapshot()
    snapshot["active_tasks"] = task_manager.active_count()
    return MetricsResponse(**snapshot)


@router.get(
    "/meta/config",
    summary="非敏感运行时配置",
    description="暴露前端的可调参数（文本长度上限、超时、限流阈值等），不含任何密钥。",
    tags=["元信息 Meta"],
)
async def runtime_config() -> dict:
    return {
        "version": settings.version,
        "environment": settings.environment,
        "max_text_length": settings.max_text_length,
        "max_request_bytes": settings.max_request_bytes,
        "llm_timeout_seconds": settings.llm_timeout_seconds,
        "tts_timeout_seconds": settings.tts_timeout_seconds,
        "upstream_max_retries": settings.upstream_max_retries,
        "rate_limit": {
            "enabled": settings.rate_limit_enabled,
            "requests": settings.rate_limit_requests,
            "window_seconds": settings.rate_limit_window_seconds,
            "generate_requests": settings.rate_limit_generate_requests,
            "generate_window_seconds": settings.rate_limit_generate_window_seconds,
        },
        "task_workers": settings.task_workers,
        "task_queue_size": settings.task_queue_size,
        "fallback_enabled": settings.fallback_enabled,
        "docs_enabled": not settings.disable_docs,
    }