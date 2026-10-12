"""Custom OpenAI-compatible TTS adapter.

Same idea as :mod:`app.adapters.llm.custom`: the user supplies ``base_url``
and ``api_key``, and the adapter talks to any endpoint that implements the
OpenAI ``POST /audio/speech`` contract.
"""

from __future__ import annotations

from app.adapters.base import VoiceInfo
from app.adapters.tts.openai import OpenAITTSAdapter


class CustomTTSAdapter(OpenAITTSAdapter):
    id = "custom"
    name = "Custom TTS (OpenAI-compatible)"
    default_voice = "alloy"
    default_base_url = ""
    api_key_hint = "sk-..."
    docs_url = ""
    notes = "Any OpenAI-compatible TTS endpoint. Fill base_url + api_key, then click fetch-models."
    requires_base_url = True
    voices = (
        VoiceInfo(id="alloy", name="Alloy", gender="neutral", language="multi"),
        VoiceInfo(id="echo", name="Echo", gender="male", language="multi"),
        VoiceInfo(id="fable", name="Fable", gender="neutral", language="multi"),
        VoiceInfo(id="onyx", name="Onyx", gender="male", language="multi"),
        VoiceInfo(id="nova", name="Nova", gender="female", language="multi"),
        VoiceInfo(id="shimmer", name="Shimmer", gender="female", language="multi"),
    )
    models = ()