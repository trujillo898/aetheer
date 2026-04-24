"""Unit tests for agents.openrouter_client.

Uses httpx.MockTransport so no network calls are made. Covers:
    - happy path returns ChatResult with usage + cost
    - transient 5xx triggers retry and eventually succeeds
    - non-retryable 4xx raises immediately (no retry)
    - transport error retried
    - Retry-After header honored
    - missing API key raises on construction
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.openrouter_client import (
    OpenRouterClient,
    OpenRouterError,
    _backoff,
)


def _ok_body(cost: float = 0.0012) -> dict:
    return {
        "id": "gen-1",
        "model": "anthropic/claude-sonnet-4.5",
        "choices": [{
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 42,
            "completion_tokens": 7,
            "total_tokens": 49,
            "cost": cost,
        },
    }


@pytest.mark.asyncio
async def test_chat_completion_happy_path():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        assert body["model"] == "anthropic/claude-sonnet-4.5"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert body["usage"] == {"include": True}
        return httpx.Response(200, json=_ok_body())

    async with OpenRouterClient(
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.chat_completion(
            model="anthropic/claude-sonnet-4.5",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert calls["n"] == 1
    assert result.content == "hello"
    assert result.usage.prompt_tokens == 42
    assert result.usage.completion_tokens == 7
    assert result.usage.cost_usd == pytest.approx(0.0012)
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_retries_on_502_then_success(monkeypatch):
    # Kill real sleep to keep tests fast
    import agents.openrouter_client as mod
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    seq = iter([
        httpx.Response(502, text="bad gateway"),
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json=_ok_body()),
    ])

    def handler(_req: httpx.Request) -> httpx.Response:
        return next(seq)

    async with OpenRouterClient(
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
        max_retries=4,
    ) as client:
        result = await client.chat_completion(
            model="m", messages=[{"role": "user", "content": "x"}],
        )
    assert result.content == "hello"


@pytest.mark.asyncio
async def test_non_retryable_4xx_raises_immediately(monkeypatch):
    import agents.openrouter_client as mod
    calls = {"n": 0}
    async def _no_sleep(_):
        calls["n"] += 1
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    async with OpenRouterClient(
        api_key="sk-bad", transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OpenRouterError) as exc:
            await client.chat_completion(
                model="m", messages=[{"role": "user", "content": "x"}],
            )
    assert exc.value.status == 401
    assert calls["n"] == 0, "should not sleep before raising a non-retryable 4xx"


@pytest.mark.asyncio
async def test_transport_error_retried_then_gives_up(monkeypatch):
    import agents.openrouter_client as mod
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    async with OpenRouterClient(
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
        max_retries=2,
    ) as client:
        with pytest.raises(OpenRouterError):
            await client.chat_completion(
                model="m", messages=[{"role": "user", "content": "x"}],
            )


@pytest.mark.asyncio
async def test_retry_after_header_is_honored(monkeypatch):
    import agents.openrouter_client as mod
    observed: list[float] = []
    async def _spy_sleep(s):
        observed.append(s)
    monkeypatch.setattr(mod.asyncio, "sleep", _spy_sleep)

    seq = iter([
        httpx.Response(429, headers={"Retry-After": "2.5"}),
        httpx.Response(200, json=_ok_body()),
    ])

    def handler(_req: httpx.Request) -> httpx.Response:
        return next(seq)

    async with OpenRouterClient(
        api_key="sk-test", transport=httpx.MockTransport(handler),
    ) as client:
        await client.chat_completion(
            model="m", messages=[{"role": "user", "content": "x"}],
        )
    assert observed == [pytest.approx(2.5)]


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError):
        OpenRouterClient()


def test_backoff_bounds():
    # attempt 0 → 0.5..0.625
    val = _backoff(0)
    assert 0.5 <= val <= 0.625 + 1e-6
    # Retry-After wins when present and parseable
    assert _backoff(5, retry_after="3.0") == 3.0
