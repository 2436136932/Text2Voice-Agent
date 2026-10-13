"""Model catalogue helpers: upstream fetching + local annotations.

Why this module exists
----------------------
Preset adapters ship a hand-written ``models`` tuple so the UI is useful before
any network call happens — the Chinese names (“gpt-4o-mini (快速、便宜)”,
“mimo-v2.5-tts (预置音色)”) are far more helpful than raw ids.

The downside is that a hard-coded list goes stale the moment a vendor adds or
retires a model. The fix is not to drop the local list (that loses the
readability) nor to trust it forever (that offers models which no longer exist),
but to treat them as **two layers**:

* ``GET /providers`` serves the local list, so the UI works immediately;
* ``POST /providers/models`` fetches the **upstream** list and merges the local
  annotations onto it by id.

The result is a live inventory that keeps its Chinese descriptions, and any
model the vendor adds shows up without a code change.

Authentication differs per vendor, which is why :func:`build_model_headers`
exists: MiMo uses ``api-key`` while OpenAI-compatible providers use
``Authorization: Bearer``. Sending the wrong header yields a 401 that looks
like an invalid key, so this must not be guessed at the call site.
"""

from __future__ import annotations

from app.adapters.registry import registry
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Providers that authenticate with a bare ``api-key`` header instead of the
#: OpenAI ``Authorization: Bearer`` convention.
MIMO_AUTH_HEADER = "api-key"

#: ``provider_id -> auth header name`` for the non-OpenAI schemes.
AUTH_HEADER_BY_PROVIDER: dict[str, str] = {
    "mimo": MIMO_AUTH_HEADER,
    # Anthropic exposes /v1/models but with its own version header; listing it
    # here keeps the request honest if the endpoint is added later.
    "anthropic": "x-api-key",
}


def build_model_headers(api_key: str, *, provider_id: str) -> dict[str, str]:
    """Return the headers this provider expects for ``GET /models``."""
    header_name = AUTH_HEADER_BY_PROVIDER.get(provider_id)
    headers = {"Accept": "application/json"}
    if header_name == MIMO_AUTH_HEADER:
        # MiMo: the key goes in ``api-key`` verbatim (no "Bearer " prefix).
        headers[header_name] = api_key
    elif header_name:
        headers[header_name] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    if provider_id == "anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")
    return headers


def resolve_models_url(
    base_url: str,
    api_key: str,
    *,
    provider_id: str,
    kind: str,  # noqa: ARG001 - kept for a symmetric signature
) -> tuple[str, dict[str, str]]:
    """Return ``(url, headers)`` for the provider's model-listing endpoint."""
    return f"{base_url.rstrip('/')}/models", build_model_headers(api_key, provider_id=provider_id)


def local_model_notes(provider_id: str, kind: str) -> dict[str, str]:
    """Map ``model_id -> local description`` for one provider.

    Used to re-attach the Chinese annotations to an upstream list, so a fetched
    model still reads as “mimo-v2.5-tts (预置音色)” rather than a bare id.
    """
    notes: dict[str, str] = {}
    try:
        if kind == "llm":
            adapter = registry.llm(provider_id)
        else:
            adapter = registry.tts(provider_id)
    except Exception:  # noqa: BLE001 - unknown provider: no annotations
        return notes

    for entry in getattr(adapter, "models", ()) or ():
        if isinstance(entry, dict):
            model_id = str(entry.get("id", "")).strip()
            name = str(entry.get("name", "")).strip()
            if model_id and name:
                notes[model_id] = name
    return notes


def local_models(provider_id: str, kind: str) -> list[dict[str, str]]:
    """The adapter's built-in model list (fallback when upstream is unavailable)."""
    try:
        adapter = registry.llm(provider_id) if kind == "llm" else registry.tts(provider_id)
    except Exception:  # noqa: BLE001
        return []
    return [dict(entry) for entry in (getattr(adapter, "models", ()) or ())]


def local_voices(provider_id: str) -> list[dict[str, str]]:
    """The adapter's built-in voice list, annotated for display."""
    try:
        adapter = registry.tts(provider_id)
    except Exception:  # noqa: BLE001
        return []
    return [voice.to_dict() for voice in adapter.voices]