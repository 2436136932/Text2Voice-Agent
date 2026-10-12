"""Services: business logic between the routes and the adapters."""

from app.services.audio import AudioStore, audio_store, estimate_duration_ms
from app.services.generation import GenerationService, generation_service
from app.services.history import HistoryService, history_service
from app.services.media import MediaRecord, MediaStore, media_store
from app.services.metrics import metrics, MetricsCollector
from app.services.providers import ProviderService, provider_service, suggest_model_for
from app.services.settings import SettingsService, settings_service
from app.services.tasks import TaskManager, task_manager
from app.services.tools import ToolStatus, ffmpeg_status
from app.services.voices import VoiceService, voice_service

__all__ = [
    "AudioStore",
    "audio_store",
    "estimate_duration_ms",
    "GenerationService",
    "generation_service",
    "HistoryService",
    "history_service",
    "MediaRecord",
    "MediaStore",
    "media_store",
    "MetricsCollector",
    "metrics",
    "ProviderService",
    "provider_service",
    "suggest_model_for",
    "SettingsService",
    "settings_service",
    "TaskManager",
    "task_manager",
    "ToolStatus",
    "ffmpeg_status",
    "VoiceService",
    "voice_service",
]
