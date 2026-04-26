from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from agents.cognitive_agent import AetheerCognitiveAgent, CognitiveDeps
from agents.model_router import AetheerModelRouter
from agents.openrouter_client import OpenRouterClient
from agents.prompts.loader import default_loader
from agents.schemas import CognitiveQuery
from services.cost_monitor import CostMonitor


@dataclass
class _FakeMcpOffline:
    async def get_system_health(self) -> dict[str, Any]:
        return {"operating_mode": "OFFLINE"}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {}


@pytest.mark.asyncio
async def test_full_analysis_offline_with_mock_tv_unified(tmp_path: Path) -> None:
    llm_calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        llm_calls["n"] += 1
        return httpx.Response(500, json={"error": "should not be called"})

    client = OpenRouterClient(
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    deps = CognitiveDeps(
        client=client,
        router=AetheerModelRouter(),
        cost_monitor=CostMonitor(db_path=tmp_path / "cost_e2e_offline.db"),
        mcp=_FakeMcpOffline(),
        prompt_loader=default_loader(),
    )
    try:
        query = CognitiveQuery(
            query_text="analisis completo",
            query_intent="full_analysis",
            instruments=["DXY"],
            timeframes=["H1"],
            requested_by="user",
            trace_id="trace-e2e-offline",
        )
        response = await AetheerCognitiveAgent(deps).cognitive_analysis(query)
    finally:
        await client.aclose()

    assert response.approved is False
    assert response.operating_mode == "OFFLINE"
    assert response.synthesis_text is None
    assert llm_calls["n"] == 0
