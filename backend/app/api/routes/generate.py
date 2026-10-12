"""Synchronous generation route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import RateLimitGenerate, SessionDep
from app.schemas.generation import GenerateRequest, GenerateResponse
from app.services.generation import generation_service

router = APIRouter(tags=["生成 Generation"])


@router.post(
    "/generate",
    response_model=GenerateResponse,
    summary="同步生成：文本 → 大模型回复 → 语音",
    description=(
        "一次性完成两步调用并返回结果。适合脚本化调用；"
        "需要进度反馈的前端请使用 `POST /tasks` + `GET /tasks/{id}` 的异步模式。\n\n"
        "**密钥安全**：请求体中的 `llm.api_key` / `tts.api_key` 仅在本请求内转发给"
        "上游服务商，不会写入数据库、日志或任何持久化存储。\n\n"
        "**降级策略**：当 `options.fallback_enabled=true` 时，若 LLM 或 TTS 调用失败，"
        "服务会返回降级结果（占位回复 / 静音音频）并在响应中标记 `degraded=true`，"
        "而不是直接返回错误。"
    ),
    responses={
        200: {"description": "生成成功（可能为降级结果，见 degraded 字段）"},
        400: {"description": "参数或服务商不合法"},
        401: {"description": "上游服务商鉴权失败"},
        413: {"description": "请求体过大"},
        422: {"description": "请求参数校验失败"},
        429: {"description": "触发限流"},
        502: {"description": "上游服务异常"},
        504: {"description": "上游服务超时"},
    },
)
async def generate(
    payload: GenerateRequest,
    request: Request,
    session: SessionDep,
    _: RateLimitGenerate,
) -> GenerateResponse:
    request_id = getattr(request.state, "request_id", None)
    return await generation_service.generate(
        session, payload, request_id=request_id
    )