"""DeepSeek chat completions (OpenAI compatible)."""

from __future__ import annotations

from app.adapters.llm.openai_compatible import OpenAICompatibleAdapter


class DeepSeekAdapter(OpenAICompatibleAdapter):
    id = "deepseek"
    name = "DeepSeek"
    default_model = "deepseek-chat"
    default_base_url = "https://api.deepseek.com/v1"
    api_key_hint = "sk-..."
    docs_url = "https://api-docs.deepseek.com/zh-cn/"
    notes = "deepseek-chat 通用对话，deepseek-reasoner 推理模型。"
    models = (
        {"id": "deepseek-chat", "name": "deepseek-chat"},
        {"id": "deepseek-reasoner", "name": "deepseek-reasoner (R1)"},
    )