"""Security tests: log redaction, credential hygiene, fingerprinting.

These are the most important tests in the suite: the product promise is
"your API key is forwarded once and never stored". Each test below pins one
part of that promise.
"""

from __future__ import annotations

import json
import logging
import io

import pytest

from app.core.errors import AppError, ErrorCode
from app.core.logging import (
    RedactionFilter,
    mask_key,
    redact_mapping,
    redact_text,
)
from app.core.security import assert_credential, key_fingerprint, sanitize_filename
from app.schemas.generation import GenerateRequest

OPENAI_KEY = "sk-proj-AbCdEf0123456789AbCdEf0123456789AbCdEf01"
ANTHROPIC_KEY = "sk-ant-api03-AbCdEf0123456789AbCdEf0123456789"
HEX32_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
UUID_TOKEN = "3f2b1a4c-9d8e-4f7a-b6c5-1e2d3f4a5b6c"


class TestRedactText:
    def test_openai_key_is_masked(self):
        result = redact_text(f"calling with {OPENAI_KEY} now")
        assert OPENAI_KEY not in result
        assert "sk-***" in result

    def test_anthropic_key_is_masked(self):
        result = redact_text(f"Authorization: Bearer {ANTHROPIC_KEY}")
        assert ANTHROPIC_KEY not in result
        # The ``sk-*`` rule rewrites the key itself, leaving the scheme intact.
        assert "sk-***" in result

    def test_bare_bearer_token_is_masked(self):
        result = redact_text("Authorization: Bearer abcdef1234567890")
        assert "abcdef1234567890" not in result
        assert "***REDACTED***" in result

    def test_hex32_key_is_masked(self):
        # ElevenLabs / Azure subscription keys.
        result = redact_text(f"xi-api-key={HEX32_KEY}")
        assert HEX32_KEY not in result

    def test_bearer_header_is_masked(self):
        result = redact_text("Authorization: Bearer abcdef1234567890")
        assert "abcdef1234567890" not in result

    def test_query_parameter_is_masked(self):
        result = redact_text("GET /v1/tts?api_key=supersecretvalue&voice=alloy")
        assert "supersecretvalue" not in result
        assert "voice=alloy" in result

    def test_uuid_is_masked(self):
        result = redact_text(f"reqid={UUID_TOKEN}")
        assert UUID_TOKEN not in result

    def test_email_is_masked(self):
        result = redact_text("user@example.com failed")
        assert "user@example.com" not in result

    def test_long_blob_is_truncated(self):
        blob = "A" * 200
        result = redact_text(f"audio={blob}")
        assert blob not in result
        assert "[200 chars]" in result

    def test_plain_text_is_untouched(self):
        text = "你好，这是一段普通的文本，包含数字 12345 和标点。"
        assert redact_text(text) == text

    def test_custom_extra_keys(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "redact_extra_keys", ["my-custom-secret"])
        result = redact_text("value is my-custom-secret here")
        assert "my-custom-secret" not in result


class TestRedactMapping:
    def test_sensitive_keys_are_replaced(self):
        payload = {
            "api_key": OPENAI_KEY,
            "apiKey": OPENAI_KEY,
            "api-key": OPENAI_KEY,
            "Authorization": f"Bearer {OPENAI_KEY}",
            "password": "hunter2",
            "model": "gpt-4o-mini",
        }
        result = redact_mapping(payload)
        assert result["api_key"] == "***REDACTED***"
        assert result["apiKey"] == "***REDACTED***"
        assert result["api-key"] == "***REDACTED***"
        assert result["Authorization"] == "***REDACTED***"
        assert result["password"] == "***REDACTED***"
        assert result["model"] == "gpt-4o-mini"

    def test_nested_structures_are_walked(self):
        payload = {
            "llm": {"api_key": OPENAI_KEY, "model": "gpt-4o"},
            "tts": {"extra": {"token": HEX32_KEY, "voice": "alloy"}},
        }
        result = redact_mapping(payload)
        assert result["llm"]["api_key"] == "***REDACTED***"
        assert result["llm"]["model"] == "gpt-4o"
        assert result["tts"]["extra"]["token"] == "***REDACTED***"
        assert result["tts"]["extra"]["voice"] == "alloy"

    def test_values_inside_lists_are_redacted(self):
        result = redact_mapping({"headers": [f"Bearer {OPENAI_KEY}"]})
        assert OPENAI_KEY not in json.dumps(result)

    def test_deep_nesting_is_capped(self):
        payload: dict = {}
        node = payload
        for _ in range(20):
            node["child"] = {}
            node = node["child"]
        node["api_key"] = OPENAI_KEY
        # Must not raise, and must not leak.
        assert OPENAI_KEY not in json.dumps(redact_mapping(payload))


class TestRedactionFilter:
    def _capture(self, emit) -> str:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactionFilter())
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("t2v.test.redaction")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        emit(logger)
        return stream.getvalue()

    def test_message_is_redacted(self):
        output = self._capture(lambda log: log.info("key is %s", OPENAI_KEY))
        assert OPENAI_KEY not in output

    def test_extra_fields_are_redacted(self):
        def emit(log):
            log.info("call", extra={"api_key": OPENAI_KEY, "provider": "openai"})

        output = self._capture(emit)
        assert OPENAI_KEY not in output

    def test_traceback_is_redacted(self):
        def emit(log):
            try:
                raise RuntimeError(f"failed with {OPENAI_KEY}")
            except RuntimeError:
                log.exception("boom")

        output = self._capture(emit)
        assert OPENAI_KEY not in output
        assert "RuntimeError" in output

    def test_formatted_args_mapping_is_redacted(self):
        def emit(log):
            log.info("%(api_key)s", {"api_key": OPENAI_KEY})

        output = self._capture(emit)
        assert OPENAI_KEY not in output


class TestCredentialHelpers:
    def test_fingerprint_is_stable_and_irreversible(self):
        first = key_fingerprint(OPENAI_KEY)
        second = key_fingerprint(OPENAI_KEY)
        assert first == second
        assert OPENAI_KEY not in first
        assert first.startswith("sha256:")
        assert len(first) == len("sha256:") + 16

    def test_fingerprint_differs_per_key(self):
        assert key_fingerprint(OPENAI_KEY) != key_fingerprint(ANTHROPIC_KEY)

    def test_mask_key_keeps_only_edges(self):
        masked = mask_key(OPENAI_KEY)
        assert OPENAI_KEY not in masked
        assert masked.startswith("sk-p")
        assert "len=" in masked

    def test_assert_credential_rejects_empty(self):
        with pytest.raises(AppError) as exc:
            assert_credential("   ", kind="llm", provider="openai")
        assert exc.value.code is ErrorCode.MISSING_CREDENTIAL

    def test_assert_credential_rejects_too_short(self):
        with pytest.raises(AppError):
            assert_credential("abc", kind="tts", provider="openai")

    def test_assert_credential_returns_stripped_value(self):
        assert assert_credential(f"  {OPENAI_KEY}  ", kind="llm", provider="openai") == OPENAI_KEY

    def test_sanitize_filename_blocks_traversal(self):
        assert "/" not in sanitize_filename("../../etc/passwd")
        assert ".." not in sanitize_filename("../../etc/passwd")
        assert sanitize_filename("") == "audio"


class TestRequestDoesNotPersistCredentials:
    def _payload(self) -> dict:
        return {
            "text": "你好",
            "llm": {"provider": "openai", "api_key": OPENAI_KEY, "model": "gpt-4o-mini"},
            "tts": {"provider": "openai", "api_key": HEX32_KEY, "voice": "alloy"},
        }

    def test_safe_dump_contains_no_credential(self):
        request = GenerateRequest(**self._payload())
        dumped = json.dumps(request.safe_dump(), ensure_ascii=False)
        assert OPENAI_KEY not in dumped
        assert HEX32_KEY not in dumped
        assert "api_key" not in dumped

    def test_repr_does_not_leak_secret(self):
        request = GenerateRequest(**self._payload())
        assert OPENAI_KEY not in repr(request)
        assert HEX32_KEY not in repr(request)

    def test_key_fingerprints_are_not_raw(self):
        request = GenerateRequest(**self._payload())
        fingerprints = request.key_fingerprints()
        assert fingerprints["llm"] and fingerprints["llm"] != OPENAI_KEY
        assert fingerprints["llm"].startswith("sha256:")

    def test_secret_is_retrievable_only_explicitly(self):
        request = GenerateRequest(**self._payload())
        assert request.llm.api_key is not None
        assert request.llm.api_key.get_secret_value() == OPENAI_KEY
        assert str(request.llm.api_key) == "**********"