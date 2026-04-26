from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from agents.schemas import CognitiveResponse, QualityBreakdown
from interfaces.sync_bus import SyncBus
from interfaces.web_app import AnalysisRuntime, create_web_app


class _FakeAgent:
    async def cognitive_analysis(self, query) -> CognitiveResponse:
        await asyncio.sleep(0.01)
        quality = QualityBreakdown(
            freshness=0.9,
            completeness=0.9,
            consistency=0.9,
            source_reliability=0.9,
            aetheer_validity=0.9,
        )
        return CognitiveResponse(
            approved=True,
            operating_mode="ONLINE",
            quality=quality,
            causal_chains=[],
            contradictions=[],
            rejection_reason=None,
            synthesis_text="Token uno. Token dos.",
            cost_usd=0.004,
            latency_ms=25,
            trace_id=query.trace_id,
        )


class _FakeCostMonitor:
    def spent_today_usd(self) -> float:
        return 1.2345

    def spent_by_agent_today(self) -> dict[str, float]:
        return {"macro": 0.5, "synthesis": 0.7345}


async def _health_provider() -> dict[str, Any]:
    return {"operating_mode": "ONLINE", "status": "online"}


def _wait_done(client: TestClient, trace_id: str, timeout_s: float = 2.0) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = client.get(f"/api/analysis/{trace_id}").json()
        if body["status"] in {"completed", "failed"}:
            return body
        time.sleep(0.02)
    raise TimeoutError(f"analysis {trace_id} did not complete")


def test_web_endpoints_happy_path() -> None:
    runtime = AnalysisRuntime(
        cognitive_agent=_FakeAgent(),
        cost_monitor=_FakeCostMonitor(),
        sync_bus=SyncBus(),
        health_provider=_health_provider,
    )
    app = create_web_app(runtime)

    with TestClient(app) as client:
        post = client.post(
            "/api/analyze",
            json={
                "query_text": "analisis dxy",
                "query_intent": "punctual",
                "requested_by": "webapp",
            },
        )
        assert post.status_code == 200
        trace_id = post.json()["trace_id"]
        assert trace_id

        state = _wait_done(client, trace_id)
        assert state["status"] == "completed"
        assert state["result"]["approved"] is True

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["operating_mode"] == "ONLINE"

        cost = client.get("/api/cost/today")
        assert cost.status_code == 200
        assert cost.json()["spent_today_usd"] == pytest.approx(1.2345)
        assert cost.json()["by_agent"]["macro"] == pytest.approx(0.5)

        stream = client.get(f"/api/analysis/{trace_id}/stream")
        assert stream.status_code == 200
        assert "event: token" in stream.text
        assert "event: done" in stream.text
