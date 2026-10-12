"""Ollama / vLLM / LM Studio style local models - OpenAI compatible.

Local deployments usually need no credential, hence ``requires_api_key`` is
false; the UI will hide the key field for this provider.
"""

from __future__ import annotations

from app.adapters.llm.openai_compatible import OpenAICompatibleAdapter


class OllamaAdapter(OpenAICompatibleAdapter):
    id = "ollama"
    name = "Ollama / 本地模型"
    default_model = "qwen2.5:7b"
    default_base_url = "http://localhost:11434/v1"
    api_key_hint = "(本地部署通常留空)"
    docs_url = "https://github.com/ollama/ollama/blob/main/docs/openai.md"
    notes = "本地推理，无需 API 密钥；请把 base_url 指向你的 Ollama / vLLM 服务。"
    requires_api_key = False
    models = (
        {"id": "qwen2.5:7b", "name": "qwen2.5:7b"},
        {"id": "llama3.1:8b", "name": "llama3.1:8b"},
        {"id": "deepseek-r1:7b", "name": "deepseek-r1:7b"},
    )