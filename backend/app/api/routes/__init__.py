"""API routers."""

from app.api.routes import (
    audio,
    generate,
    health,
    history,
    meta,
    providers,
    settings,
    tasks,
)

__all__ = ["audio", "generate", "health", "history", "meta", "providers", "settings", "tasks"]