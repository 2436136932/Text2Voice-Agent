"""通义千问 (Alibaba DashScope) via the OpenAI compatible endpoint.

DashScope exposes an OpenAI-compatible surface at
``https://dashscope.aliyuncs.com/compatible-mode/v1``, so we reuse the shared
base class. The native ``/api/v1/services/aigc/text-generation/generation``
API is available through ``base_url`` for advanced users but is not required.
"""

from __future__ import annotations

from app.adapters.llm.openai_compatible import OpenAICompatibleAdapter


class QwenAdapter(OpenAICompatibleAdapter):
    id = "qwen"
    name = "通义千问 (DashScope)"
    default_model = "qwen-plus"
    default_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_hint = "sk-..."
    docs_url = "https://help.aliyun.com/zh/model-studio/developer-reference/compatibility-of-openai-with-dashscope"
    notes = "使用 DashScope 兼容模式，密钥在阿里云百炼控制台获取。"
    models = (
        {"id": "qwen-plus", "name": "qwen-plus (均衡)"},
        {"id": "qwen-turbo", "name": "qwen-turbo (快速)"},
        {"id": "qwen-max", "name": "qwen-max (最强)"},
        {"id": "qwen-long", "name": "qwen-long (长文本)"},
        {"id": "qwen2.5-7b-instruct", "name": "qwen2.5-7b-instruct (开源)"},
    )