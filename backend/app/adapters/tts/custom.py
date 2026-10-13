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
    name = "自定义 TTS (OpenAI 兼容)"
    default_voice = "alloy"
    default_base_url = ""
    api_key_hint = "sk-..."
    docs_url = ""
    notes = (
        "接入任意 OpenAI 兼容的语音合成服务（含 mimo 等第三方）。"
        "填写 Base URL 与 API Key 后点击「获取模型」自动拉取模型列表，"
        "支持语音合成的模型会带 TTS 标记。"
    )
    requires_base_url = True
    supports_base_url = True
    requires_api_key = True
    #: Never fall back to an OpenAI model name: the endpoint belongs to someone
    #: else and would 404 on "gpt-4o-mini-tts". The user picks a real model via
    #: 「获取模型」, and we send exactly what they chose.
    default_model_name = ""
    #: Synthesize path appended to ``base_url``. Exposed to the UI as an
    #: advanced field so non-standard gateways can be reached without code.
    default_speech_path = "/audio/speech"
    # -- voice cloning ------------------------------------------------------
    # No vendor clone endpoint exists for an arbitrary OpenAI-compatible
    # gateway, but the reference clip can be forwarded on every synthesis call
    # (see ``app.adapters.tts.openai.OpenAITTSAdapter.synthesize``).
    supports_voice_clone = False
    supports_reference_audio = True
    clone_models = ()
    clone_hint = (
        "自定义端点没有统一的克隆接口，因此不支持一次性克隆；"
        "请使用「参考音频」模式：上传/裁剪一段参考音频后直接合成，"
        "后端会在每次合成时把参考音频转发给你的端点。"
    )
    voices = (
        VoiceInfo(id="alloy", name="Alloy", gender="neutral", language="multi"),
        VoiceInfo(id="echo", name="Echo", gender="male", language="multi"),
        VoiceInfo(id="fable", name="Fable", gender="neutral", language="multi"),
        VoiceInfo(id="onyx", name="Onyx", gender="male", language="multi"),
        VoiceInfo(id="nova", name="Nova", gender="female", language="multi"),
        VoiceInfo(id="shimmer", name="Shimmer", gender="female", language="multi"),
    )
    models = ()