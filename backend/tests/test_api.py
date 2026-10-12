"""API contract tests: every route, every documented behaviour.

Everything runs against the in-process ASGI app with the offline ``mock``
providers, so the suite needs no credentials and no network.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.asyncio

MOCK_LLM = {"provider": "mock", "model": "mock-echo-v1"}
MOCK_TTS = {"provider": "mock", "voice": "mock-neutral", "format": "wav"}


def generate_body(**overrides) -> dict:
    body = {
        "text": "请用一句话介绍杭州西湖。",
        "llm": dict(MOCK_LLM),
        "tts": dict(MOCK_TTS),
        "options": {"auto_save": True, "fallback_enabled": True},
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
class TestSystemRoutes:
    async def test_health(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["database"] == "ok"
        assert payload["providers"]["llm"] > 0
        assert payload["providers"]["tts"] > 0

    async def test_api_prefixed_health(self, client):
        response = await client.get("/api/v1/health")
        assert response.status_code == 200

    async def test_request_id_header_is_returned(self, client):
        response = await client.get("/health")
        assert response.headers["X-Request-ID"]

    async def test_client_request_id_is_echoed(self, client):
        response = await client.get("/health", headers={"X-Request-ID": "my-trace-id-123"})
        assert response.headers["X-Request-ID"] == "my-trace-id-123"

    async def test_root_index(self, client):
        response = await client.get("/")
        assert response.status_code == 200
        assert response.json()["api_prefix"] == "/api/v1"

    async def test_error_code_catalog(self, client):
        response = await client.get("/api/v1/meta/error-codes")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] > 20
        codes = {item["code"]: item["http_status"] for item in payload["codes"]}
        assert codes["TTS_AUTH_FAILED"] == 401
        assert codes["RATE_LIMITED"] == 429
        assert codes["TASK_NOT_FINISHED"] == 409

    async def test_runtime_config_has_no_secrets(self, client):
        response = await client.get("/api/v1/meta/config")
        assert response.status_code == 200
        text = json.dumps(response.json())
        assert "api_key" not in text
        assert response.json()["max_text_length"] == 5000

    async def test_metrics(self, client):
        response = await client.get("/api/v1/meta/metrics")
        assert response.status_code == 200
        assert "uptime_seconds" in response.json()


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------
class TestOpenApi:
    async def test_schema_is_served(self, client):
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"]
        assert schema["info"]["version"] == "1.0.0"

    async def test_all_documented_paths_exist(self, client):
        schema = (await client.get("/openapi.json")).json()
        paths = set(schema["paths"])
        for expected in (
            "/api/v1/providers",
            "/api/v1/providers/validate",
            "/api/v1/generate",
            "/api/v1/tasks",
            "/api/v1/tasks/{task_id}",
            "/api/v1/tasks/{task_id}/result",
            "/api/v1/history",
            "/api/v1/history/{record_id}",
            "/api/v1/audio/{record_id}",
            "/api/v1/settings",
            "/health",
        ):
            assert expected in paths, f"{expected} missing from OpenAPI"

    async def test_error_envelope_is_documented(self, client):
        schema = (await client.get("/openapi.json")).json()
        assert "ErrorResponse" in schema["components"]["schemas"]
        # The 401 of the generate route must reference the shared envelope.
        generate_401 = schema["paths"]["/api/v1/generate"]["post"]["responses"]["401"]
        assert generate_401["content"]["application/json"]["schema"]["$ref"].endswith("ErrorResponse")

    async def test_swagger_ui_is_available(self, client):
        response = await client.get("/docs")
        assert response.status_code == 200
        assert "swagger" in response.text.lower()

    async def test_secret_fields_are_not_exposed_as_plain_strings(self, client):
        schema = (await client.get("/openapi.json")).json()
        # SecretStr is documented as a password-format string, never as an example.
        text = json.dumps(schema)
        assert "sk-proj-" not in text
        llm_config = schema["components"]["schemas"].get("LLMConfigSchema")
        assert llm_config is not None
        assert "api_key" in llm_config["properties"]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class TestProviderRoutes:
    async def test_list_providers(self, client):
        response = await client.get("/api/v1/providers")
        assert response.status_code == 200
        payload = response.json()
        assert {item["id"] for item in payload["llm"]} >= {"openai", "anthropic", "qwen"}
        assert {item["id"] for item in payload["tts"]} >= {"openai", "elevenlabs", "azure", "volcengine"}

    async def test_provider_metadata_is_complete(self, client):
        payload = (await client.get("/api/v1/providers")).json()
        for provider in payload["tts"]:
            assert provider["voices"], provider["id"]
            assert provider["formats"], provider["id"]
            assert provider["default_voice"], provider["id"]

    async def test_validate_mock_llm_succeeds(self, client):
        response = await client.post(
            "/api/v1/providers/validate",
            json={"kind": "llm", "provider": "mock", "api_key": None, "model": "mock-echo-v1"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["valid"] is True
        assert payload["kind"] == "llm"

    async def test_validate_mock_tts_succeeds(self, client):
        response = await client.post(
            "/api/v1/providers/validate",
            json={"kind": "tts", "provider": "mock", "voice": "mock-neutral"},
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True

    async def test_validate_missing_key_returns_invalid_not_error(self, client):
        response = await client.post(
            "/api/v1/providers/validate",
            json={"kind": "llm", "provider": "openai", "model": "gpt-4o-mini"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["valid"] is False
        assert payload["detail"]["code"] == "MISSING_CREDENTIAL"

    async def test_validate_unknown_provider_is_400(self, client):
        response = await client.post(
            "/api/v1/providers/validate",
            json={"kind": "llm", "provider": "nope", "api_key": "sk-abcdefghijklmnop"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "LLM_UNSUPPORTED_PROVIDER"

    async def test_validate_rejects_javascript_base_url(self, client):
        response = await client.post(
            "/api/v1/providers/validate",
            json={
                "kind": "llm",
                "provider": "openai",
                "api_key": "sk-abcdefghijklmnop",
                "base_url": "javascript:alert(1)",
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_provider_listing_contains_no_keys(self, client):
        text = (await client.get("/api/v1/providers")).text
        assert "api_key" not in text or "api_key_hint" in text
        assert "sk-proj-" not in text


# ---------------------------------------------------------------------------
# Synchronous generation
# ---------------------------------------------------------------------------
class TestGenerateRoute:
    async def test_full_pipeline_returns_audio(self, client):
        response = await client.post("/api/v1/generate", json=generate_body())
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["record_id"] > 0
        assert payload["reply_text"]
        assert payload["input_text"] == "请用一句话介绍杭州西湖。"
        assert payload["audio_url"] == f"/api/v1/audio/{payload['record_id']}"
        assert payload["audio_bytes"] > 44
        assert payload["audio_format"] == "wav"
        assert payload["duration_ms"] and payload["duration_ms"] > 0
        assert payload["degraded"] is False
        assert payload["llm"]["provider"] == "mock"
        assert payload["tts"]["provider"] == "mock"
        assert payload["total_latency_ms"] >= 0

    async def test_audio_is_playable_and_well_formed(self, client):
        created = (await client.post("/api/v1/generate", json=generate_body())).json()
        audio = await client.get(created["audio_url"])
        assert audio.status_code == 200
        assert audio.headers["content-type"].startswith("audio/")
        assert audio.content[:4] == b"RIFF"
        assert len(audio.content) == created["audio_bytes"]

    async def test_audio_download_sets_attachment(self, client):
        created = (await client.post("/api/v1/generate", json=generate_body())).json()
        response = await client.get(f"{created['audio_url']}?download=1")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]

    async def test_audio_range_request(self, client):
        created = (await client.post("/api/v1/generate", json=generate_body())).json()
        response = await client.get(created["audio_url"], headers={"Range": "bytes=0-99"})
        assert response.status_code == 206
        assert len(response.content) == 100
        assert response.headers["content-range"].startswith("bytes 0-99/")

    async def test_base64_option(self, client):
        body = generate_body()
        body["options"]["include_audio_base64"] = True
        payload = (await client.post("/api/v1/generate", json=body)).json()
        assert payload["audio_base64"]
        assert len(payload["audio_base64"]) > 50

    async def test_empty_text_is_rejected(self, client):
        response = await client.post("/api/v1/generate", json=generate_body(text="   "))
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_text_over_limit_is_rejected(self, client):
        response = await client.post("/api/v1/generate", json=generate_body(text="字" * 5001))
        assert response.status_code == 422
        assert "5000" in json.dumps(response.json()["detail"], ensure_ascii=False)

    async def test_unsupported_provider_is_400(self, client):
        body = generate_body(llm={"provider": "not-real", "api_key": "sk-abcdefghijklmnop"})
        response = await client.post("/api/v1/generate", json=body)
        assert response.status_code == 400
        assert response.json()["code"] == "LLM_UNSUPPORTED_PROVIDER"

    async def test_unsupported_format_is_400(self, client):
        body = generate_body(tts={"provider": "mock", "voice": "mock-neutral", "format": "midi"})
        response = await client.post("/api/v1/generate", json=body)
        assert response.status_code == 400
        assert response.json()["code"] == "TTS_UNSUPPORTED_FORMAT"

    async def test_missing_credential_is_400(self, client):
        body = generate_body(llm={"provider": "openai", "model": "gpt-4o-mini"})
        response = await client.post("/api/v1/generate", json=body)
        assert response.status_code == 400
        assert response.json()["code"] == "MISSING_CREDENTIAL"

    async def test_degraded_mode_survives_llm_failure(self, client):
        """An unreachable base_url must still yield audio, flagged as degraded."""
        body = generate_body(
            llm={
                "provider": "openai",
                "api_key": "sk-test-key-abcdefghijklmnop",
                "model": "gpt-4o-mini",
                "base_url": "http://127.0.0.1:9",  # closed port -> connection refused
            }
        )
        response = await client.post("/api/v1/generate", json=body)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["degraded"] is True
        assert payload["degraded_reason"]
        assert payload["audio_bytes"] > 0
        assert payload["reply_text"]

    async def test_fallback_disabled_surfaces_the_error(self, client):
        body = generate_body(
            llm={
                "provider": "openai",
                "api_key": "sk-test-key-abcdefghijklmnop",
                "base_url": "http://127.0.0.1:9",
            }
        )
        body["options"]["fallback_enabled"] = False
        response = await client.post("/api/v1/generate", json=body)
        assert response.status_code in (502, 504)
        assert response.json()["code"].startswith("LLM_")

    async def test_auto_save_false_keeps_history_clean(self, client):
        body = generate_body()
        body["options"]["auto_save"] = False
        created = (await client.post("/api/v1/generate", json=body)).json()
        # Audio is still retrievable...
        assert (await client.get(created["audio_url"])).status_code == 200
        # ...but nothing was written to history.
        history = (await client.get("/api/v1/history")).json()
        assert history["total"] == 0

    async def test_credentials_never_appear_in_the_response(self, client):
        body = generate_body(
            llm={"provider": "mock", "api_key": "sk-proj-SUPERSECRETVALUE1234567890"},
            tts={"provider": "mock", "api_key": "0123456789abcdef0123456789abcdef", "format": "wav"},
        )
        response = await client.post("/api/v1/generate", json=body)
        assert "SUPERSECRETVALUE" not in response.text
        assert "0123456789abcdef0123456789abcdef" not in response.text


# ---------------------------------------------------------------------------
# Async tasks
# ---------------------------------------------------------------------------
class TestTaskRoutes:
    async def _submit_and_wait(self, client, body, timeout: float = 20.0) -> dict:
        submitted = await client.post("/api/v1/tasks", json=body)
        assert submitted.status_code == 202, submitted.text
        task_id = submitted.json()["task_id"]

        deadline = asyncio.get_event_loop().time() + timeout
        status_payload = {}
        while asyncio.get_event_loop().time() < deadline:
            status_response = await client.get(f"/api/v1/tasks/{task_id}")
            assert status_response.status_code == 200
            status_payload = status_response.json()
            if status_payload["status"] in {"succeeded", "failed", "canceled"}:
                break
            await asyncio.sleep(0.05)
        return {"task_id": task_id, **status_payload}

    async def test_submit_returns_202_with_task_id(self, client):
        response = await client.post("/api/v1/tasks", json=generate_body())
        assert response.status_code == 202
        payload = response.json()
        assert payload["task_id"]
        assert payload["status"] in {"pending", "running"}
        assert payload["stage"] in {"queued", "llm", "tts", "persist", "done"}

    async def test_task_completes_and_yields_a_result(self, client):
        final = await self._submit_and_wait(client, generate_body())
        assert final["status"] == "succeeded", final
        assert final["stage"] == "done"
        assert final["progress"] == 100
        assert final["record_id"]

        result = await client.get(f"/api/v1/tasks/{final['task_id']}/result")
        assert result.status_code == 200
        payload = result.json()
        assert payload["reply_text"]
        assert payload["audio_bytes"] > 0
        assert (await client.get(payload["audio_url"])).status_code == 200

    async def test_progress_is_monotonic_and_labelled(self, client):
        submitted = (await client.post("/api/v1/tasks", json=generate_body())).json()
        task_id = submitted["task_id"]
        seen_progress = [submitted["progress"]]
        seen_stages = {submitted["stage"]}
        for _ in range(400):
            payload = (await client.get(f"/api/v1/tasks/{task_id}")).json()
            seen_progress.append(payload["progress"])
            seen_stages.add(payload["stage"])
            if payload["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.02)
        assert seen_progress == sorted(seen_progress), seen_progress
        assert "llm" in seen_stages or "tts" in seen_stages
        assert seen_progress[-1] == 100

    async def test_unknown_task_is_404(self, client):
        response = await client.get("/api/v1/tasks/deadbeef")
        assert response.status_code == 404
        assert response.json()["code"] == "TASK_NOT_FOUND"

    async def test_result_before_completion_is_409(self, client):
        # A task that will take a moment: mock TTS renders real samples.
        body = generate_body(text="字" * 400)
        submitted = (await client.post("/api/v1/tasks", json=body)).json()
        response = await client.get(f"/api/v1/tasks/{submitted['task_id']}/result")
        # Either it already finished (fast machine) or we get the documented 409.
        if response.status_code == 409:
            assert response.json()["code"] == "TASK_NOT_FINISHED"
        else:
            assert response.status_code == 200
        await self._submit_and_wait(client, generate_body(text="短"))

    async def test_failed_task_reports_the_error_envelope(self, client):
        body = generate_body(
            llm={"provider": "openai", "api_key": "sk-test-key-abcdefghijklmnop", "base_url": "http://127.0.0.1:9"}
        )
        body["options"]["fallback_enabled"] = False
        final = await self._submit_and_wait(client, body)
        assert final["status"] == "failed"
        assert final["error"]["code"].startswith("LLM_")
        assert final["error"]["message"]

        response = await client.get(f"/api/v1/tasks/{final['task_id']}/result")
        assert response.status_code == 500

    async def test_task_listing(self, client):
        await self._submit_and_wait(client, generate_body())
        response = await client.get("/api/v1/tasks")
        assert response.status_code == 200
        assert len(response.json()["items"]) >= 1

    async def test_cancel_finished_task_is_conflict(self, client):
        final = await self._submit_and_wait(client, generate_body())
        response = await client.delete(f"/api/v1/tasks/{final['task_id']}")
        assert response.status_code == 409
        assert response.json()["code"] == "TASK_ALREADY_FINISHED"

    async def test_task_payload_snapshot_has_no_key(self, client):
        """The durable task row must not contain the submitted credential."""
        body = generate_body(
            llm={"provider": "mock", "api_key": "sk-proj-NEVERPERSIST1234567890"},
            tts={"provider": "mock", "api_key": "NEVERPERSISTTOKENVALUE", "format": "wav"},
        )
        final = await self._submit_and_wait(client, body)

        from app.db.base import get_sessionmaker
        from app.db.models import TaskModel

        async with get_sessionmaker()() as session:
            row = await session.get(TaskModel, final["task_id"])
        dumped = json.dumps(row.request_payload, ensure_ascii=False)
        assert "NEVERPERSIST" not in dumped
        assert "api_key" not in dumped


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class TestHistoryRoutes:
    async def _create(self, client, text="测试文本") -> dict:
        response = await client.post("/api/v1/generate", json=generate_body(text=text))
        assert response.status_code == 200, response.text
        return response.json()

    async def test_list_is_paginated(self, client):
        for index in range(3):
            await self._create(client, f"第 {index} 条文本")
        response = await client.get("/api/v1/history?page=1&page_size=2")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 3
        assert payload["pages"] == 2
        assert len(payload["items"]) == 2
        assert payload["page_size"] == 2

    async def test_newest_first(self, client):
        await self._create(client, "最早的一条")
        await self._create(client, "最新的一条")
        items = (await client.get("/api/v1/history")).json()["items"]
        assert "最新" in items[0]["input_text"]

    async def test_search_filters(self, client):
        await self._create(client, "关于杭州西湖的介绍")
        await self._create(client, "关于北京故宫的介绍")
        payload = (await client.get("/api/v1/history?q=西湖")).json()
        assert payload["total"] == 1
        assert "西湖" in payload["items"][0]["input_text"]

    async def test_detail_endpoint(self, client):
        created = await self._create(client)
        response = await client.get(f"/api/v1/history/{created['record_id']}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == created["record_id"]
        assert payload["reply_text"]
        assert payload["audio_url"]

    async def test_missing_detail_is_404(self, client):
        response = await client.get("/api/v1/history/999999")
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    async def test_delete_single(self, client):
        created = await self._create(client)
        response = await client.delete(f"/api/v1/history/{created['record_id']}")
        assert response.status_code == 204
        assert (await client.get("/api/v1/history")).json()["total"] == 0
        # The audio file is gone too.
        assert (await client.get(created["audio_url"])).status_code == 404

    async def test_delete_batch(self, client):
        first = await self._create(client, "第一条")
        second = await self._create(client, "第二条")
        response = await client.request(
            "DELETE",
            "/api/v1/history",
            json={"ids": [first["record_id"], second["record_id"]]},
        )
        assert response.status_code == 200
        assert response.json()["deleted"] == 2

    async def test_delete_all(self, client):
        await self._create(client)
        await self._create(client)
        response = await client.request("DELETE", "/api/v1/history", json={"all": True})
        assert response.status_code == 200
        assert response.json()["deleted"] == 2

    async def test_delete_requires_a_selector(self, client):
        response = await client.request("DELETE", "/api/v1/history", json={})
        assert response.status_code == 422

    async def test_stats(self, client):
        await self._create(client)
        payload = (await client.get("/api/v1/history/stats")).json()
        assert payload["total"] == 1
        assert payload["succeeded"] == 1
        assert payload["audio_bytes"] > 0

    async def test_csv_export(self, client):
        await self._create(client)
        response = await client.get("/api/v1/history/export")
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]
        assert "reply_text" in response.text

    async def test_page_size_is_capped(self, client):
        response = await client.get("/api/v1/history?page_size=1000")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class TestSettingsRoutes:
    async def test_defaults_are_returned(self, client):
        response = await client.get("/api/v1/settings")
        assert response.status_code == 200
        payload = response.json()
        assert payload["default_llm_provider"]
        assert payload["default_tts_provider"]
        assert payload["auto_save_credentials"] is False
        assert payload["request_timeout_seconds"] == 120

    async def test_partial_update(self, client):
        response = await client.put(
            "/api/v1/settings",
            json={"default_tts_voice": "nova", "default_speed": 1.35, "auto_save_history": False},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["default_tts_voice"] == "nova"
        assert payload["default_speed"] == 1.35
        assert payload["auto_save_history"] is False

        reloaded = (await client.get("/api/v1/settings")).json()
        assert reloaded["default_tts_voice"] == "nova"

    async def test_settings_never_contain_credentials(self, client):
        text = (await client.get("/api/v1/settings")).text
        assert "api_key" not in text

    async def test_submitting_a_key_is_rejected(self, client):
        """The schema forbids extra fields, so a key cannot be persisted."""
        response = await client.put("/api/v1/settings", json={"api_key": "sk-proj-abcdef123456"})
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_out_of_range_speed_is_rejected(self, client):
        response = await client.put("/api/v1/settings", json={"default_speed": 99})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Error envelope & middleware
# ---------------------------------------------------------------------------
class TestErrorContract:
    async def test_404_uses_the_envelope(self, client):
        response = await client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        payload = response.json()
        assert set(payload) == {"code", "message", "detail", "request_id"}
        assert payload["code"] == "NOT_FOUND"

    async def test_405_uses_the_envelope(self, client):
        response = await client.delete("/api/v1/providers")
        assert response.status_code == 405
        assert response.json()["code"] == "METHOD_NOT_ALLOWED"

    async def test_validation_error_lists_fields(self, client):
        response = await client.post("/api/v1/generate", json={"text": "hi"})
        assert response.status_code == 422
        payload = response.json()
        assert payload["code"] == "VALIDATION_ERROR"
        fields = {item["field"] for item in payload["detail"]["errors"]}
        assert any("llm" in field for field in fields)
        assert any("tts" in field for field in fields)

    async def test_malformed_json_is_handled(self, client):
        response = await client.post(
            "/api/v1/generate",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_oversized_body_is_rejected(self, client, settings):
        original = settings.max_request_bytes
        settings.max_request_bytes = 512
        try:
            response = await client.post(
                "/api/v1/generate",
                content=b"x" * 4096,
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 413
            assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
        finally:
            settings.max_request_bytes = original

    async def test_every_error_carries_a_request_id(self, client):
        response = await client.get("/api/v1/nope")
        assert response.json()["request_id"]
        assert response.headers["X-Request-ID"] == response.json()["request_id"]


class TestRateLimiting:
    async def test_limiter_blocks_after_budget_is_spent(self, client, settings):
        settings.rate_limit_enabled = True
        settings.rate_limit_requests = 3
        settings.rate_limit_window_seconds = 60
        from app.core.rate_limit import limiter

        await limiter.store.reset()
        try:
            statuses = []
            for _ in range(6):
                statuses.append((await client.get("/api/v1/providers")).status_code)
            assert 429 in statuses, statuses
            blocked = [s for s in statuses if s == 429]
            assert blocked
        finally:
            settings.rate_limit_enabled = False
            await limiter.store.reset()

    async def test_rate_limited_response_uses_the_envelope(self, client, settings):
        settings.rate_limit_enabled = True
        settings.rate_limit_generate_requests = 1
        settings.rate_limit_generate_window_seconds = 60
        from app.core.rate_limit import limiter

        await limiter.store.reset()
        try:
            first = await client.post("/api/v1/generate", json=generate_body())
            assert first.status_code == 200
            second = await client.post("/api/v1/generate", json=generate_body())
            assert second.status_code == 429
            payload = second.json()
            assert payload["code"] == "RATE_LIMITED"
            assert payload["detail"]["retry_after"] > 0
        finally:
            settings.rate_limit_enabled = False
            await limiter.store.reset()