"""Redis-backed pub/sub wrapper with an in-memory fallback for tests.

The project requirement is Redis pub/sub synchronization between interfaces.
This module keeps that behavior when `redis` is installed and a URL is
provided, while still allowing unit tests to run without external services.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("aetheer.interfaces.sync_bus")

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - optional dependency at runtime
    Redis = None  # type: ignore[assignment]

_STOP = object()


@dataclass(frozen=True, slots=True)
class SyncEvent:
    """Normalized event envelope transported by the synchronization bus."""

    channel: str
    event: str
    payload: dict[str, Any]
    trace_id: str | None
    timestamp: float

    def model_dump(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "event": self.event,
            "payload": self.payload,
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_payload(cls, channel: str, raw: dict[str, Any]) -> "SyncEvent":
        return cls(
            channel=channel,
            event=str(raw.get("event", "message")),
            payload=dict(raw.get("payload") or {}),
            trace_id=raw.get("trace_id"),
            timestamp=float(raw.get("timestamp") or time.time()),
        )


class _InMemoryBroker:
    """Tiny in-process pub/sub broker used as test fallback."""

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue[Any]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, data: str) -> int:
        async with self._lock:
            queues = list(self._subs.get(channel, []))
        for q in queues:
            q.put_nowait(data)
        return len(queues)

    async def subscribe(self, channel: str) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue()
        async with self._lock:
            self._subs[channel].append(q)
        return q

    async def unsubscribe(self, channel: str, queue: asyncio.Queue[Any]) -> None:
        async with self._lock:
            arr = self._subs.get(channel, [])
            if queue in arr:
                arr.remove(queue)
            if not arr and channel in self._subs:
                self._subs.pop(channel, None)
        queue.put_nowait(_STOP)


class SyncBus:
    """Redis pub/sub interface used by WebApp and Telegram interface layers."""

    def __init__(
        self,
        *,
        redis_client: Redis | None = None,
        channel_prefix: str = "aetheer",
    ) -> None:
        self._redis = redis_client
        self._prefix = channel_prefix.strip() or "aetheer"
        self._memory = _InMemoryBroker() if redis_client is None else None

    @property
    def uses_redis(self) -> bool:
        return self._redis is not None

    @classmethod
    def from_redis_url(
        cls,
        redis_url: str | None,
        *,
        channel_prefix: str = "aetheer",
    ) -> "SyncBus":
        if redis_url and Redis is not None:
            client = Redis.from_url(redis_url, decode_responses=True)
            return cls(redis_client=client, channel_prefix=channel_prefix)
        return cls(redis_client=None, channel_prefix=channel_prefix)

    def _channel(self, channel: str) -> str:
        ch = channel.strip().replace(" ", "_")
        return f"{self._prefix}.{ch}"

    async def publish(
        self,
        channel: str,
        *,
        event: str,
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> int:
        envelope = SyncEvent(
            channel=channel,
            event=event,
            payload=dict(payload or {}),
            trace_id=trace_id,
            timestamp=time.time(),
        )
        body = json.dumps(envelope.model_dump(), ensure_ascii=False)
        target = self._channel(channel)
        if self._redis is not None:
            try:
                return int(await self._redis.publish(target, body))
            except Exception as exc:
                logger.warning("redis publish failed on %s: %s", target, exc)
                return 0
        assert self._memory is not None
        return await self._memory.publish(target, body)

    async def publish_analysis(
        self,
        trace_id: str,
        payload: dict[str, Any],
        *,
        status: str,
    ) -> int:
        return await self.publish(
            "analysis",
            event=f"analysis.{status}",
            payload=payload,
            trace_id=trace_id,
        )

    async def publish_operating_mode(
        self,
        *,
        mode: str,
        source: str,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> int:
        data: dict[str, Any] = {"mode": mode, "source": source}
        if reason:
            data["reason"] = reason
        return await self.publish(
            "system",
            event="system.operating_mode",
            payload=data,
            trace_id=trace_id,
        )

    async def subscribe(
        self,
        channel: str,
        *,
        poll_interval: float = 0.1,
    ) -> AsyncIterator[SyncEvent]:
        target = self._channel(channel)
        if self._redis is not None:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(target)
            try:
                while True:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=poll_interval,
                    )
                    if msg is None:
                        await asyncio.sleep(0)
                        continue
                    data = msg.get("data")
                    if not isinstance(data, str):
                        continue
                    try:
                        raw = json.loads(data)
                        yield SyncEvent.from_payload(channel, raw)
                    except Exception:
                        logger.warning("discarding malformed bus message on %s", target)
            finally:
                await pubsub.unsubscribe(target)
                await pubsub.close()
            return

        assert self._memory is not None
        queue = await self._memory.subscribe(target)
        try:
            while True:
                data = await queue.get()
                if data is _STOP:
                    return
                if not isinstance(data, str):
                    continue
                try:
                    raw = json.loads(data)
                    yield SyncEvent.from_payload(channel, raw)
                except Exception:
                    logger.warning("discarding malformed memory bus message on %s", target)
        finally:
            await self._memory.unsubscribe(target, queue)

    async def wait_for(
        self,
        channel: str,
        *,
        predicate: Any,
        timeout: float = 5.0,
    ) -> SyncEvent:
        async def _consume() -> SyncEvent:
            async for event in self.subscribe(channel):
                if predicate(event):
                    return event
            raise TimeoutError("subscription ended unexpectedly")

        return await asyncio.wait_for(_consume(), timeout=timeout)

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.close()


__all__ = ["SyncBus", "SyncEvent"]
