"""Model selection must follow the presence of a reference clip, both ways.

Upgrade: clip present + plain model configured -> clone model.
Downgrade: no clip + clone model configured -> plain model.

Without the downgrade one clone poisons every later run: the model choice
persists in settings/localStorage, and a clone model without a clip fails
with "reference audio required" — what happened when switching from
声音克隆 back to 文字转语音.
"""

from __future__ import annotations

import pytest

from app.adapters.base import TTSRequest
from app.adapters.registry import registry
from app.core.errors import AppError, ErrorCode
from app.schemas.generation import GenerateRequest
from app.services.generation import GenerationService
from app.services.media import media_store

pytestmark = pytest.mark.asyncio

CLONE_MODEL = "mimo-v2.5-tts-voiceclone"
PLAIN_MODEL = "mimo-v2.5-tts"


def make_clip() -> str:
    record = media_store.save(
        data=b"RIFF-fake-reference",
        filename="ref.wav",
        extension="wav",
        kind="audio",
        content_type="audio/wav",
        duration_ms=5000,
        sample_rate=24000,
        channels=1,
    )
    return record.media_id


def build_request(*, model: str, reference_media_id: str | None) -> GenerateRequest:
    return GenerateRequest(
        text="模型选择测试文本",
        llm={"provider": "mock"},
        tts={
            "provider": "mimo",
            "api_key": "sk-test-abcdefghijklmnop",
            "model": model,
            "voice": "冰糖",
            "format": "wav",
        },
        options={
            "auto_save": False,
            "reuse_reply_text": "模型选择测试文本",
            "reference_media_id": reference_media_id,
        },
    )


class Recorder:
    """Capture the model id the adapter was called with, then fail fast.

    The spy raises a *degradable* upstream error, so generation completes with
    a placeholder instead of propagating — the assertions below check which
    model reached the adapter, which is the behaviour under test.
    """

    def __init__(self) -> None:
        self.adapter = registry.tts("mimo")
        self._original = self.adapter.synthesize
        self.seen: dict[str, str] = {}

    async def spy(self, tts_request: TTSRequest):  # noqa: ANN202
        self.seen["model"] = tts_request.model
        raise AppError(ErrorCode.TTS_UPSTREAM_ERROR, "spy", retryable=False)

    def __enter__(self) -> "Recorder":
        self.adapter.synthesize = self.spy  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        self.adapter.synthesize = self._original  # type: ignore[method-assign]


class TestAdapterModelPair:
    def test_mimo_declares_both_models(self):
        adapter = registry.tts("mimo")
        assert adapter.clone_model == CLONE_MODEL
        assert adapter.plain_model == PLAIN_MODEL

    def test_adapters_without_a_clone_split_declare_neither(self):
        for provider in ("openai", "mock"):
            adapter = registry.tts(provider)
            assert adapter.clone_model == ""
            assert adapter.plain_model == ""


class TestModelFollowsTheClip:
    async def test_clone_model_without_clip_falls_back_to_plain(self, session):
        """The poisoning case: clone model configured, no clip this request.

        The request must reach the adapter with the PLAIN model — before the
        fix it failed inside the adapter with TTS_REFERENCE_UNSUPPORTED
        ("reference audio required"), which is what broke plain synthesis
        after any clone run.
        """
        with Recorder() as recorder:
            await GenerationService().generate(
                session, build_request(model=CLONE_MODEL, reference_media_id=None)
            )
        assert recorder.seen.get("model") == PLAIN_MODEL

    async def test_clip_with_plain_model_upgrades_to_clone(self, session):
        """The original upgrade path must keep working."""
        clip = make_clip()
        with Recorder() as recorder:
            await GenerationService().generate(
                session, build_request(model=PLAIN_MODEL, reference_media_id=clip)
            )
        assert recorder.seen.get("model") == CLONE_MODEL

    async def test_plain_model_without_clip_is_untouched(self, session):
        """No clip, plain model: nothing to rewrite."""
        with Recorder() as recorder:
            await GenerationService().generate(
                session, build_request(model=PLAIN_MODEL, reference_media_id=None)
            )
        assert recorder.seen.get("model") == PLAIN_MODEL

    async def test_clone_model_with_clip_stays_clone(self, session):
        """Clip present and clone model already selected: no rewrite needed."""
        clip = make_clip()
        with Recorder() as recorder:
            await GenerationService().generate(
                session, build_request(model=CLONE_MODEL, reference_media_id=clip)
            )
        assert recorder.seen.get("model") == CLONE_MODEL

    async def test_missing_clip_never_raises_reference_unsupported(self, session):
        """Guard against regressing to the poisoning behaviour."""
        with Recorder() as recorder:
            response = await GenerationService().generate(
                session, build_request(model=CLONE_MODEL, reference_media_id=None)
            )
        # Degraded (the spy failed), but for an upstream reason.
        assert response.degraded is True
        assert "REFERENCE" not in (response.degraded_reason or "")
        assert recorder.seen.get("model") == PLAIN_MODEL
