"""TTS (speech synthesis) adapters, one module per vendor."""

from app.adapters.tts.azure import AzureSpeechAdapter
from app.adapters.tts.custom import CustomTTSAdapter
from app.adapters.tts.edge import EdgeTTSAdapter
from app.adapters.tts.elevenlabs import ElevenLabsAdapter
from app.adapters.tts.minimax import MiniMaxTTSAdapter
from app.adapters.tts.mock import MockLLMAdapter, MockTTSAdapter
from app.adapters.tts.openai import OpenAITTSAdapter
from app.adapters.tts.volcengine import VolcengineTTSAdapter

__all__ = [
    "AzureSpeechAdapter",
    "CustomTTSAdapter",
    "EdgeTTSAdapter",
    "ElevenLabsAdapter",
    "MiniMaxTTSAdapter",
    "MockLLMAdapter",
    "MockTTSAdapter",
    "OpenAITTSAdapter",
    "VolcengineTTSAdapter",
]