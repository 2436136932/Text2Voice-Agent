"""Provider catalog and credential validation routes."""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field, SecretStr

from app.api.deps import RateLimitDefault, RateLimitGenerate, SessionDep
from app.schemas.generation import (
    ProviderListResponse,
    ValidateProviderRequest,
    ValidateProviderResponse,
)
from app.services.providers import provider_service
from app.services.voices import voice_service

router = APIRouter(prefix="/providers", tags=["服务商 Providers"])


# ---------------------------------------------------------------------------
# Fetch models from a custom OpenAI-compatible endpoint
# ---------------------------------------------------------------------------
class FetchModelsRequest(BaseModel):
    """Ask the backend to call ``GET {base_url}/models`` and return the list."""

    base_url: str = Field(min_length=1, max_length=512)
    api_key: SecretStr | None = None
    #: ``"llm"`` fetches chat models; ``"tts"`` fetches audio-speech models.
    kind: str = Field(default="llm", pattern="^(llm|tts)$")

    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class ModelItem(BaseModel):
    id: str
    name: str
    supports_tts: bool = False
    owned_by: str = ""


class FetchModelsResponse(BaseModel):
    models: list[ModelItem]
    total: int
    base_url: str


# Heuristic: model id patterns that indicate TTS / audio capability.
_TTS_HINTS = (
    "tts", "speech", "audio", "voice", "sound",
    "speech-01", "bark", "xtts", "f5-tts",
)


def _looks_like_tts(model_id: str) -> bool:
    mid = model_id.lower()
    return any(hint in mid for hint in _TTS_HINTS)


@router.get(
    "",
    response_model=ProviderListResponse,
    summary="列出所有支持的 LLM / TTS 服务商及其能力",
    description=(
        "返回前端渲染配置表单所需的全部元数据：模型列表、音色列表、"
        "语速范围、支持的音频格式、是否需要 base_url 等。"
        "API 密钥不在其中——它由用户每次请求时携带。\n\n"
        "**声音克隆能力**：TTS 条目额外包含 `supports_voice_clone`、"
        "`supports_reference_audio`、`clone_models`、`clone_hint` 四个字段，"
        "前端据此决定是否展示「克隆音色」入口以及推荐哪个模型。\n\n"
        "**我的音色**：本机克隆成功的音色会合并到对应服务商的 `voices` 列表中，"
        "并标记 `cloned=true`。因此音色列表是动态的，克隆成功后刷新本接口即可选到新音色。"
    ),
    responses={429: {"description": "触发限流"}},
)
async def list_providers(
    _: RateLimitDefault,
    session: SessionDep,
) -> ProviderListResponse:
    catalog = provider_service.catalog()
    # ``registry.describe_all()`` stays a pure static catalog; the user's own
    # cloned voices are merged in here so the UI can offer them immediately
    # after a successful clone (no backend restart needed).
    cloned = await voice_service.by_provider(session)
    for entry in catalog.get("tts", []):
        extra_voices = cloned.get(entry["id"])
        if not extra_voices:
            continue
        entry["voices"] = [*entry.get("voices", []), *[_as_voice(item) for item in extra_voices]]
    return ProviderListResponse(**catalog)


def _as_voice(item: dict) -> dict:
    """Render a ``ClonedVoice`` row as a ``VoiceInfo``-shaped dict."""
    return {
        "id": item["voice_id"],
        "name": f"{item['name']}（我的音色）",
        "gender": "neutral",
        "language": "multi",
        "description": f"克隆音色 · 模型 {item.get('model') or '默认'}",
        "cloned": True,
    }


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


@router.post(
    "/models",
    response_model=FetchModelsResponse,
    summary="从自定义 OpenAI 兼容端点拉取模型列表",
    description=(
        "用提交的 base_url + api_key 调用 `GET {base_url}/models`，"
        "返回模型列表。当 kind=tts 时，会在支持语音合成的模型上标记 "
        "`supports_tts=true`（按模型名启发式判断：含 tts/speech/audio/voice 等）。\n\n"
        "密钥仅用于本次请求，不落库、不记录。"
    ),
    responses={
        200: {"description": "模型列表"},
        400: {"description": "base_url 不合法或缺少密钥"},
        502: {"description": "上游端点不可达或返回异常"},
    },
)
async def fetch_models(
    payload: FetchModelsRequest,
    _: RateLimitGenerate,
) -> FetchModelsResponse:
    import time

    import httpx

    from app.adapters.base import get_http_client, map_upstream_error
    from app.core.errors import AppError, ErrorCode
    from app.core.logging import get_logger

    logger = get_logger(__name__)

    base_url = payload.base_url.rstrip("/")
    api_key = payload.api_key.get_secret_value() if payload.api_key else ""

    if not api_key:
        raise AppError(
            ErrorCode.MISSING_CREDENTIAL,
            "请填写 API 密钥后再获取模型。",
            detail={"kind": payload.kind},
        )

    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    logger.info("fetching models from %s kind=%s", url, payload.kind)
    started = time.perf_counter()
    client = get_http_client()
    try:
        response = await client.get(url, headers=headers, timeout=20.0)
    except Exception as exc:
        raise map_upstream_error(
            kind="llm", provider="custom", exception=exc, endpoint=url
        ) from exc

    if response.status_code >= 400:
        try:
            body = response.json()
        except Exception:
            body = response.text
        raise map_upstream_error(
            kind="llm", provider="custom", status_code=response.status_code,
            body=body, endpoint=url,
        )

    try:
        data = response.json()
    except Exception as exc:
        raise AppError(
            ErrorCode.LLM_UPSTREAM_ERROR,
            f"端点返回了非 JSON 响应，无法解析模型列表。",
            detail={"base_url": base_url},
            retryable=False,
        ) from exc

    # OpenAI format: {"data": [{"id": "gpt-4o", "owned_by": "openai"}, ...]}
    raw_models = data.get("data", []) if isinstance(data, dict) else []
    if not raw_models and isinstance(data, list):
        raw_models = data  # some endpoints return a bare list

    models: list[ModelItem] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            continue
        supports_tts = _looks_like_tts(model_id) if payload.kind == "tts" else False
        models.append(ModelItem(
            id=model_id,
            name=model_id,
            supports_tts=supports_tts,
            owned_by=str(item.get("owned_by", "")),
        ))

    latency = int((time.perf_counter() - started) * 1000)
    logger.info("fetched %d models from %s in %dms", len(models), base_url, latency)

    return FetchModelsResponse(models=models, total=len(models), base_url=base_url)