"""Custom OpenAI-compatible LLM adapter.

Lets the user point at any OpenAI-compatible chat completions endpoint by
filling in ``base_url`` + ``api_key`` in the UI - no code change required.
"""

from __future__ import annotations

from app.adapters.llm.openai_compatible import OpenAICompatibleAdapter


class CustomLLMAdapter(OpenAICompatibleAdapter):
    id = "custom"
    name = "自定义 (OpenAI 兼容)"
    default_model = ""
    default_base_url = ""
    api_key_hint = "sk-..."
    docs_url = ""
    notes = "任意 OpenAI 兼容接口：填写 base_url（如 https://api.example.com/v1）与 API 密钥，点击「获取模型」可自动拉取模型列表。"
    requires_base_url = True
    models = ()