"""Services: business logic between the routes and the adapters."""

from app.services.audio import AudioStore, audio_store, estimate_duration_ms
from app.services.generation import GenerationService, generation_service
from app.services.history import HistoryService, history_service
from app.services.metrics import metrics, MetricsCollector
from app.services.providers import ProviderService, provider_service
from app.services.settings import SettingsService, settings_service
from app.services.tasks import TaskManager, task_manager

__all__ = [
    "AudioStore",
    "audio_store",
    "estimate_duration_ms",
    "GenerationService",
    "generation_service",
    "HistoryService",
    "history_service",
    "MetricsCollector",
    "metrics",
    "ProviderService",
    "provider_service",
    "SettingsService",
    "settings_service",
    "TaskManager",
    "task_manager",
]