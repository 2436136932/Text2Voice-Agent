"""Provider catalog and credential validation routes."""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field, SecretStr

from app.adapters.registry import registry
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
# Fetch models from upstream
# ---------------------------------------------------------------------------
class FetchModelsRequest(BaseModel):
    """Ask the backend to call the provider's ``GET /models`` endpoint."""

    #: Provider id — a built-in one (``mimo``, ``openai``…) or ``custom``.
    provider: str = Field(default="custom", max_length=32)
    #: Required for ``custom``; optional for built-ins, which have a default.
    base_url: str = Field(default="", max_length=512)
    api_key: SecretStr | None = None
    #: ``"llm"`` fetches chat models; ``"tts"`` fetches audio-speech models.
    kind: str = Field(default="llm", pattern="^(llm|tts)$")

    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class ModelItem(BaseModel):
    id: str
    name: str
    supports_tts: bool = False
    owned_by: str = ""
    #: Local Chinese annotation, re-attached by id (may be empty for a model the
    #: vendor added since this build).
    note: str = ""
    #: ``"upstream"`` when it came from the live API, ``"local"`` for the
    #: built-in fallback list.
    source: str = "upstream"


class FetchModelsResponse(BaseModel):
    models: list[ModelItem]
    total: int
    base_url: str
    #: Where this list came from — lets the UI label it honestly.
    source: str = "upstream"


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
    summary="从上游拉取真实模型列表",
    description=(
        "调用服务商的 `GET {base_url}/models` 返回**上游真实**的模型列表，"
        "避免本地预设列表在上游增删模型后过期。\n\n"
        "`provider` 可传内置服务商 id（如 `mimo`、`openai`）或 `custom`：\n"
        "* 内置服务商：`base_url` 可留空，自动使用其默认地址，"
        "并按该服务商自己的认证方式发送密钥（例如 MiMo 用 `api-key`，"
        "OpenAI 用 `Authorization: Bearer`）；\n"
        "* `custom`：必须提供 `base_url`，按 OpenAI 兼容约定认证。\n\n"
        "返回的每个模型都会带上本地注释（`note`）与 TTS 能力标记，"
        "便于在保留中文说明的同时反映上游的真实情况。\n\n"
        "**注意**：并非所有服务商都实现 `/models`（如 Anthropic、Azure）。"
        "此时返回 502 并给出说明，前端应回退到内置列表。\n\n"
        "密钥仅用于本次请求，不落库、不记录。"
    ),
    responses={
        200: {"description": "上游模型列表"},
        400: {"description": "缺少必要参数（MISSING_CREDENTIAL / INVALID_PROVIDER）"},
        502: {"description": "上游端点不可达、未实现 /models 或返回异常"},
    },
)
async def fetch_models(
    payload: FetchModelsRequest,
    _: RateLimitGenerate,
) -> FetchModelsResponse:
    import time

    from app.adapters.base import get_http_client, map_upstream_error
    from app.core.errors import AppError, ErrorCode
    from app.core.logging import get_logger
    from app.services.model_catalog import (
        MIMO_AUTH_HEADER,
        build_model_headers,
        local_model_notes,
        resolve_models_url,
    )

    logger = get_logger(__name__)

    api_key = payload.api_key.get_secret_value() if payload.api_key else ""
    if not api_key:
        raise AppError(
            ErrorCode.MISSING_CREDENTIAL,
            "请填写 API 密钥后再获取模型。",
            detail={
                "kind": payload.kind,
                "suggestion": "上游的 /models 接口需要鉴权，请先填入有效的 API Key。",
            },
        )

    # Resolve the endpoint from the *adapter* so built-in providers work without
    # the user retyping a base URL, and so provider-specific auth is respected.
    base_url = payload.base_url.strip()
    provider_id = (payload.provider or "custom").strip().lower()

    try:
        adapter = (
            registry.llm(provider_id) if payload.kind == "llm" else registry.tts(provider_id)
        )
    except AppError as exc:
        exc.detail.setdefault(
            "suggestion",
            "请确认服务商 id 正确；自定义端点请选择「自定义 (OpenAI 兼容)」。",
        )
        raise

    effective_base = adapter.resolve_base_url(base_url or None)
    if not effective_base:
        raise AppError(
            ErrorCode.INVALID_PROVIDER,
            f"{adapter.name} 需要 base_url 才能获取模型列表。",
            detail={
                "provider": provider_id,
                "suggestion": "请在「高级设置 → Base URL」中填写接口地址（如 https://api.example.com/v1）。",
            },
        )

    url, headers = resolve_models_url(
        effective_base,
        api_key,
        provider_id=provider_id,
        kind=payload.kind,
    )

    logger.info("fetching models provider=%s url=%s kind=%s", provider_id, url, payload.kind)
    started = time.perf_counter()
    client = get_http_client()
    try:
        response = await client.get(url, headers=headers, timeout=20.0)
    except Exception as exc:
        raise map_upstream_error(
            kind="llm", provider=provider_id, exception=exc, endpoint=url
        ) from exc

    if response.status_code >= 400:
        try:
            body = response.json()
        except Exception:
            body = response.text
        error = map_upstream_error(
            kind="llm", provider=provider_id, status_code=response.status_code,
            body=body, endpoint=url,
        )
        # Make the fallback path explicit for the UI.
        error.detail.setdefault(
            "suggestion",
            f"{adapter.name} 未提供可用的 /models 接口，请手动输入模型名。"
            "当前显示的仍是内置列表。",
        )
        raise error

    try:
        data = response.json()
    except Exception as exc:
        raise AppError(
            ErrorCode.LLM_UPSTREAM_ERROR,
            "端点返回了非 JSON 响应，无法解析模型列表。",
            detail={
                "base_url": effective_base,
                "suggestion": "请确认该地址是 API 端点而不是网页地址。",
            },
            retryable=False,
        ) from exc

    # OpenAI format: {"data": [{"id": "gpt-4o", "owned_by": "openai"}, ...]}
    raw_models = data.get("data", []) if isinstance(data, dict) else []
    if not raw_models and isinstance(data, list):
        raw_models = data  # some endpoints return a bare list

    notes = local_model_notes(provider_id, payload.kind)

    models: list[ModelItem] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            continue
        models.append(ModelItem(
            id=model_id,
            name=model_id,
            supports_tts=_looks_like_tts(model_id) if payload.kind == "tts" else False,
            owned_by=str(item.get("owned_by", "")),
            note=notes.get(model_id, ""),
            source="upstream",
        ))

    latency = int((time.perf_counter() - started) * 1000)
    logger.info(
        "fetched %d upstream models provider=%s in %dms", len(models), provider_id, latency
    )

    return FetchModelsResponse(
        models=models,
        total=len(models),
        base_url=effective_base,
        source="upstream",
    )


# ---------------------------------------------------------------------------
# Endpoint discovery
# ---------------------------------------------------------------------------
class ProbePathsRequest(BaseModel):
    """Try the common TTS routes against a custom endpoint."""

    base_url: str = Field(min_length=1, max_length=512)
    api_key: SecretStr | None = None

    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class ProbePathItem(BaseModel):
    path: str
    status: int | None = None
    alive: bool = False
    note: str = ""


class ProbePathsResponse(BaseModel):
    base_url: str
    results: list[ProbePathItem]
    suggested_path: str | None = None


@router.post(
    "/probe-paths",
    response_model=ProbePathsResponse,
    summary="探测自定义端点实际支持哪些语音合成路径",
    description=(
        "第三方网关往往不是 OpenAI 的标准路径。此接口并发探测一批常见路径"
        "（/audio/speech、/tts、/text-to-speech…），返回每个路径的响应状态，"
        "并给出建议路径。\n\n"
        "判定标准：返回 400/401/403/422 等也表示**路径存在**（只是请求体或密钥"
        "不合），只有 404 才说明该路径不存在。\n\n"
        "密钥仅用于本次探测，不落库、不记录。"
    ),
    responses={
        200: {"description": "探测完成"},
        400: {"description": "缺少 base_url 或密钥"},
    },
)
async def probe_paths(
    payload: ProbePathsRequest,
    _: RateLimitGenerate,
) -> ProbePathsResponse:
    from app.core.errors import AppError, ErrorCode
    from app.services.endpoint_probe import best_path, probe_speech_paths

    base_url = payload.base_url.rstrip("/")
    api_key = payload.api_key.get_secret_value() if payload.api_key else ""
    if not api_key:
        raise AppError(
            ErrorCode.MISSING_CREDENTIAL,
            "请先填写 API Key 再探测接口路径。",
            detail={"suggestion": "探测需要携带密钥，因为它会真实访问你的端点。"},
        )

    results = await probe_speech_paths(base_url, api_key)
    found = best_path(results)
    return ProbePathsResponse(
        base_url=base_url,
        results=[ProbePathItem(**item.to_dict()) for item in results],  # type: ignore[arg-type]
        suggested_path=found,
    )