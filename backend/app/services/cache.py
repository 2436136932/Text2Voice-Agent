"""Synthesis cache: deduplicate identical TTS requests.

The key is a sha256 over every input that changes the output bytes —
provider, model, voice, speed, format, reference clip, and the exact text —
so a hit is bit-for-bit equivalent to a fresh vendor call. Audio files live
under ``settings.cache_dir`` (a ``cache/`` subtree of the audio store) and
the index lives in SQLite.

Eviction is deliberately two-dimensional, because peers learned both lessons:

* **Count cap** (LRU by ``last_hit_at``) — bounded disk use per entry count.
* **TTL** — entries nobody touches for ``tts_cache_ttl_days`` days go even if
  the count cap is not reached (staleness of vendor voices matters too: a
  re-cloned voice may sound different after the vendor updates the model).

The cache is transparent to callers: a hit returns the same
:class:`~app.adapters.base.TTSResult` shape a synthesis would, with
``latency_ms=0`` and ``from_cache=True``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import TTSCacheEntry

logger = get_logger(__name__)

#: Segment separator must not appear in any segment; ``\\x00`` is safe because
#: none of the inputs can contain a NUL byte (they are validated strings or
#: hex/base64 payloads).
_SEP = "\x00"


def cache_key(
    *,
    provider: str,
    model: str,
    voice: str,
    speed: float,
    audio_format: str,
    text: str,
    reference_bytes: bytes | None = None,
) -> str:
    """Build the deterministic cache key for one synthesis request.

    The reference clip is part of the key *by its content*, so re-cloning
    with a different sample invalidates correctly without extra bookkeeping.
    """
    digest = hashlib.sha256()
    for part in (provider, model, voice, f"{speed:.3f}", audio_format, text):
        digest.update(part.encode("utf-8"))
        digest.update(_SEP.encode("utf-8"))
    if reference_bytes:
        digest.update(reference_bytes)
    else:
        digest.update(b"no-reference")
    return digest.hexdigest()


def cache_file_name(key: str, audio_format: str) -> str:
    """File name inside the cache subtree for one key."""
    extension = {"mp3": "mp3", "wav": "wav", "pcm": "pcm", "opus": "opus"}.get(
        (audio_format or "mp3").lower(), "bin"
    )
    return f"{key}.{extension}"


class TTSCache:
    """SQLite-indexed, disk-backed cache of synthesis results."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        path = self._root or settings.cache_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve(self, relative_path: str) -> Path:
        """Resolve a cache path, refusing anything outside the cache root."""
        candidate = (self.root / relative_path).resolve()
        root = self.root.resolve()
        if root != candidate and root not in candidate.parents:
            raise ValueError(f"cache path escapes root: {relative_path}")
        return candidate

    # -- core API -----------------------------------------------------------

    async def get(
        self, session: AsyncSession, key: str
    ) -> tuple[bytes, TTSCacheEntry] | None:
        """Return ``(audio_bytes, entry)`` on a hit, else ``None``.

        A hit bumps ``hit_count``/``last_hit_at`` (LRU bookkeeping) in the
        same transaction the caller commits.
        """
        if settings.tts_cache_max_entries <= 0:
            return None
        row = (
            await session.execute(
                select(TTSCacheEntry).where(TTSCacheEntry.cache_key == key)
            )
        ).scalar_one_or_none()
        if row is None:
            return None

        path = self._resolve(row.audio_path)
        if not path.is_file():
            # Index points at a missing file (manual wipe, crash between
            # writes): treat as a miss and drop the stale row.
            await session.delete(row)
            logger.info("cache entry file missing, dropped key=%s", key[:12])
            return None

        audio = path.read_bytes()
        await session.execute(
            update(TTSCacheEntry)
            .where(TTSCacheEntry.id == row.id)
            .values(hit_count=row.hit_count + 1, last_hit_at=func.now())
        )
        return audio, row

    async def put(
        self,
        session: AsyncSession,
        *,
        key: str,
        provider: str,
        model: str,
        voice: str,
        audio_format: str,
        content_type: str,
        audio: bytes,
        duration_ms: int | None,
    ) -> None:
        """Store one synthesis result (file + index row) and enforce caps.

        Best-effort by design: a cache write failure must never fail the
        generation that produced the audio.
        """
        if settings.tts_cache_max_entries <= 0:
            return
        try:
            relative = cache_file_name(key, audio_format)
            target = self._resolve(relative)
            target.write_bytes(audio)
            session.add(
                TTSCacheEntry(
                    cache_key=key,
                    provider=provider,
                    model=model[:128],
                    voice=voice[:128],
                    audio_format=audio_format[:16],
                    audio_path=relative,
                    audio_bytes=len(audio),
                    duration_ms=duration_ms,
                    content_type=content_type[:64],
                )
            )
            logger.info(
                "cache stored provider=%s key=%s bytes=%s",
                provider, key[:12], len(audio),
            )
        except Exception:  # noqa: BLE001 — cache must not break generation
            logger.exception("cache write failed (ignored) key=%s", key[:12])

    async def _enforce_caps(self, session: AsyncSession) -> None:
        """Evict by TTL first, then LRU down to the count cap.

        Runs against *committed* rows from the retention loop — doing it
        inside ``put()`` would see the not-yet-flushed insert and undercount.
        """
        ttl_days = settings.tts_cache_ttl_days
        if ttl_days > 0:
            await session.execute(
                delete(TTSCacheEntry).where(
                    TTSCacheEntry.last_hit_at
                    < func.datetime("now", f"-{int(ttl_days)} days")
                )
            )
        cap = settings.tts_cache_max_entries
        count = (
            await session.execute(select(func.count(TTSCacheEntry.id)))
        ).scalar_one()
        if count <= cap:
            return
        # Oldest-touch first; delete their files after the caller commits via
        # :meth:`sweep_orphan_files` (kept simple: file removal happens here
        # on a best-effort basis for the rows we are deleting).
        victims = (
            await session.execute(
                select(TTSCacheEntry)
                # id as the final tiebreaker: entries written in the same
                # clock tick share last_hit_at, and the eviction must still
                # be deterministic (oldest insert goes first).
                .order_by(TTSCacheEntry.last_hit_at.asc(), TTSCacheEntry.id.asc())
                .limit(count - cap)
            )
        ).scalars().all()
        for entry in victims:
            await session.delete(entry)
            try:
                path = self._resolve(entry.audio_path)
                if path.is_file():
                    path.unlink()
            except (OSError, ValueError):
                logger.warning("cache file unlink failed path=%s", entry.audio_path)
        logger.info("cache evicted entries=%s cap=%s", len(victims), cap)

    # -- maintenance ----------------------------------------------------------

    async def sweep_orphan_files(self, session: AsyncSession) -> int:
        """Delete cache files with no index row (crash between the two writes).

        Runs in the daily retention loop; returns the number removed.
        """
        known = set(
            (
                await session.execute(select(TTSCacheEntry.audio_path))
            ).scalars().all()
        )
        removed = 0
        for path in self.root.iterdir():
            if not path.is_file():
                continue
            if path.name not in known:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    logger.warning("orphan cache unlink failed path=%s", path.name)
        if removed:
            logger.info("cache orphan sweep removed=%s", removed)
        return removed

    async def clear(self, session: AsyncSession) -> int:
        """Wipe the whole cache (index + files). Returns entries removed."""
        rows = (await session.execute(select(TTSCacheEntry))).scalars().all()
        for entry in rows:
            await session.delete(entry)
        removed = 0
        for path in self.root.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    logger.warning("cache clear unlink failed path=%s", path.name)
        logger.info("cache cleared entries=%s files=%s", len(rows), removed)
        return len(rows)


tts_cache = TTSCache()
