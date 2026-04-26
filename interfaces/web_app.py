"""FastAPI interface for Aetheer v3 (local-only, no auth in v3.0)."""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents.schemas import (
    CognitiveQuery,
    CognitiveResponse,
    QueryIntent,
    RequestedBy,
    Timeframe,
)
from interfaces.sync_bus import SyncBus
from interfaces.web_streaming import AnalysisStreamHub


class CognitiveAgentLike(Protocol):
    async def cognitive_analysis(self, query: CognitiveQuery) -> CognitiveResponse: ...


class CostMonitorLike(Protocol):
    def spent_today_usd(self) -> float: ...
    def spent_by_agent_today(self) -> dict[str, float]: ...


class AnalyzeRequest(BaseModel):
    query_text: str = Field(min_length=1)
    query_intent: QueryIntent = "full_analysis"
    instruments: list[str] = Field(default_factory=list)
    timeframes: list[Timeframe] = Field(default_factory=list)
    requested_by: RequestedBy = "webapp"
    trace_id: str | None = None


@dataclass(slots=True)
class AnalysisRecord:
    trace_id: str
    status: str
    query: CognitiveQuery
    response: CognitiveResponse | None
    error: str | None
    created_at: float
    updated_at: float


class AnalysisRuntime:
    """Shared runtime used by WebApp and Telegram handlers."""

    def __init__(
        self,
        *,
        cognitive_agent: CognitiveAgentLike,
        cost_monitor: CostMonitorLike,
        sync_bus: SyncBus | None = None,
        stream_hub: AnalysisStreamHub | None = None,
        health_provider: Any | None = None,
    ) -> None:
        self._agent = cognitive_agent
        self._cost_monitor = cost_monitor
        self._sync_bus = sync_bus or SyncBus()
        self._stream_hub = stream_hub or AnalysisStreamHub()
        self._health_provider = health_provider

        self._records: dict[str, AnalysisRecord] = {}
        self._done_events: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    @property
    def stream_hub(self) -> AnalysisStreamHub:
        return self._stream_hub

    @property
    def sync_bus(self) -> SyncBus:
        return self._sync_bus

    async def submit(self, req: AnalyzeRequest) -> str:
        trace_id = req.trace_id or f"{req.requested_by}-{uuid4().hex[:12]}"
        query = CognitiveQuery(
            query_text=req.query_text,
            query_intent=req.query_intent,
            instruments=req.instruments,
            timeframes=req.timeframes,
            requested_by=req.requested_by,
            trace_id=trace_id,
        )
        now = time.time()
        async with self._lock:
            if trace_id in self._records:
                raise ValueError(f"trace_id already exists: {trace_id}")
            self._records[trace_id] = AnalysisRecord(
                trace_id=trace_id,
                status="queued",
                query=query,
                response=None,
                error=None,
                created_at=now,
                updated_at=now,
            )
            self._done_events[trace_id] = asyncio.Event()

        await self._sync_bus.publish_analysis(trace_id, {"status": "queued"}, status="queued")
        task = asyncio.create_task(self._run(trace_id, query))
        self._tasks[trace_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(trace_id, None))
        return trace_id

    async def _run(self, trace_id: str, query: CognitiveQuery) -> None:
        try:
            await self._set_status(trace_id, status="running", error=None, response=None)
            await self._sync_bus.publish_analysis(
                trace_id, {"status": "running"}, status="running"
            )

            response = await self._agent.cognitive_analysis(query)
            await self._set_status(
                trace_id, status="completed", error=None, response=response
            )

            if response.synthesis_text:
                await self._stream_hub.publish_synthesis_text(trace_id, response.synthesis_text)
            await self._stream_hub.publish_done(trace_id)

            payload = response.model_dump()
            await self._sync_bus.publish_analysis(
                trace_id, payload, status="completed"
            )
            if response.operating_mode == "OFFLINE":
                await self._sync_bus.publish_operating_mode(
                    mode="OFFLINE",
                    source=query.requested_by,
                    trace_id=trace_id,
                    reason=response.rejection_reason,
                )
        except Exception as exc:
            msg = str(exc)
            await self._set_status(trace_id, status="failed", error=msg, response=None)
            await self._stream_hub.publish_error(trace_id, msg)
            await self._sync_bus.publish_analysis(
                trace_id, {"status": "failed", "error": msg}, status="failed"
            )
        finally:
            done = self._done_events.get(trace_id)
            if done is not None:
                done.set()

    async def _set_status(
        self,
        trace_id: str,
        *,
        status: str,
        error: str | None,
        response: CognitiveResponse | None,
    ) -> None:
        async with self._lock:
            rec = self._records[trace_id]
            rec.status = status
            rec.error = error
            if response is not None:
                rec.response = response
            rec.updated_at = time.time()

    async def wait_for_completion(
        self,
        trace_id: str,
        *,
        timeout: float = 90.0,
    ) -> AnalysisRecord:
        done = self._done_events.get(trace_id)
        if done is None:
            raise KeyError(trace_id)
        await asyncio.wait_for(done.wait(), timeout=timeout)
        rec = self._records.get(trace_id)
        if rec is None:
            raise KeyError(trace_id)
        return rec

    def get_record(self, trace_id: str) -> AnalysisRecord | None:
        return self._records.get(trace_id)

    def status_counts(self) -> dict[str, int]:
        return dict(Counter(rec.status for rec in self._records.values()))

    def list_recent_trace_ids(self, *, limit: int = 10) -> list[str]:
        rows = sorted(self._records.values(), key=lambda x: x.updated_at, reverse=True)
        return [x.trace_id for x in rows[:limit]]

    async def get_health(self) -> dict[str, Any]:
        provider = self._health_provider
        if provider is None:
            deps = getattr(self._agent, "_deps", None)
            mcp = getattr(deps, "mcp", None)
            provider = getattr(mcp, "get_system_health", None)
        if provider is None:
            raise RuntimeError("health provider not configured")

        health = await provider()
        if isinstance(health, str):
            try:
                return dict(json.loads(health))
            except Exception:
                return {"raw": health}
        return dict(health)

    def cost_today_payload(self) -> dict[str, Any]:
        by_agent = self._cost_monitor.spent_by_agent_today()
        cap = getattr(getattr(self._cost_monitor, "_config", None), "daily_cap_usd", None)
        return {
            "spent_today_usd": round(float(self._cost_monitor.spent_today_usd()), 6),
            "by_agent": {k: round(float(v), 6) for k, v in sorted(by_agent.items())},
            "daily_cap_usd": cap,
        }

    def serialize_record(self, trace_id: str) -> dict[str, Any]:
        rec = self.get_record(trace_id)
        if rec is None:
            raise KeyError(trace_id)
        payload: dict[str, Any] = {
            "trace_id": rec.trace_id,
            "status": rec.status,
            "query": rec.query.model_dump(),
            "error": rec.error,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
        }
        if rec.response is not None:
            payload["result"] = rec.response.model_dump()
        return payload


def create_web_app(runtime: AnalysisRuntime) -> FastAPI:
    app = FastAPI(
        title="Aetheer WebApp v3.0",
        description="Local-only API. No auth in v3.0 (planned for v3.1).",
        version="3.0.0",
    )

    @app.post("/api/analyze")
    async def analyze(request: AnalyzeRequest) -> dict[str, str]:
        try:
            trace_id = await runtime.submit(request)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"trace_id": trace_id}

    @app.get("/api/analysis/{trace_id}")
    async def analysis_state(trace_id: str) -> dict[str, Any]:
        try:
            return runtime.serialize_record(trace_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="trace_id not found") from exc

    @app.get("/api/analysis/{trace_id}/stream")
    async def analysis_stream(trace_id: str) -> StreamingResponse:
        if runtime.get_record(trace_id) is None:
            raise HTTPException(status_code=404, detail="trace_id not found")
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(
            runtime.stream_hub.sse_stream(trace_id),
            media_type="text/event-stream",
            headers=headers,
        )

    @app.websocket("/api/analysis/{trace_id}/ws")
    async def analysis_ws(websocket: WebSocket, trace_id: str) -> None:
        if runtime.get_record(trace_id) is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        await runtime.stream_hub.websocket_stream(trace_id, websocket)
        await websocket.close()

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return await runtime.get_health()

    @app.get("/api/cost/today")
    async def cost_today() -> dict[str, Any]:
        return runtime.cost_today_payload()

    return app


__all__ = [
    "AnalyzeRequest",
    "AnalysisRecord",
    "AnalysisRuntime",
    "create_web_app",
]
