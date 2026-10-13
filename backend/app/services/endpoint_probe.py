"""Endpoint discovery for OpenAI-compatible gateways.

Third-party gateways rarely implement exactly the OpenAI route set. When a
synthesis call 404s, the user needs to know *which* path their endpoint
actually exposes — guessing one at a time through the UI is painful.

:func:`probe_speech_paths` tries the common candidates against the configured
base URL and reports which ones respond, so the UI can suggest the right
「合成接口路径」 instead of leaving the user to trial-and-error.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.adapters.base import get_http_client
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Routes commonly used by OpenAI-compatible and vendor-specific TTS APIs.
#: Ordered most-likely-first so the report reads usefully.
CANDIDATE_PATHS: tuple[str, ...] = (
    "/audio/speech",
    "/audio/speech/create",
    "/tts",
    "/text-to-speech",
    "/speech",
    "/audio/tts",
    "/v1/audio/speech",
    "/v1/tts",
    "/synthesize",
    "/api/tts",
    "/audio/generations",
)

#: Status codes that prove *something* is routed at that path.
#: 400/422 mean the handler exists but rejected our minimal body — that is a
#: strong signal the path is right and only the payload needs adjusting.
_ALIVE_STATUSES = {200, 201, 400, 401, 403, 405, 415, 422, 429}


@dataclass(slots=True)
class PathProbe:
    path: str
    status: int | None
    alive: bool
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "status": self.status,
            "alive": self.alive,
            "note": self.note,
        }


async def _probe_one(base_url: str, path: str, api_key: str, timeout: float) -> PathProbe:
    """Probe one candidate path with a minimal, harmless request."""
    url = f"{base_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # Tiny body: if the path exists but the payload is wrong we get 400/422,
    # which still proves the route is there.
    body = {"model": "probe", "input": "hi", "voice": "alloy"}

    client = get_http_client()
    try:
        response = await client.post(url, headers=headers, json=body, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return PathProbe(path=path, status=None, alive=False, note=f"不可达：{type(exc).__name__}")

    status = response.status_code
    if status in _ALIVE_STATUSES:
        if status in (200, 201):
            note = "可用（返回了响应）"
        elif status in (401, 403):
            note = "路径存在，但密钥被拒绝"
        elif status in (400, 422, 415):
            note = "路径存在，但请求体格式不符（可能需要其他字段）"
        elif status == 405:
            note = "路径存在，但不接受 POST"
        else:
            note = "路径存在（被限流）"
        return PathProbe(path=path, status=status, alive=True, note=note)

    if status == 404:
        return PathProbe(path=path, status=status, alive=False, note="不存在")

    return PathProbe(path=path, status=status, alive=False, note=f"HTTP {status}")


async def probe_speech_paths(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 10.0,
    max_concurrency: int = 4,
) -> list[PathProbe]:
    """Probe every candidate route concurrently and return the findings.

    Concurrency is capped so a gateway with aggressive rate limiting is not
    hammered by a dozen simultaneous requests.
    """
    if not base_url.strip():
        return []

    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def guarded(path: str) -> PathProbe:
        async with semaphore:
            return await _probe_one(base_url, path, api_key, timeout)

    results = await asyncio.gather(*(guarded(path) for path in CANDIDATE_PATHS))
    alive = [item for item in results if item.alive]
    logger.info(
        "speech path probe base_url=%s probed=%s alive=%s",
        base_url, len(results), [item.path for item in alive],
    )
    return list(results)


def best_path(results: list[PathProbe]) -> str | None:
    """Pick the most likely correct path from a probe run."""
    for item in results:
        if item.alive and item.status in (200, 201):
            return item.path
    for item in results:
        if item.alive:
            return item.path
    return None
