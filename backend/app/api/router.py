"""API version 1 router aggregation.

Every route module is mounted here under ``settings.api_prefix`` (default
``/api/v1``). Adding a feature = add a module + one ``include_router`` call.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    audio,
    capabilities,
    generate,
    health,
    history,
    media,
    providers,
    settings,
    tasks,
    voices,
)

api_router = APIRouter()

# -- system / meta ----------------------------------------------------------
api_router.include_router(health.router)
api_router.include_router(capabilities.router)

# -- catalog ----------------------------------------------------------------
api_router.include_router(providers.router)

# -- generation -------------------------------------------------------------
api_router.include_router(generate.router)
api_router.include_router(tasks.router)

# -- data -------------------------------------------------------------------
api_router.include_router(history.router)
api_router.include_router(audio.router)
api_router.include_router(media.router)
api_router.include_router(voices.router)
api_router.include_router(settings.router)

__all__ = ["api_router"]