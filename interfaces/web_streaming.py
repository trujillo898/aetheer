"""Streaming helpers for WebApp token delivery (SSE + WebSocket)."""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

_TERMINAL_EVENTS = {"done", "error"}


def encode_sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    lines = [f"event: {event}"]
    for line in payload.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


class AnalysisStreamHub:
    """In-process stream fanout keyed by trace_id."""

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue[Any]]] = defaultdict(list)
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._terminal: set[str] = set()
        self._lock = asyncio.Lock()

    async def publish_event(self, trace_id: str, event: str, data: dict[str, Any]) -> None:
        envelope = {
            "trace_id": trace_id,
            "event": event,
            "data": data,
            "timestamp": time.time(),
        }
        async with self._lock:
            self._history[trace_id].append(envelope)
            queues = list(self._subs.get(trace_id, []))
            if event in _TERMINAL_EVENTS:
                self._terminal.add(trace_id)
        for q in queues:
            q.put_nowait(envelope)

    async def publish_token(self, trace_id: str, token: str, index: int) -> None:
        await self.publish_event(trace_id, "token", {"token": token, "index": index})

    async def publish_done(self, trace_id: str) -> None:
        await self.publish_event(trace_id, "done", {})

    async def publish_error(self, trace_id: str, message: str) -> None:
        await self.publish_event(trace_id, "error", {"message": message})

    async def publish_synthesis_text(
        self,
        trace_id: str,
        text: str,
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        tokens = re.findall(r"\S+\s*", text)
        for idx, token in enumerate(tokens):
            await self.publish_token(trace_id, token, idx)
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

    async def subscribe(self, trace_id: str) -> AsyncIterator[dict[str, Any]]:
        async with self._lock:
            history = list(self._history.get(trace_id, []))
            terminal = trace_id in self._terminal
        for item in history:
            yield item
        if terminal:
            return

        queue: asyncio.Queue[Any] = asyncio.Queue()
        async with self._lock:
            self._subs[trace_id].append(queue)
        try:
            while True:
                item = await queue.get()
                if not isinstance(item, dict):
                    return
                yield item
                if item.get("event") in _TERMINAL_EVENTS:
                    return
        finally:
            async with self._lock:
                arr = self._subs.get(trace_id, [])
                if queue in arr:
                    arr.remove(queue)
                if not arr and trace_id in self._subs:
                    self._subs.pop(trace_id, None)

    async def sse_stream(self, trace_id: str) -> AsyncIterator[str]:
        async for item in self.subscribe(trace_id):
            event = str(item.get("event", "message"))
            data = dict(item.get("data") or {})
            data.setdefault("trace_id", trace_id)
            yield encode_sse(event, data)

    async def websocket_stream(self, trace_id: str, websocket: Any) -> None:
        async for item in self.subscribe(trace_id):
            event = str(item.get("event", "message"))
            data = dict(item.get("data") or {})
            await websocket.send_json(
                {"event": event, "trace_id": trace_id, "data": data}
            )


__all__ = ["AnalysisStreamHub", "encode_sse"]
