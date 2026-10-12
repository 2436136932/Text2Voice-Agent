"""Provider registry: discovery, factory and capability reporting.

The registry is the **only** component that enumerates providers. Routes,
services and schemas never import a concrete adapter class; they ask the
registry by ``id``. That is what makes "add a new TTS vendor" a one-file
change (see ``docs/EXTENDING_TTS.md``).

Example
-------
>>> registry.tts("openai").default_voice
'alloy'
>>> registry.llm_ids()
['anthropic', 'deepseek', ...]
"""

from __future__ import annotations

import importlib.util
from typing import Iterable

from app.adapters.base import LLMAdapter, TTSAdapter
from app.adapters.llm import (
    AnthropicAdapter,
    CustomLLMAdapter,
    DeepSeekAdapter,
    GeminiAdapter,
    MoonshotAdapter,
    OllamaAdapter,
    OpenAIAdapter,
    QwenAdapter,
    ZhipuAdapter,
)
from app.adapters.tts import (
    AzureSpeechAdapter,
    CustomTTSAdapter,
    EdgeTTSAdapter,
    ElevenLabsAdapter,
    MiniMaxTTSAdapter,
    MockLLMAdapter,
    MockTTSAdapter,
    OpenAITTSAdapter,
    VolcengineTTSAdapter,
)
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Instantiated once - adapters are stateless (credentials are per request).
_LLM_ADAPTERS: tuple[LLMAdapter, ...] = (
    OpenAIAdapter(),
    AnthropicAdapter(),
    QwenAdapter(),
    DeepSeekAdapter(),
    GeminiAdapter(),
    ZhipuAdapter(),
    MoonshotAdapter(),
    OllamaAdapter(),
    CustomLLMAdapter(),
    MockLLMAdapter(),
)

_TTS_ADAPTERS: tuple[TTSAdapter, ...] = (
    OpenAITTSAdapter(),
    ElevenLabsAdapter(),
    AzureSpeechAdapter(),
    VolcengineTTSAdapter(),
    MiniMaxTTSAdapter(),
    EdgeTTSAdapter(),
    CustomTTSAdapter(),
    MockTTSAdapter(),
)


def _extra_available(module: str | None) -> bool:
    """True when an optional dependency is importable."""
    if not module:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


class AdapterRegistry:
    """Lookup table for LLM and TTS adapters."""

    def __init__(self) -> None:
        self._llm: dict[str, LLMAdapter] = {adapter.id: adapter for adapter in _LLM_ADAPTERS}
        self._tts: dict[str, TTSAdapter] = {adapter.id: adapter for adapter in _TTS_ADAPTERS}
        self._log_unavailable()

    def _log_unavailable(self) -> None:
        for adapter in _TTS_ADAPTERS:
            if not _extra_available(adapter.requires_extra):
                logger.warning(
                    "provider %s is unavailable: optional dependency %r is not installed",
                    adapter.id, adapter.requires_extra,
                )

    # -- lookup -------------------------------------------------------------
    def llm(self, provider_id: str) -> LLMAdapter:
        adapter = self._llm.get((provider_id or "").strip().lower())
        if adapter is None:
            raise AppError(
                ErrorCode.LLM_UNSUPPORTED_PROVIDER,
                f"不支持的 LLM 服务商 “{provider_id}”。",
                detail={"provider": provider_id, "supported": sorted(self._llm)},
            )
        return adapter

    def tts(self, provider_id: str) -> TTSAdapter:
        adapter = self._tts.get((provider_id or "").strip().lower())
        if adapter is None:
            raise AppError(
                ErrorCode.TTS_UNSUPPORTED_PROVIDER,
                f"不支持的 TTS 服务商 “{provider_id}”。",
                detail={"provider": provider_id, "supported": sorted(self._tts)},
            )
        return adapter

    def get(self, kind: str, provider_id: str) -> LLMAdapter | TTSAdapter:
        return self.llm(provider_id) if kind == "llm" else self.tts(provider_id)

    # -- enumeration --------------------------------------------------------
    def available_tts(self) -> Iterable[TTSAdapter]:
        return (a for a in self._tts.values() if _extra_available(a.requires_extra))

    def available_llm(self) -> Iterable[LLMAdapter]:
        return (a for a in self._llm.values() if _extra_available(a.requires_extra))

    def llm_ids(self) -> list[str]:
        return sorted(self._llm)

    def tts_ids(self) -> list[str]:
        return sorted(self._tts)

    def describe_all(self) -> dict[str, list[dict]]:
        """Payload for ``GET /providers``."""
        return {
            "llm": [adapter.describe() for adapter in self.available_llm()],
            "tts": [adapter.describe() for adapter in self.available_tts()],
        }

    def has(self, kind: str, provider_id: str) -> bool:
        table = self._llm if kind == "llm" else self._tts
        return (provider_id or "").strip().lower() in table


#: Process-wide singleton.
registry = AdapterRegistry()