"""Moonshot AI (Kimi) chat completions - OpenAI compatible."""

from __future__ import annotations

from app.adapters.llm.openai_compatible import OpenAICompatibleAdapter


class MoonshotAdapter(OpenAICompatibleAdapter):
    id = "moonshot"
    name = "Moonshot (Kimi)"
    default_model = "moonshot-v1-8k"
    default_base_url = "https://api.moonshot.cn/v1"
    api_key_hint = "sk-..."
    docs_url = "https://platform.moonshot.cn/docs"
    notes = "Kimi 系列，长文本能力强。"
    models = (
        {"id": "moonshot-v1-8k", "name": "moonshot-v1-8k"},
        {"id": "moonshot-v1-32k", "name": "moonshot-v1-32k"},
        {"id": "moonshot-v1-128k", "name": "moonshot-v1-128k"},
    )