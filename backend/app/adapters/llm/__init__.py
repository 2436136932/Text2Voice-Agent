"""LLM (chat completion) adapters, one module per vendor."""

from app.adapters.llm.anthropic import AnthropicAdapter
from app.adapters.llm.deepseek import DeepSeekAdapter
from app.adapters.llm.gemini import GeminiAdapter
from app.adapters.llm.moonshot import MoonshotAdapter
from app.adapters.llm.ollama import OllamaAdapter
from app.adapters.llm.openai import OpenAIAdapter
from app.adapters.llm.qwen import QwenAdapter
from app.adapters.llm.zhipu import ZhipuAdapter

__all__ = [
    "AnthropicAdapter",
    "DeepSeekAdapter",
    "GeminiAdapter",
    "MoonshotAdapter",
    "OllamaAdapter",
    "OpenAIAdapter",
    "QwenAdapter",
    "ZhipuAdapter",
]