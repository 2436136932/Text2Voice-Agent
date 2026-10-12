"""Provider adapters: unified interfaces over LLM and TTS vendors.

Layout
------
``base.py``
    Abstract contracts, request/result value objects, shared HTTP client and
    the upstream-error mapper.
``llm/``
    One module per chat-completion vendor.
``tts/``
    One module per speech-synthesis vendor.
``registry.py``
    Discovery + factory; the only place that knows the full provider list.

Adding a vendor means adding one module and one ``register()`` call - no route,
schema or service change is required. See ``docs/EXTENDING_TTS.md``.
"""

from app.adapters.base import (
    AdapterInfo,
    LLMAdapter,
    LLMRequest,
    LLMResult,
    TTSAdapter,
    TTSRequest,
    TTSResult,
    VoiceInfo,
    get_http_client,
    map_upstream_error,
    shutdown_http_client,
)

__all__ = [
    "AdapterInfo",
    "LLMAdapter",
    "LLMRequest",
    "LLMResult",
    "TTSAdapter",
    "TTSRequest",
    "TTSResult",
    "VoiceInfo",
    "get_http_client",
    "map_upstream_error",
    "shutdown_http_client",
]