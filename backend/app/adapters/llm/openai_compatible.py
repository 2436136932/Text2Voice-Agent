"""Reusable base class for every ``/chat/completions`` compatible vendor.

OpenAI, DeepSeek, Moonshot, Zhipu, Qwen (compatible-mode), Ollama, vLLM,
LocalAI, Together, Groq… all speak the same wire format. Subclasses only
declare metadata and a default base URL.
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


class OpenAICompatibleAdapter(LLMAdapter):
    """Chat completions against any OpenAI-compatible ``/chat/completions``."""

    default_base_url: str = settings.openai_base_url
    #: Header used for the credential (Ollama needs none).
    auth_header: str = "Authorization"
    #: Prefix applied to the key when building the auth header.
    auth_scheme: str = "Bearer "

    def resolve_base_url(self, base_url: str | None) -> str:
        return (base_url or self.default_base_url).rstrip("/")

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.id,
            name=self.name,
            kind="llm",
            requires_api_key=self.requires_api_key,
            api_key_hint=self.api_key_hint,
            docs_url=self.docs_url,
            notes=self.notes,
        )

    #: Overridable metadata -------------------------------------------------
    api_key_hint: str = "sk-..."
    docs_url: str = ""
    notes: str = ""
    requires_api_key: bool = True

    # -- helpers ------------------------------------------------------------
    def build_headers(self, api_key: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers[self.auth_header] = f"{self.auth_scheme}{api_key}"
        return headers

    def build_payload(self, request: LLMRequest, model: str) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        for item in request.history:
            messages.append({"role": item.role, "content": item.content})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": max(0.0, min(2.0, request.temperature)),
            "max_tokens": max(1, request.max_tokens),
            "stream": False,
        }
        # Vendor specific knobs are merged last so callers can override.
        payload.update({k: v for k, v in request.extra.items() if k not in {"api_key"}})
        return payload

    def parse_response(self, body: Any, model: str) -> LLMResult:  # noqa: ARG002
        if not isinstance(body, dict):
            raise AppError(
                ErrorCode.LLM_UPSTREAM_ERROR,
                f"{self.name} 返回了非预期的响应格式。",
                detail={"provider": self.id},
                retryable=True,
            )
        choices = body.get("choices") or []
        text = ""
        finish_reason = None
        if choices:
            first = choices[0] or {}
            message = first.get("message") or {}
            text = (message.get("content") or first.get("text") or "").strip()
            finish_reason = first.get("finish_reason")
        if not text:
            raise AppError(
                ErrorCode.LLM_EMPTY_RESPONSE,
                f"{self.name} 返回了空回复，请调整提示词或稍后重试。",
                detail={"provider": self.id},
                retryable=True,
            )
        usage_raw = body.get("usage") or {}
        usage = {
            key: int(value)
            for key, value in {
                "prompt_tokens": usage_raw.get("prompt_tokens", 0),
                "completion_tokens": usage_raw.get("completion_tokens", 0),
                "total_tokens": usage_raw.get("total_tokens", 0),
            }.items()
            if isinstance(value, (int, float))
        }
        return LLMResult(
            text=text,
            provider=self.id,
            model=body.get("model") or model,
            latency_ms=0,
            usage=usage,
            finish_reason=finish_reason,
        )

    # -- main entry point ---------------------------------------------------
    async def complete(self, request: LLMRequest) -> LLMResult:
        model = request.model or self.default_model
        api_key = request.api_key.strip() if request.api_key else ""
        if self.requires_api_key:
            api_key = assert_credential(api_key, kind="llm", provider=self.id)

        url = f"{self.resolve_base_url(request.base_url)}/chat/completions"
        payload = self.build_payload(request, model)

        started = time.perf_counter()
        body, _ = await request_json(
            method="POST",
            url=url,
            kind="llm",
            provider=self.id,
            headers=self.build_headers(api_key),
            json_body=payload,
            timeout=settings.llm_timeout_seconds,
        )
        result = self.parse_response(body, model)
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "llm completion ok provider=%s model=%s latency_ms=%s chars=%s",
            self.id, result.model, result.latency_ms, len(result.text),
        )
        return result