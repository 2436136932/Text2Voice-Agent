"""Anthropic Claude adapter (``/v1/messages``).

Anthropic does not expose an OpenAI-compatible endpoint, so this module
implements the wire format directly:

* auth header ``x-api-key`` plus the mandatory ``anthropic-version`` header;
* the system prompt is a top-level ``system`` field, not a message;
* ``max_tokens`` is required by the API;
* the reply lives in ``content[]`` blocks of type ``text``.
"""

from __future__ import annotations

import time
from typing import Any

from app.adapters.base import (
    AdapterInfo,
    LLMAdapter,
    LLMRequest,
    LLMResult,
    request_json,
)
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.security import assert_credential

logger = get_logger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicAdapter(LLMAdapter):
    id = "anthropic"
    name = "Anthropic Claude"
    kind = "llm"
    default_model = "claude-3-5-haiku-latest"
    default_base_url = "https://api.anthropic.com/v1"
    api_key_hint = "sk-ant-..."
    docs_url = "https://docs.anthropic.com/en/api/messages"
    notes = "原生 Messages API；密钥以 sk-ant- 开头。"
    models = (
        {"id": "claude-3-5-haiku-latest", "name": "Claude 3.5 Haiku (快速)"},
        {"id": "claude-3-5-sonnet-latest", "name": "Claude 3.5 Sonnet"},
        {"id": "claude-3-7-sonnet-latest", "name": "Claude 3.7 Sonnet"},
        {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5"},
    )

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id,
            name=self.name,
            kind="llm",
            api_key_hint=self.api_key_hint,
            docs_url=self.docs_url,
            notes=self.notes,
        )

    def resolve_base_url(self, base_url: str | None) -> str:
        return (base_url or self.default_base_url).rstrip("/")

    def build_payload(self, request: LLMRequest, model: str) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        for item in request.history:
            if item.role == "system":
                continue  # handled by the top-level ``system`` field
            messages.append({"role": item.role, "content": item.content})
        if not messages:
            messages.append({"role": "user", "content": request.prompt})
        else:
            messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max(1, request.max_tokens),
            "temperature": max(0.0, min(1.0, request.temperature)),
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt
        payload.update(request.extra)
        return payload

    async def complete(self, request: LLMRequest) -> LLMResult:
        api_key = assert_credential(request.api_key, kind="llm", provider=self.id)
        model = request.model or self.default_model
        url = f"{self.resolve_base_url(request.base_url)}/messages"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        payload = self.build_payload(request, model)

        started = time.perf_counter()
        body, _ = await request_json(
            method="POST",
            url=url,
            kind="llm",
            provider=self.id,
            headers=headers,
            json_body=payload,
            timeout=settings.llm_timeout_seconds,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        if not isinstance(body, dict):
            raise AppError(
                ErrorCode.LLM_UPSTREAM_ERROR,
                "Anthropic 返回了非预期的响应格式。",
                detail={"provider": self.id},
                retryable=True,
            )

        blocks = body.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if not text:
            raise AppError(
                ErrorCode.LLM_EMPTY_RESPONSE,
                "Claude 返回了空回复，请调整提示词后重试。",
                detail={"provider": self.id, "stop_reason": body.get("stop_reason")},
                retryable=True,
            )

        usage_raw = body.get("usage") or {}
        input_tokens = int(usage_raw.get("input_tokens") or 0)
        output_tokens = int(usage_raw.get("output_tokens") or 0)
        logger.info(
            "llm completion ok provider=%s model=%s latency_ms=%s chars=%s",
            self.id, model, latency_ms, len(text),
        )
        return LLMResult(
            text=text,
            provider=self.id,
            model=body.get("model") or model,
            latency_ms=latency_ms,
            usage={
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            finish_reason=body.get("stop_reason"),
        )