"""Provider service: capability catalog and credential validation.

``validate`` never persists anything; it builds an adapter request from the
submitted credentials, performs the cheapest possible probe, and returns the
outcome as a value object (including failures - an invalid key is a *valid*
response, not an HTTP error).
"""

from __future__ import annotations

from typing import Any

from app.adapters.base import LLMRequest, TTSAdapter, TTSRequest, ValidationResult
from app.adapters.registry import registry
from app.core.logging import get_logger
from app.schemas.generation import ValidateProviderRequest

logger = get_logger(__name__)


def suggest_model_for(kind: str, provider: str, capability: str = "") -> str:
    """Advise the user which model to pick for a capability, in Chinese.

    The failure this exists for is mundane but confusing: a user selects a
    **text-only** model for TTS (``mimo-v2.5-pro`` instead of
    ``mimo-v2.5-tts``), the vendor answers "model not found", and the raw
    upstream text tells them nothing about what to do next.

    Parameters
    ----------
    kind:
        ``"llm"`` or ``"tts"``.
    provider:
        Provider id, used only to name the provider in the message.
    capability:
        Optional hint - ``"tts"`` / ``"clone"`` - so a cloning failure points
        at the *clone-capable* models and the provider's own ``clone_hint``.

    Returns
    -------
    str
        A complete, actionable sentence. Empty when nothing useful is known.
    """
    if kind == "llm":
        try:
            adapter = registry.llm(provider)
        except Exception:  # noqa: BLE001 - suggestion must never mask the real error
            return ""
        models = [str(item.get("id", "")) for item in getattr(adapter, "models", ())]
        if models:
            return (
                f"请在「系统设置」把 LLM 模型切换为 {models[0]}"
                f"（{provider} 可用模型：{', '.join(models[:4])}）。"
            )
        return ""

    try:
        adapter = registry.tts(provider)
    except Exception:  # noqa: BLE001
        return ""

    if capability == "clone" and adapter.clone_hint:
        # The provider's own hint is more specific than anything generic.
        return adapter.clone_hint

    models = [str(item.get("id", "")) for item in getattr(adapter, "models", ())]
    label = "TTS（语音合成）" if capability != "clone" else "声音克隆"
    if models:
        names = "、".join(models[:4])
        return (
            f"请在「系统设置」把 TTS 模型切换为支持{label}的模型（{provider} 可选：{names}）。"
            + (f" {adapter.clone_hint}" if capability == "clone" and adapter.clone_hint else "")
        )
    if adapter.clone_hint:
        return adapter.clone_hint
    return f"请确认 {provider} 的模型名填写正确，或改用其默认模型。"


def annotate_tts_error(error: Any, provider: str, capability: str = "") -> Any:
    """Attach a ``suggestion`` to a TTS error whose cause looks model-related.

    Only errors that plausibly stem from a model/端point mismatch are touched;
    authentication, rate limiting and timeouts already carry a clear message
    and must not be muddied with model advice.
    """
    from app.core.errors import AppError, ErrorCode

    if not isinstance(error, AppError):
        return error

    model_related_codes = {
        ErrorCode.TTS_ERROR,
        ErrorCode.TTS_INVALID_VOICE,
        ErrorCode.TTS_UPSTREAM_ERROR,
    }
    if error.code not in model_related_codes:
        return error

    message = (error.message or "").lower()
    upstream = str(error.detail.get("upstream_message", "")).lower()
    haystack = f"{message} {upstream}"
    if not any(token in haystack for token in ("model", "模型", "404", "not found", "voice")):
        return error

    suggestion = suggest_model_for("tts", provider, capability)
    if not suggestion:
        return error
    if "suggestion" not in error.detail:
        error.detail["suggestion"] = suggestion
    return error


class ProviderService:
    """Read-only provider catalog + connection probing."""

    def catalog(self) -> dict[str, list[dict]]:
        return registry.describe_all()

    async def validate(self, payload: ValidateProviderRequest) -> ValidationResult:
        api_key = payload.api_key.get_secret_value() if payload.api_key else ""

        if payload.kind == "llm":
            adapter = registry.llm(payload.provider)
            request = LLMRequest(
                prompt="ping",
                api_key=api_key,
                model=payload.model or adapter.default_model,
                base_url=payload.base_url,
                temperature=0.0,
                max_tokens=1,
                extra=payload.extra,
            )
        else:
            adapter = registry.tts(payload.provider)
            request = TTSRequest(
                text="你好，这是一条语音测试。",
                api_key=api_key,
                voice=payload.voice or adapter.default_voice,
                base_url=payload.base_url,
                speed=payload.speed or 1.0,
                audio_format=adapter.formats[0],
                extra=payload.extra,
            )

        logger.info(
            "validating provider kind=%s provider=%s model=%s voice=%s",
            payload.kind, payload.provider, payload.model, payload.voice,
        )
        result = await adapter.validate(request)  # type: ignore[arg-type]
        logger.info(
            "validation result provider=%s kind=%s valid=%s latency_ms=%s",
            payload.provider, payload.kind, result.valid, result.latency_ms,
        )
        return result


provider_service = ProviderService()