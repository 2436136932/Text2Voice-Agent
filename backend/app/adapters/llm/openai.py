"""OpenAI chat completions (also covers Azure OpenAI via ``base_url``)."""

from __future__ import annotations

from app.adapters.llm.openai_compatible import OpenAICompatibleAdapter


class OpenAIAdapter(OpenAICompatibleAdapter):
    id = "openai"
    name = "OpenAI"
    default_model = "gpt-4o-mini"
    default_base_url = "https://api.openai.com/v1"
    api_key_hint = "sk-..."
    docs_url = "https://platform.openai.com/docs/api-reference/chat"
    notes = "支持 GPT-4o / GPT-4o-mini / GPT-4.1 等；填写 base_url 可指向 Azure OpenAI 或兼容网关。"
    models = (
        {"id": "gpt-4o-mini", "name": "gpt-4o-mini (快速、便宜)"},
        {"id": "gpt-4o", "name": "gpt-4o (旗舰多模态)"},
        {"id": "gpt-4.1-mini", "name": "gpt-4.1-mini"},
        {"id": "gpt-4.1", "name": "gpt-4.1"},
        {"id": "gpt-3.5-turbo", "name": "gpt-3.5-turbo (兼容旧账号)"},
    )