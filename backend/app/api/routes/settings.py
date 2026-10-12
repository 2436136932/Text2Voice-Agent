"""User preference routes.

There is deliberately **no endpoint that accepts an API key**: preferences and
credentials are different things, and only preferences are persisted.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import RateLimitDefault, SessionDep
from app.schemas.generation import SettingsSchema, SettingsUpdateRequest
from app.services.settings import settings_service

router = APIRouter(prefix="/settings", tags=["系统设置 Settings"])


@router.get(
    "",
    response_model=SettingsSchema,
    summary="读取用户偏好设置",
    description=(
        "包含默认服务商、默认音色、默认语速、是否自动保存历史/凭据等。\n\n"
        "**不含任何 API 密钥**——密钥由前端在内存中保存并在每次请求时提交。"
    ),
)
async def get_settings(session: SessionDep, _: RateLimitDefault) -> SettingsSchema:
    return await settings_service.get(session)


@router.put(
    "",
    response_model=SettingsSchema,
    summary="更新用户偏好设置（局部更新）",
    description=(
        "只提交需要修改的字段。请求体**不接受** `api_key` 等凭据字段，"
        "传入会被校验拒绝（422），以保证“密钥不落库”这一约束在接口层面成立。"
    ),
    responses={422: {"description": "参数校验失败（含尝试提交凭据字段）"}},
)
async def update_settings(
    payload: SettingsUpdateRequest,
    session: SessionDep,
    _: RateLimitDefault,
) -> SettingsSchema:
    return await settings_service.update(session, payload)