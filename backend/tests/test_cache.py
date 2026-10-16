"""Synthesis cache: key stability, hit/miss semantics, eviction and orphans."""

from __future__ import annotations

from sqlalchemy import select

from app.adapters.base import TTSRequest, TTSResult
from app.adapters.registry import registry
from app.core.config import settings
from app.db.models import TTSCacheEntry
from app.services.cache import TTSCache, cache_key, cache_file_name
from app.schemas.generation import GenerateRequest
from app.services.generation import GenerationService


class TestCacheKey:
    def test_same_inputs_same_key(self):
        a = cache_key(provider="mimo", model="m", voice="v", speed=1.0, audio_format="mp3", text="hi")
        b = cache_key(provider="mimo", model="m", voice="v", speed=1.0, audio_format="mp3", text="hi")
        assert a == b
        assert len(a) == 64  # sha256 hex

    def test_any_change_changes_key(self):
        base = dict(provider="mimo", model="m", voice="v", speed=1.0, audio_format="mp3", text="hi")
        variants = [
            {**base, "text": "hi "},
            {**base, "voice": "other"},
            {**base, "speed": 1.25},
            {**base, "audio_format": "wav"},
            {**base, "provider": "openai"},
            {**base, "reference_bytes": b"clip"},
        ]
        keys = {cache_key(**base)}
        for variant in variants:
            keys.add(cache_key(**variant))
        assert len(keys) == len(variants) + 1

    def test_reference_content_changes_key(self):
        a = cache_key(provider="p", model="m", voice="v", speed=1.0, audio_format="wav",
                      text="t", reference_bytes=b"one")
        b = cache_key(provider="p", model="m", voice="v", speed=1.0, audio_format="wav",
                      text="t", reference_bytes=b"two")
        assert a != b


class TestCacheRoundTrip:
    async def test_put_then_get_returns_same_bytes(self, session, tmp_path):
        cache = TTSCache(root=tmp_path / "cache")
        key = "k" * 64
        await cache.put(
            session, key=key, provider="mock", model="m", voice="v",
            audio_format="wav", content_type="audio/wav", audio=b"RIFF-test",
            duration_ms=100,
        )
        await session.commit()

        hit = await cache.get(session, key)
        assert hit is not None
        audio, entry = hit
        assert audio == b"RIFF-test"
        assert entry.provider == "mock"

    async def test_miss_returns_none(self, session, tmp_path):
        cache = TTSCache(root=tmp_path / "cache")
        assert await cache.get(session, "z" * 64) is None

    async def test_missing_file_drops_row(self, session, tmp_path):
        """Index row pointing at a deleted file must not serve a hit."""
        cache = TTSCache(root=tmp_path / "cache")
        key = "f" * 64
        await cache.put(
            session, key=key, provider="mock", model="m", voice="v",
            audio_format="wav", content_type="audio/wav", audio=b"data",
            duration_ms=None,
        )
        await session.commit()
        # Simulate a manual wipe of the file.
        (tmp_path / "cache" / cache_file_name(key, "wav")).unlink()
        assert await cache.get(session, key) is None
        await session.commit()  # the drop is a delete: flush it
        # The stale row is dropped, not left to poison later lookups.
        remaining = (await session.execute(select(TTSCacheEntry))).scalars().all()
        assert remaining == []

    async def test_count_cap_evicts_lru(self, session, tmp_path, monkeypatch):
        """Enforcement runs against committed rows (retention loop style)."""
        monkeypatch.setattr(settings, "tts_cache_max_entries", 2)
        cache = TTSCache(root=tmp_path / "cache")
        for i, key in enumerate(("a" * 64, "b" * 64, "c" * 64)):
            await cache.put(
                session, key=key, provider="mock", model="m", voice="v",
                audio_format="wav", content_type="audio/wav", audio=f"data{i}".encode(),
                duration_ms=None,
            )
        await session.commit()

        await cache._enforce_caps(session)
        await session.commit()

        remaining = (await session.execute(
            select(TTSCacheEntry.cache_key).order_by(TTSCacheEntry.id)
        )).scalars().all()
        # Cap of 2 keeps the two most-recently-written entries.
        assert remaining == ["b" * 64, "c" * 64]

    async def test_disabled_cache_never_stores(self, session, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "tts_cache_max_entries", 0)
        cache = TTSCache(root=tmp_path / "cache")
        await cache.put(
            session, key="d" * 64, provider="mock", model="m", voice="v",
            audio_format="wav", content_type="audio/wav", audio=b"x",
            duration_ms=None,
        )
        await session.commit()
        assert await cache.get(session, "d" * 64) is None
        # put() must not even create the cache directory when disabled.
        assert not (tmp_path / "cache").exists() or list((tmp_path / "cache").iterdir()) == []

    async def test_sweep_orphan_files(self, session, tmp_path):
        cache = TTSCache(root=tmp_path / "cache")
        # A file with no index row (crash between file and index writes).
        cache.root  # ensure the directory exists
        (tmp_path / "cache" / "orphan.mp3").write_bytes(b"junk")
        removed = await cache.sweep_orphan_files(session)
        assert removed == 1
        assert list((tmp_path / "cache").iterdir()) == []


class TestGenerationIntegration:
    async def test_second_identical_generation_hits_cache(self, session, tmp_path, monkeypatch):
        """End-to-end: same request twice, vendor TTS called once.

        Uses the real ``tts_cache`` singleton (as the generation service does)
        pointed at a temp root via the settings monkeypatch, so this catches
        wiring mistakes the unit tests above cannot see.
        """
        from app.services import cache as cache_module

        test_root = tmp_path / "cache"
        monkeypatch.setattr(cache_module, "tts_cache", TTSCache(root=test_root))
        adapter = registry.tts("mock")
        calls = {"n": 0}
        original = adapter.synthesize

        async def counting_synth(request: TTSRequest) -> TTSResult:
            calls["n"] += 1
            return await original(request)

        monkeypatch.setattr(adapter, "synthesize", counting_synth)

        def make_request() -> GenerateRequest:
            return GenerateRequest(
                text="缓存验证文本",
                llm={"provider": "mock", "model": "mock-echo-v1"},
                tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
                options={"auto_save": False, "reuse_reply_text": "缓存验证文本"},
            )

        first = await GenerationService().generate(session, make_request())
        await session.commit()
        second = await GenerationService().generate(session, make_request())
        assert first.audio_bytes == second.audio_bytes
        assert calls["n"] == 1, "identical request must not re-call the vendor"
        assert second.llm.finish_reason == "reused"
