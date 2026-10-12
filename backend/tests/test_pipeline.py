"""Pipeline-level tests: retry, degradation, persistence and audio storage."""

from __future__ import annotations

import asyncio

import pytest

from app.adapters.base import LLMRequest, LLMResult, TTSRequest, TTSResult
from app.adapters.registry import registry
from app.core.errors import AppError, ErrorCode
from app.core.retry import RetryBudget, call_with_retry
from app.db.models import GenerationRecord, SettingModel
from app.schemas.generation import GenerateRequest
from app.services.audio import AudioStore, estimate_duration_ms
from app.services.generation import GenerationService

pytestmark = pytest.mark.asyncio


class TestRetryPolicy:
    async def test_retries_then_succeeds(self, monkeypatch):
        attempts: list[int] = []

        async def flaky(attempt: int) -> str:
            attempts.append(attempt)
            if attempt < 3:
                raise AppError(
                    ErrorCode.LLM_TIMEOUT, "timeout", retryable=True, http_status=504
                )
            return "ok"

        result = await call_with_retry(flaky, provider="p", kind="llm", max_retries=3)
        assert result == "ok"
        assert attempts == [1, 2, 3]

    async def test_non_retryable_fails_immediately(self):
        attempts: list[int] = []

        async def bad_key(attempt: int) -> str:
            attempts.append(attempt)
            raise AppError(ErrorCode.LLM_AUTH_FAILED, "bad key", retryable=False, http_status=401)

        with pytest.raises(AppError) as exc:
            await call_with_retry(bad_key, provider="p", kind="llm", max_retries=3)
        assert exc.value.code is ErrorCode.LLM_AUTH_FAILED
        assert attempts == [1], "auth failures must not be retried"

    async def test_exhausts_budget_then_raises(self):
        attempts: list[int] = []

        async def always_timeout(attempt: int) -> str:
            attempts.append(attempt)
            raise AppError(ErrorCode.LLM_TIMEOUT, "timeout", retryable=True, http_status=504)

        with pytest.raises(AppError):
            await call_with_retry(always_timeout, provider="p", kind="llm", max_retries=2)
        assert attempts == [1, 2, 3]

    async def test_retry_callback_is_invoked(self):
        seen: list[int] = []

        async def flaky(attempt: int) -> str:
            if attempt == 1:
                raise AppError(ErrorCode.LLM_RATE_LIMITED, "slow down", retryable=True, http_status=429)
            return "done"

        await call_with_retry(
            flaky, provider="p", kind="llm", max_retries=2, on_retry=lambda a, e: seen.append(a)
        )
        assert seen == [1]


class TestRetryBudget:
    def test_opens_after_threshold(self):
        budget = RetryBudget(failure_threshold=3, cooldown_seconds=60)
        assert budget.is_open("openai") is False
        for _ in range(3):
            budget.record_failure("openai")
        assert budget.is_open("openai") is True
        assert budget.retry_after("openai") > 0

    def test_success_resets(self):
        budget = RetryBudget(failure_threshold=2, cooldown_seconds=60)
        budget.record_failure("openai")
        budget.record_success("openai")
        budget.record_failure("openai")
        assert budget.is_open("openai") is False

    def test_cooldown_expires(self):
        budget = RetryBudget(failure_threshold=1, cooldown_seconds=0.01)
        budget.record_failure("openai")
        assert budget.is_open("openai") is True
        import time

        time.sleep(0.05)
        assert budget.is_open("openai") is False


class TestAudioStore:
    def test_save_and_resolve(self, tmp_audio_store: AudioStore):
        relative = tmp_audio_store.save(
            record_id=1, provider="openai", voice="alloy", audio_format="mp3", data=b"ID3data"
        )
        assert relative.endswith(".mp3")
        assert tmp_audio_store.path_for(relative).read_bytes() == b"ID3data"

    def test_filename_is_sanitized(self, tmp_audio_store: AudioStore):
        relative = tmp_audio_store.save(
            record_id=2,
            provider="../../evil",
            voice="a b/c",
            audio_format="wav",
            data=b"RIFF",
        )
        assert ".." not in relative
        assert "/" not in relative
        assert relative.startswith("2_")

    def test_traversal_is_refused(self, tmp_audio_store: AudioStore):
        assert tmp_audio_store.path_for("../../../etc/passwd") is None

    def test_missing_file_returns_none(self, tmp_audio_store: AudioStore):
        assert tmp_audio_store.path_for("nope.mp3") is None

    def test_delete(self, tmp_audio_store: AudioStore):
        relative = tmp_audio_store.save(
            record_id=3, provider="p", voice="v", audio_format="mp3", data=b"x"
        )
        assert tmp_audio_store.delete(relative) is True
        assert tmp_audio_store.delete(relative) is False

    def test_count_and_bytes(self, tmp_audio_store: AudioStore):
        tmp_audio_store.save(record_id=4, provider="p", voice="v", audio_format="wav", data=b"a" * 100)
        assert tmp_audio_store.count() == 1
        assert tmp_audio_store.total_bytes() == 100

    def test_unknown_format_uses_bin_extension(self, tmp_audio_store: AudioStore):
        relative = tmp_audio_store.save(
            record_id=5, provider="p", voice="v", audio_format="exotic", data=b"z"
        )
        assert relative.endswith(".bin")


class TestGenerationServiceDegradation:
    async def test_llm_failure_produces_degraded_audio(self, session):
        request = GenerateRequest(
            text="测试降级",
            llm={
                "provider": "openai",
                "api_key": "sk-test-key-abcdefghijklmnop",
                "base_url": "http://127.0.0.1:9",
            },
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True, "fallback_enabled": True},
        )
        response = await GenerationService().generate(session, request)
        assert response.degraded is True
        assert "LLM" in (response.degraded_reason or "")
        assert response.audio_bytes > 0
        assert response.llm.finish_reason == "degraded"

    async def test_tts_failure_produces_silent_placeholder(self, session):
        request = GenerateRequest(
            text="测试 TTS 降级",
            llm={"provider": "mock", "model": "mock-echo-v1"},
            tts={
                "provider": "openai",
                "api_key": "sk-test-key-abcdefghijklmnop",
                "base_url": "http://127.0.0.1:9",
                "format": "mp3",
            },
            options={"auto_save": True, "fallback_enabled": True},
        )
        response = await GenerationService().generate(session, request)
        assert response.degraded is True
        assert "TTS" in (response.degraded_reason or "")
        # The placeholder is a real, playable WAV.
        assert response.audio_format == "wav"
        assert response.audio_bytes > 44

    async def test_reuse_reply_text_skips_the_llm(self, session):
        request = GenerateRequest(
            text="原始输入",
            llm={"provider": "mock"},
            tts={"provider": "mock", "format": "wav"},
            options={"auto_save": True, "reuse_reply_text": "这是上一条回复，不需要重新生成。"},
        )
        response = await GenerationService().generate(session, request)
        assert response.reply_text == "这是上一条回复，不需要重新生成。"
        assert response.llm.finish_reason == "reused"
        assert response.llm.latency_ms == 0

    async def test_record_is_persisted_with_metadata(self, session):
        request = GenerateRequest(
            text="持久化测试",
            llm={"provider": "mock"},
            tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
            options={"auto_save": True},
        )
        response = await GenerationService().generate(session, request, request_id="trace-1")
        await session.commit()

        row = await session.get(GenerationRecord, response.record_id)
        assert row is not None
        assert row.input_text == "持久化测试"
        assert row.reply_text == response.reply_text
        assert row.status == "succeeded"
        assert row.request_id == "trace-1"
        assert row.duration_ms and row.duration_ms > 0
        assert row.audio_path.endswith(".wav")
        assert row.total_latency_ms is not None

    async def test_no_credential_is_written_to_the_row(self, session):
        secret = "sk-proj-THISKEYMUSTNOTBELANDINTHE123456"
        request = GenerateRequest(
            text="密钥检查",
            llm={"provider": "mock", "api_key": secret},
            tts={"provider": "mock", "format": "wav"},
            options={"auto_save": True},
        )
        response = await GenerationService().generate(session, request)
        await session.commit()

        row = await session.get(GenerationRecord, response.record_id)
        # Walk every column and assert the raw key is nowhere.
        for column in GenerationRecord.__table__.columns:
            value = getattr(row, column.name)
            assert secret not in str(value), f"secret leaked into {column.name}"
        assert row.key_fingerprint.startswith("sha256:")

    async def test_progress_callback_reports_all_stages(self, session):
        stages: list[str] = []
        request = GenerateRequest(
            text="进度测试",
            llm={"provider": "mock"},
            tts={"provider": "mock", "format": "wav"},
            options={"auto_save": False},
        )
        await GenerationService().generate(
            session, request, on_progress=lambda stage, pct, msg: stages.append(stage)
        )
        assert "llm" in stages
        assert "tts" in stages
        assert "persist" in stages
        assert stages[-1] == "done"

    async def test_unsaved_generation_is_still_downloadable(self, session, settings):
        request = GenerateRequest(
            text="不保存历史",
            llm={"provider": "mock"},
            tts={"provider": "mock", "format": "wav"},
            options={"auto_save": False},
        )
        response = await GenerationService().generate(session, request)
        matches = list(settings.audio_path.glob(f"{response.record_id}_*"))
        assert matches, "audio file must exist even without a history row"
        assert estimate_duration_ms(matches[0].read_bytes(), "wav") > 0


class TestSettingsPersistence:
    async def test_defaults_are_seeded(self, session):
        values = await SettingModel.load(session)
        assert values["auto_save_credentials"] is False
        assert values["request_timeout_seconds"] == 120

    async def test_unknown_keys_are_merged_not_lost(self, session):
        await SettingModel.save(session, {"custom_future_key": "kept"})
        await session.commit()
        values = await SettingModel.load(session)
        assert values["custom_future_key"] == "kept"
        assert values["default_tts_provider"] == "openai"


class TestConcurrency:
    async def test_parallel_generations_do_not_interfere(self, session):
        """Four concurrent pipelines must produce four distinct records."""
        from app.db.base import get_sessionmaker

        async def run(index: int) -> int:
            async with get_sessionmaker()() as own_session:
                request = GenerateRequest(
                    text=f"并发任务 {index}",
                    llm={"provider": "mock"},
                    tts={"provider": "mock", "voice": "mock-neutral", "format": "wav"},
                    options={"auto_save": True},
                )
                response = await GenerationService().generate(own_session, request)
                await own_session.commit()
                return response.record_id

        record_ids = await asyncio.gather(*(run(i) for i in range(4)))
        assert len(set(record_ids)) == 4
        assert all(record_id > 0 for record_id in record_ids)


class TestAdapterContractCompliance:
    """Every registered adapter must satisfy the abstract contract."""

    def test_all_llm_adapters_implement_the_interface(self):
        for adapter in registry.available_llm():
            assert isinstance(adapter, registry.llm(adapter.id).__class__)
            assert adapter.id
            assert adapter.name
            assert adapter.default_model
            assert hasattr(adapter, "complete")
            assert adapter.info().kind == "llm"

    def test_all_tts_adapters_implement_the_interface(self):
        for adapter in registry.available_tts():
            assert adapter.id
            assert adapter.name
            assert adapter.default_voice
            assert adapter.voices
            assert adapter.formats
            assert adapter.speed_min < adapter.speed_max
            assert adapter.max_chars > 0
            assert adapter.info().kind == "tts"

    def test_content_type_mapping_covers_declared_formats(self):
        """Every declared format must map to a real audio MIME type."""
        for adapter in registry.available_tts():
            for audio_format in adapter.formats:
                assert adapter.content_type_for(audio_format).startswith("audio/")

    async def test_mock_adapters_satisfy_the_contract_end_to_end(self):
        llm = registry.llm("mock")
        result: LLMResult = await llm.complete(LLMRequest(prompt="hi", api_key=""))
        assert isinstance(result, LLMResult)

        tts = registry.tts("mock")
        tts_result: TTSResult = await tts.synthesize(
            TTSRequest(text="hi", api_key="", voice=tts.default_voice, audio_format="wav")
        )
        assert isinstance(tts_result, TTSResult)
        assert tts_result.audio.startswith(b"RIFF")