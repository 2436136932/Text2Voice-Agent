"""Tests for upstream model fetching.

Motivated by a real shortcoming: preset adapters ship a **hand-written** model
tuple, so the list shown in the UI goes stale as soon as a vendor adds or retires
a model. `POST /providers/models` must therefore work for *every* provider (not
just `custom`), authenticate the way that provider expects, and re-attach the
local Chinese annotations to whatever the upstream actually returns.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.model_catalog import (
    build_model_headers,
    local_model_notes,
    resolve_models_url,
)

pytestmark = pytest.mark.asyncio


class TestAuthSchemes:
    """Sending the wrong header yields a 401 that looks like a bad key."""

    def test_mimo_uses_api_key_header(self):
        headers = build_model_headers("SECRET", provider_id="mimo")
        assert headers["api-key"] == "SECRET"
        assert "Authorization" not in headers

    def test_openai_uses_bearer(self):
        headers = build_model_headers("SECRET", provider_id="openai")
        assert headers["Authorization"] == "Bearer SECRET"
        assert "api-key" not in headers

    def test_anthropic_uses_x_api_key_with_version(self):
        headers = build_model_headers("SECRET", provider_id="anthropic")
        assert headers["x-api-key"] == "SECRET"
        assert headers["anthropic-version"] == "2023-06-01"

    def test_custom_defaults_to_bearer(self):
        assert build_model_headers("SECRET", provider_id="custom")["Authorization"] == "Bearer SECRET"


class TestLocalAnnotations:
    def test_mimo_notes_are_available(self):
        notes = local_model_notes("mimo", "tts")
        assert notes.get("mimo-v2.5-tts") == "mimo-v2.5-tts (预置音色)"
        assert "voiceclone" in notes.get("mimo-v2.5-tts-voiceclone", "")

    def test_unknown_provider_returns_no_notes(self):
        assert local_model_notes("does-not-exist", "tts") == {}

    def test_url_and_headers_together(self):
        url, headers = resolve_models_url(
            "https://api.xiaomimimo.com/v1", "K", provider_id="mimo", kind="tts"
        )
        assert url == "https://api.xiaomimimo.com/v1/models"
        assert headers["api-key"] == "K"


class TestFetchModelsEndpoint:
    @pytest.fixture()
    def stub_upstream(self, monkeypatch):
        """Intercept the outbound GET and record it."""
        state: dict = {"models": [], "status": 200, "calls": []}

        class _Client:
            async def get(self, url, **kwargs):  # noqa: ANN001
                state["calls"].append({"url": url, "headers": kwargs.get("headers")})
                return httpx.Response(
                    state["status"],
                    json={"data": [{"id": m} for m in state["models"]]},
                    request=httpx.Request("GET", url),
                )

        monkeypatch.setattr("app.adapters.base.get_http_client", lambda: _Client())
        return state

    async def test_builtin_provider_uses_default_base_url(self, client, stub_upstream):
        stub_upstream["models"] = ["mimo-v2.5-tts", "mimo-v2.5-tts-voiceclone"]
        response = await client.post(
            "/api/v1/providers/models",
            json={"provider": "mimo", "api_key": "test-key-1234", "kind": "tts"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["source"] == "upstream"
        assert payload["base_url"] == "https://api.xiaomimimo.com/v1"
        assert stub_upstream["calls"][0]["url"].endswith("/v1/models")
        assert stub_upstream["calls"][0]["headers"]["api-key"] == "test-key-1234"

    async def test_local_notes_are_merged_onto_upstream_models(self, client, stub_upstream):
        stub_upstream["models"] = [
            "mimo-v2.5-tts",
            "mimo-v2.5-tts-voiceclone",
            "mimo-v2.6-unreleased",  # a model this build has never heard of
        ]
        response = await client.post(
            "/api/v1/providers/models",
            json={"provider": "mimo", "api_key": "test-key-1234", "kind": "tts"},
        )
        models = {m["id"]: m for m in response.json()["models"]}
        assert models["mimo-v2.5-tts"]["note"] == "mimo-v2.5-tts (预置音色)"
        # A brand-new upstream model must still be offered, just without a note.
        assert "mimo-v2.6-unreleased" in models
        assert models["mimo-v2.6-unreleased"]["note"] == ""
        assert models["mimo-v2.6-unreleased"]["source"] == "upstream"

    async def test_tts_capability_is_marked(self, client, stub_upstream):
        stub_upstream["models"] = ["mimo-v2.5-tts", "mimo-v2.5-pro"]
        response = await client.post(
            "/api/v1/providers/models",
            json={"provider": "mimo", "api_key": "test-key-1234", "kind": "tts"},
        )
        models = {m["id"]: m for m in response.json()["models"]}
        assert models["mimo-v2.5-tts"]["supports_tts"] is True
        assert models["mimo-v2.5-pro"]["supports_tts"] is False

    async def test_upstream_failure_is_reported_with_fallback_hint(
        self, client, stub_upstream
    ):
        """A provider without /models must fail loudly, not silently."""
        stub_upstream["status"] = 404
        response = await client.post(
            "/api/v1/providers/models",
            json={"provider": "anthropic", "api_key": "sk-ant-test1234", "kind": "llm"},
        )
        assert response.status_code in (404, 502)
        detail = response.json()["detail"]
        assert detail.get("suggestion"), "must tell the user what to do instead"

    async def test_custom_provider_requires_base_url(self, client):
        response = await client.post(
            "/api/v1/providers/models",
            json={"provider": "custom", "api_key": "test-key-1234", "kind": "tts"},
        )
        assert response.status_code == 400
        assert response.json()["detail"].get("suggestion")

    async def test_missing_key_is_rejected(self, client):
        response = await client.post(
            "/api/v1/providers/models",
            json={"provider": "mimo", "kind": "tts"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "MISSING_CREDENTIAL"

    async def test_unknown_provider_is_rejected(self, client):
        response = await client.post(
            "/api/v1/providers/models",
            json={"provider": "not-a-provider", "api_key": "test-key-1234", "kind": "tts"},
        )
        assert response.status_code == 400
        assert response.json()["detail"].get("suggestion")
