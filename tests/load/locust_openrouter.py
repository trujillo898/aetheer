"""Locust load profile for Aetheer/OpenRouter path.

Usage:
    locust -f tests/load/locust_openrouter.py \
      --host=http://127.0.0.1:8000 --users=20 --spawn-rate=5
"""
from __future__ import annotations

import time

from locust import HttpUser, between, events, task


class AetheerLoadUser(HttpUser):
    wait_time = between(0.1, 1.0)

    def _workflow(self, *, intent: str, query_text: str, timeout_s: float) -> None:
        started = time.perf_counter()
        trace_id: str | None = None

        with self.client.post(
            "/api/analyze",
            name=f"analyze:{intent}:submit",
            json={
                "query_text": query_text,
                "query_intent": intent,
                "requested_by": "webapp",
                "instruments": ["DXY", "EURUSD", "GBPUSD"],
                "timeframes": ["M15", "H1", "H4"],
            },
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"submit failed: {resp.status_code}")
                return
            try:
                trace_id = resp.json()["trace_id"]
                resp.success()
            except Exception as exc:
                resp.failure(f"invalid json: {exc}")
                return

        assert trace_id is not None
        deadline = time.time() + timeout_s
        exception: Exception | None = None
        response_length = 0

        while time.time() < deadline:
            poll = self.client.get(
                f"/api/analysis/{trace_id}",
                name=f"analyze:{intent}:poll",
            )
            response_length = len(poll.text)
            if poll.status_code != 200:
                exception = RuntimeError(f"poll status={poll.status_code}")
                break
            try:
                status = poll.json().get("status")
            except Exception as exc:
                exception = RuntimeError(f"poll json parse failed: {exc}")
                break
            if status == "completed":
                break
            if status == "failed":
                exception = RuntimeError("analysis failed")
                break
            time.sleep(0.25)
        else:
            exception = TimeoutError(f"{intent} timeout after {timeout_s}s")

        total_ms = (time.perf_counter() - started) * 1000
        events.request.fire(
            request_type="workflow",
            name=f"analysis:{intent}",
            response_time=total_ms,
            response_length=response_length,
            response=None,
            context={},
            exception=exception,
        )

    @task(3)
    def punctual(self) -> None:
        self._workflow(
            intent="punctual",
            query_text="sesgo puntual dxy en h1",
            timeout_s=3.0,
        )

    @task(1)
    def full_analysis(self) -> None:
        self._workflow(
            intent="full_analysis",
            query_text="analisis completo de sesion londres y ny",
            timeout_s=45.0,
        )
