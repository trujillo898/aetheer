from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from agents.cognitive_agent import AetheerCognitiveAgent, CognitiveDeps, SPECIALIST_AGENTS
from agents.model_router import AetheerModelRouter
from agents.openrouter_client import OpenRouterClient
from agents.prompts.loader import default_loader
from agents.schemas import CognitiveQuery
from services.cost_monitor import BudgetConfig, CostMonitor


@dataclass
class _FakeMcp:
    health: dict[str, Any] = field(default_factory=lambda: {"operating_mode": "ONLINE"})

    async def get_system_health(self) -> dict[str, Any]:
        return self.health

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {}


def _chat_json(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "gen",
        "model": "test",
        "choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70, "cost": 0.0001},
    }


def _classify(body: dict[str, Any]) -> str:
    user_msgs = [m for m in body.get("messages", []) if m.get("role") == "user"]
    if not user_msgs:
        return "unknown"
    content = user_msgs[0]["content"]
    if "Evaluate the assembled bundle" in content:
        return "governor"
    if "Approved bundle:" in content:
        return "synthesis"
    for name in SPECIALIST_AGENTS:
        if f'"agent": "{name}"' in content:
            return name
    return "unknown"


def _query() -> CognitiveQuery:
    return CognitiveQuery(
        query_text="analisis",
        query_intent="full_analysis",
        instruments=["DXY"],
        timeframes=["H1"],
        requested_by="user",
        trace_id="trace-d011-1",
    )


@pytest.fixture
def _cost(tmp_path: Path) -> CostMonitor:
    return CostMonitor(
        db_path=tmp_path / "cost_d011.db",
        config=BudgetConfig(daily_cap_usd=10.0),
    )


@pytest.mark.asyncio
async def test_d011_offline_when_tv_health_offline(_cost: CostMonitor) -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": "should not call llm"})

    mcp = _FakeMcp(health={"operating_mode": "OFFLINE"})
    client = OpenRouterClient(api_key="sk-test", transport=httpx.MockTransport(handler), max_retries=0)
    deps = CognitiveDeps(
        client=client,
        router=AetheerModelRouter(),
        cost_monitor=_cost,
        mcp=mcp,
        prompt_loader=default_loader(),
    )
    try:
        resp = await AetheerCognitiveAgent(deps).cognitive_analysis(_query())
    finally:
        await client.aclose()

    assert resp.operating_mode == "OFFLINE"
    assert resp.approved is False
    assert resp.synthesis_text is None
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_d011_quality_floor_forces_offline(_cost: CostMonitor) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kind = _classify(body)
        seen.append(kind)
        if kind in SPECIALIST_AGENTS:
            payload = {
                "agent": kind,
                "agent_version": "2.0.0",
                "execution_meta": {"operating_mode": "ONLINE", "data_quality": "low"},
                "causal_chains": [],
                "payload": {"meta": {"source": "fallback_provider"}},
            }
            return httpx.Response(200, json=_chat_json(payload))
        # governor/synthesis should not be reached below quality floor
        return httpx.Response(500, json={"error": "unexpected call"})

    mcp = _FakeMcp()
    client = OpenRouterClient(api_key="sk-test", transport=httpx.MockTransport(handler), max_retries=0)
    deps = CognitiveDeps(
        client=client,
        router=AetheerModelRouter(),
        cost_monitor=_cost,
        mcp=mcp,
        prompt_loader=default_loader(),
    )
    try:
        resp = await AetheerCognitiveAgent(deps).cognitive_analysis(_query())
    finally:
        await client.aclose()

    assert resp.operating_mode == "OFFLINE"
    assert resp.approved is False
    assert resp.quality.global_score < 0.60
    assert "governor" not in seen
    assert "synthesis" not in seen


@pytest.mark.asyncio
async def test_d011_online_path_when_health_and_quality_ok(_cost: CostMonitor) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kind = _classify(body)
        if kind in SPECIALIST_AGENTS:
            payload = {
                "agent": kind,
                "agent_version": "2.0.0",
                "execution_meta": {"operating_mode": "ONLINE", "data_quality": "high"},
                "causal_chains": [
                    {
                        "cause": "c",
                        "effect": "e",
                        "invalid_condition": "i",
                        "confidence": 0.7,
                        "timeframe": "H1",
                        "supporting_evidence": [],
                        "contradicting_evidence": [],
                    }
                ],
                "payload": {"meta": {"source": "tradingview"}},
            }
            return httpx.Response(200, json=_chat_json(payload))
        if kind == "governor":
            return httpx.Response(
                200,
                json=_chat_json(
                    {
                        "approved": True,
                        "operating_mode": "ONLINE",
                        "contradictions": [],
                        "rejection_reason": None,
                    }
                ),
            )
        if kind == "synthesis":
            return httpx.Response(200, json=_chat_json({"text": "unused"} | {}))
        # synthesis expects plain text content, not JSON object shape
        return httpx.Response(200, json={
            "id": "gen",
            "model": "test",
            "choices": [{"message": {"role": "assistant", "content": "Analisis online"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40, "cost": 0.0001},
        })

    mcp = _FakeMcp()
    client = OpenRouterClient(api_key="sk-test", transport=httpx.MockTransport(handler), max_retries=0)
    deps = CognitiveDeps(
        client=client,
        router=AetheerModelRouter(),
        cost_monitor=_cost,
        mcp=mcp,
        prompt_loader=default_loader(),
    )
    try:
        resp = await AetheerCognitiveAgent(deps).cognitive_analysis(_query())
    finally:
        await client.aclose()

    assert resp.approved is True
    assert resp.operating_mode == "ONLINE"
    assert resp.synthesis_text is not None
