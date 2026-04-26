from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from agents.cognitive_agent import AetheerCognitiveAgent, CognitiveDeps, SPECIALIST_AGENTS
from agents.model_router import AetheerModelRouter
from agents.openrouter_client import OpenRouterClient
from agents.prompts.loader import default_loader
from agents.schemas import CognitiveQuery
from services.cost_monitor import CostMonitor


@dataclass
class _FakeMcp:
    async def get_system_health(self) -> dict[str, Any]:
        return {"operating_mode": "ONLINE"}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {}


def _kind(body: dict[str, Any]) -> str:
    msg = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
    if "Evaluate the assembled bundle" in msg:
        return "governor"
    if "Approved bundle:" in msg:
        return "synthesis"
    for name in SPECIALIST_AGENTS:
        if f'"agent": "{name}"' in msg:
            return name
    return "unknown"


def _chat(payload: Any) -> dict[str, Any]:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "id": "gen",
        "model": "m",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60, "cost": 0.0001},
    }


@pytest.mark.asyncio
async def test_full_analysis_online_with_mock_tv_unified(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        kind = _kind(body)
        if kind in SPECIALIST_AGENTS:
            return httpx.Response(
                200,
                json=_chat(
                    {
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
                ),
            )
        if kind == "governor":
            return httpx.Response(
                200,
                json=_chat(
                    {
                        "approved": True,
                        "operating_mode": "ONLINE",
                        "contradictions": [],
                        "rejection_reason": None,
                    }
                ),
            )
        if kind == "synthesis":
            return httpx.Response(200, json=_chat("Analisis completo online."))
        return httpx.Response(500, json={"error": "unexpected"})

    client = OpenRouterClient(
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    deps = CognitiveDeps(
        client=client,
        router=AetheerModelRouter(),
        cost_monitor=CostMonitor(db_path=tmp_path / "cost_e2e_online.db"),
        mcp=_FakeMcp(),
        prompt_loader=default_loader(),
    )
    try:
        query = CognitiveQuery(
            query_text="analisis completo",
            query_intent="full_analysis",
            instruments=["DXY", "EURUSD"],
            timeframes=["H1", "H4"],
            requested_by="user",
            trace_id="trace-e2e-online",
        )
        response = await AetheerCognitiveAgent(deps).cognitive_analysis(query)
    finally:
        await client.aclose()

    assert response.approved is True
    assert response.operating_mode == "ONLINE"
    assert response.synthesis_text is not None
