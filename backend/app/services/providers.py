"""Provider service: capability catalog and credential validation.

``validate`` never persists anything; it builds an adapter request from the
submitted credentials, performs the cheapest possible probe, and returns the
outcome as a value object (including failures - an invalid key is a *valid*
response, not an HTTP error).
"""

from __future__ import annotations

from app.adapters.base import LLMRequest, TTSRequest, ValidationResult
from app.adapters.registry import registry
from app.core.logging import get_logger
from app.schemas.generation import ValidateProviderRequest

logger = get_logger(__name__)


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