"""Google Gemini adapter (``generateContent``).

The API key travels as the ``x-goog-api-key`` header rather than a bearer
token, and the model name is part of the path.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

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


class GeminiAdapter(LLMAdapter):
    id = "gemini"
    name = "Google Gemini"
    kind = "llm"
    default_model = "gemini-2.0-flash"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    api_key_hint = "AIza..."
    docs_url = "https://ai.google.dev/api/generate-content"
    notes = "密钥以 AIza 开头；模型名会拼接进请求路径。"
    models = (
        {"id": "gemini-2.0-flash", "name": "gemini-2.0-flash"},
        {"id": "gemini-1.5-flash", "name": "gemini-1.5-flash"},
        {"id": "gemini-1.5-pro", "name": "gemini-1.5-pro"},
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

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        for item in request.history:
            if item.role == "system":
                continue
            role = "model" if item.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": item.content}]})
        contents.append({"role": "user", "parts": [{"text": request.prompt}]})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": max(0.0, min(2.0, request.temperature)),
                "maxOutputTokens": max(1, request.max_tokens),
            },
        }
        if request.system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": request.system_prompt}]}
        return payload

    async def complete(self, request: LLMRequest) -> LLMResult:
        api_key = assert_credential(request.api_key, kind="llm", provider=self.id)
        model = request.model or self.default_model
        url = (
            f"{self.resolve_base_url(request.base_url)}/models/"
            f"{quote(model, safe='')}:generateContent"
        )
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        payload = self.build_payload(request)

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
                "Gemini 返回了非预期的响应格式。",
                detail={"provider": self.id},
                retryable=True,
            )

        candidates = body.get("candidates") or []
        text = ""
        finish_reason = None
        if candidates:
            first = candidates[0] or {}
            finish_reason = first.get("finishReason")
            for part in (first.get("content") or {}).get("parts", []) or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text += part["text"]
        text = text.strip()

        if not text:
            blocked = (body.get("promptFeedback") or {}).get("blockReason")
            raise AppError(
                ErrorCode.LLM_EMPTY_RESPONSE,
                f"Gemini 未返回文本内容{'（内容被安全策略拦截）' if blocked else ''}，请调整输入后重试。",
                detail={"provider": self.id, "block_reason": blocked, "finish_reason": finish_reason},
                retryable=not blocked,
            )

        usage_raw = body.get("usageMetadata") or {}
        prompt_tokens = int(usage_raw.get("promptTokenCount") or 0)
        completion_tokens = int(usage_raw.get("candidatesTokenCount") or 0)
        logger.info(
            "llm completion ok provider=%s model=%s latency_ms=%s chars=%s",
            self.id, model, latency_ms, len(text),
        )
        return LLMResult(
            text=text,
            provider=self.id,
            model=model,
            latency_ms=latency_ms,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": int(usage_raw.get("totalTokenCount") or (prompt_tokens + completion_tokens)),
            },
            finish_reason=finish_reason,
        )