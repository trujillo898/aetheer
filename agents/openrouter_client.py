"""Async OpenRouter client for Aetheer v3.0.

Thin wrapper over OpenRouter's OpenAI-compatible chat completions endpoint
(`https://openrouter.ai/api/v1/chat/completions`) with the features Aetheer
needs and nothing Agent-S's sync OpenAI-SDK wrapper gave us:

    - async/await (concurrent agent calls for context-orchestrator fan-out)
    - exponential backoff on 429 / 5xx / connection errors
    - usage + cost accounting per call (cents-accurate from OpenRouter's
      `usage.cost` field, falling back to model-table pricing)
    - explicit, pinned JSON error shape so `model_router` can decide fallback

Design notes:
    * NO streaming here; Fase 6 (WebApp) adds streaming via a separate method.
    * NO tool-calling plumbing here; Fase 2 (AetheerCognitiveAgent) plugs the
      MCP tool schema in via `extra_body={"tools": ...}` — we accept **kwargs
      passthrough for that.
    * NO global state; the client is cheap to instantiate per request.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("aetheer.openrouter")

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 4
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenRouterError(RuntimeError):
    """Raised for unrecoverable OpenRouter failures (after retries)."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(slots=True)
class ChatUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0  # 0.0 if unknown; router can fall back to pricing table


@dataclass(slots=True)
class ChatResult:
    model: str
    content: str
    usage: ChatUsage
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class OpenRouterClient:
    """Async OpenRouter chat client. Reusable across requests; close with `aclose()`."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_referer: str | None = None,
        app_title: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError(
                "OpenRouter API key missing. Set OPENROUTER_API_KEY or pass api_key=..."
            )
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries

        headers = {"Authorization": f"Bearer {self.api_key}"}
        # OpenRouter rewards apps that identify themselves in leaderboards/stats;
        # both headers are optional but harmless.
        if http_referer:
            headers["HTTP-Referer"] = http_referer
        if app_title:
            headers["X-Title"] = app_title

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OpenRouterClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> ChatResult:
        """POST /chat/completions with retry. Returns a `ChatResult`.

        Raises `OpenRouterError` after `max_retries` attempts on retryable
        conditions, or immediately on non-retryable 4xx.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            # Ask OpenRouter to compute `usage.cost` server-side so we don't
            # need to maintain a price table on the hot path.
            "usage": {"include": True},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        if tools is not None:
            payload["tools"] = tools
        if extra_body:
            payload.update(extra_body)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning(
                    "openrouter transport error (attempt %d/%d): %s",
                    attempt + 1, self.max_retries + 1, exc,
                )
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(_backoff(attempt))
                continue

            if response.status_code in RETRYABLE_STATUS:
                last_error = OpenRouterError(
                    f"retryable {response.status_code}",
                    status=response.status_code,
                    body=_safe_body(response),
                )
                logger.warning(
                    "openrouter %d (attempt %d/%d)",
                    response.status_code, attempt + 1, self.max_retries + 1,
                )
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(_backoff(attempt, response.headers.get("Retry-After")))
                continue

            if response.status_code >= 400:
                raise OpenRouterError(
                    f"openrouter {response.status_code}: {_safe_body(response)}",
                    status=response.status_code,
                    body=_safe_body(response),
                )

            return _parse_result(response.json(), model)

        assert last_error is not None
        if isinstance(last_error, OpenRouterError):
            raise last_error
        raise OpenRouterError(f"openrouter unreachable: {last_error}") from last_error


def _parse_result(body: dict[str, Any], requested_model: str) -> ChatResult:
    choices = body.get("choices") or []
    if not choices:
        raise OpenRouterError("openrouter response has no choices", body=body)
    choice = choices[0]
    content = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason")

    usage_src = body.get("usage") or {}
    usage = ChatUsage(
        prompt_tokens=int(usage_src.get("prompt_tokens") or 0),
        completion_tokens=int(usage_src.get("completion_tokens") or 0),
        total_tokens=int(usage_src.get("total_tokens") or 0),
        cost_usd=float(usage_src.get("cost") or 0.0),
    )
    return ChatResult(
        model=body.get("model", requested_model),
        content=content,
        usage=usage,
        finish_reason=finish,
        raw=body,
    )


def _backoff(attempt: int, retry_after: str | None = None) -> float:
    """Exponential backoff with jitter, honoring Retry-After when present."""
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    base = min(30.0, (2 ** attempt) * 0.5)
    return base + random.uniform(0, base * 0.25)


def _safe_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:500]
