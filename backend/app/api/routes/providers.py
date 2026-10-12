"""Provider catalog and credential validation routes."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import RateLimitDefault, RateLimitGenerate
from app.schemas.generation import (
    ProviderListResponse,
    ValidateProviderRequest,
    ValidateProviderResponse,
)
from app.services.providers import provider_service

router = APIRouter(prefix="/providers", tags=["服务商 Providers"])


@router.get(
    "",
    response_model=ProviderListResponse,
    summary="列出所有支持的 LLM / TTS 服务商及其能力",
    description=(
        "返回前端渲染配置表单所需的全部元数据：模型列表、音色列表、"
        "语速范围、支持的音频格式、是否需要 base_url 等。"
        "API 密钥不在其中——它由用户每次请求时携带。"
    ),
)
async def list_providers(_: RateLimitDefault) -> ProviderListResponse:
    catalog = provider_service.catalog()
    return ProviderListResponse(**catalog)


@router.post(
    "/validate",
    response_model=ValidateProviderResponse,
    summary="校验服务商密钥是否可用",
    description=(
        "使用提交的密钥发起一次最小开销的探测请求（LLM 为 1 token 的补全，"
        "TTS 为一段极短文本的合成）。密钥仅用于本次探测，不会被保存或记录。\n\n"
        "**注意**：校验失败仍返回 HTTP 200，`valid=false`，"
        "以便前端统一展示“测试连接”的结果。"
    ),
    responses={
        200: {"description": "探测完成（valid 可能为 false）"},
        400: {"description": "服务商不支持或参数不合法"},
        429: {"description": "触发限流"},
    },
)
async def validate_provider(
    payload: ValidateProviderRequest,
    _: RateLimitGenerate,
) -> ValidateProviderResponse:
    result = await provider_service.validate(payload)
    return ValidateProviderResponse(
        valid=result.valid,
        message=result.message,
        latency_ms=result.latency_ms,
        provider=result.provider,
        kind=result.kind,
        detail=result.detail,
    )