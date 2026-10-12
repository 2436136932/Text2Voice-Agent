"""Zhipu AI (GLM / 智谱) chat completions - OpenAI compatible."""

from __future__ import annotations

from app.adapters.llm.openai_compatible import OpenAICompatibleAdapter


class ZhipuAdapter(OpenAICompatibleAdapter):
    id = "zhipu"
    name = "智谱 GLM"
    default_model = "glm-4-flash"
    default_base_url = "https://open.bigmodel.cn/api/paas/v4"
    api_key_hint = "xxxxx.xxxxx"
    docs_url = "https://open.bigmodel.cn/dev/api"
    notes = "密钥格式为 id.secret，glm-4-flash 免费额度友好。"
    models = (
        {"id": "glm-4-flash", "name": "glm-4-flash (免费)"},
        {"id": "glm-4-air", "name": "glm-4-air"},
        {"id": "glm-4-plus", "name": "glm-4-plus"},
    )