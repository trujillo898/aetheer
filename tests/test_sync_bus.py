from __future__ import annotations

import asyncio
import time

import pytest

from interfaces.sync_bus import SyncBus


async def _next_event(bus: SyncBus, channel: str):
    async for event in bus.subscribe(channel):
        return event, time.monotonic()
    raise RuntimeError("subscription ended unexpectedly")


@pytest.mark.asyncio
async def test_publish_reaches_two_subscribers_within_two_seconds() -> None:
    bus = SyncBus()  # in-memory mode
    a_task = asyncio.create_task(_next_event(bus, "analysis"))
    b_task = asyncio.create_task(_next_event(bus, "analysis"))
    await asyncio.sleep(0.05)

    await bus.publish_analysis(
        "trace-sync-1",
        {"status": "completed", "approved": True},
        status="completed",
    )
    (a_event, a_time), (b_event, b_time) = await asyncio.gather(a_task, b_task)

    assert a_event.trace_id == "trace-sync-1"
    assert b_event.trace_id == "trace-sync-1"
    assert abs(a_time - b_time) < 2.0


@pytest.mark.asyncio
async def test_offline_propagation_under_one_second() -> None:
    bus = SyncBus()
    task = asyncio.create_task(_next_event(bus, "system"))
    await asyncio.sleep(0.05)

    start = time.monotonic()
    await bus.publish_operating_mode(
        mode="OFFLINE",
        source="webapp",
        reason="kill switch",
        trace_id="trace-offline-1",
    )
    (event, received_at) = await task
    assert event.event == "system.operating_mode"
    assert event.payload["mode"] == "OFFLINE"
    assert (received_at - start) < 1.0
