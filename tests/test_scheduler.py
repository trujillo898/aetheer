"""Tests for `services.scheduler.AetheerScheduler`.

Coverage matches the Fase 5 acceptance criteria:

    1. feature_flags.scheduler.enabled=False → start() is a no-op AND no
       jobs are registered.
    2. Empty AETHEER_SCHEDULE_* → that preset is NOT registered.
    3. freezegun: at 07:00 UTC, `next_run_at("london")` is "now" (or
       tomorrow once the minute passes), and the *only* preset whose
       next firing equals 07:00 is `london` — verifying we wired the
       cron triggers to the right minute.
    4. Errors inside cognitive_analysis are logged but do NOT propagate;
       the scheduler stays alive.
    5. Result is routed: result_sink (when wired) receives the response;
       without a sink, we don't blow up.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone
from typing import Any

import pytest
from freezegun import freeze_time

from agents.schemas import (
    CognitiveQuery,
    CognitiveResponse,
    QualityBreakdown,
)
from services.schedule_presets import (
    daily_preset,
    london_preset,
    ny_preset,
)
from services.scheduler import AetheerScheduler, _SchedulerFlags


# ───────────────────── helpers ─────────────────────


class _FakeAgent:
    """Records calls; returns a canned approved response by default."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[CognitiveQuery] = []
        self._raises = raises

    async def cognitive_analysis(self, query: CognitiveQuery) -> CognitiveResponse:
        self.calls.append(query)
        if self._raises is not None:
            raise self._raises
        qb = QualityBreakdown(
            freshness=0.85, completeness=0.85, consistency=0.85,
            source_reliability=0.85, aetheer_validity=0.85,
        )
        return CognitiveResponse(
            approved=True,
            operating_mode="ONLINE",
            quality=qb,
            synthesis_text="Análisis programado: estructura intacta...",
            cost_usd=0.012,
            latency_ms=8000,
            trace_id=query.trace_id,
        )


def _all_presets() -> list:
    return [
        london_preset(time(7, 0)),
        ny_preset(time(12, 30)),
        daily_preset(time(22, 0)),
    ]


# ───────────────────── tests ─────────────────────


# --- acceptance #1: disabled flag ---


async def test_disabled_flag_makes_start_a_noop() -> None:
    s = AetheerScheduler(
        _FakeAgent(),
        _all_presets(),
        flags=_SchedulerFlags(enabled=False),
    )
    await s.start()
    assert s._started is False
    # No jobs should have been registered when disabled
    assert s._scheduler.get_jobs() == []


async def test_enabled_flag_registers_jobs() -> None:
    s = AetheerScheduler(
        _FakeAgent(),
        _all_presets(),
        flags=_SchedulerFlags(enabled=True),
    )
    job_ids = sorted(j.id for j in s._scheduler.get_jobs())
    assert job_ids == ["daily", "london", "ny"]
    # start/stop is safe even though there's no real loop firing
    await s.start()
    assert s._started is True
    await s.stop()
    assert s._started is False


async def test_enabled_but_no_configs_is_noop() -> None:
    s = AetheerScheduler(
        _FakeAgent(),
        [],
        flags=_SchedulerFlags(enabled=True),
    )
    await s.start()
    assert s._started is False  # nothing to do


# --- acceptance #2: empty env var → no job ---
# (covered exhaustively in test_schedule_presets.py; one
#  end-to-end check here that empty config means no job.)


async def test_only_configured_presets_are_registered() -> None:
    s = AetheerScheduler(
        _FakeAgent(),
        [london_preset(time(7, 0))],
        flags=_SchedulerFlags(enabled=True),
    )
    job_ids = sorted(j.id for j in s._scheduler.get_jobs())
    assert job_ids == ["london"]
    assert s.next_run_at("ny") is None
    assert s.next_run_at("daily") is None


# --- acceptance #3: freezegun timing ---


@freeze_time("2026-04-25 06:59:59", tz_offset=0)
async def test_next_run_at_seven_am_resolves_to_today_seven_am() -> None:
    s = AetheerScheduler(
        _FakeAgent(),
        _all_presets(),
        flags=_SchedulerFlags(enabled=True),
    )
    nrt = s.next_run_at("london")
    assert nrt is not None
    assert nrt == datetime(2026, 4, 25, 7, 0, tzinfo=timezone.utc)
    # ny / daily are both later today
    assert s.next_run_at("ny") == datetime(2026, 4, 25, 12, 30, tzinfo=timezone.utc)
    assert s.next_run_at("daily") == datetime(2026, 4, 25, 22, 0, tzinfo=timezone.utc)


@freeze_time("2026-04-25 07:00:30", tz_offset=0)
async def test_after_fire_minute_next_run_rolls_to_tomorrow() -> None:
    """At 07:00:30, london already fired today → next run is tomorrow."""
    s = AetheerScheduler(
        _FakeAgent(),
        _all_presets(),
        flags=_SchedulerFlags(enabled=True),
    )
    nrt = s.next_run_at("london")
    assert nrt == datetime(2026, 4, 26, 7, 0, tzinfo=timezone.utc)
    # ny still upcoming today
    assert s.next_run_at("ny") == datetime(2026, 4, 25, 12, 30, tzinfo=timezone.utc)


@freeze_time("2026-04-25 07:00:00", tz_offset=0)
async def test_at_exact_fire_minute_only_london_matches() -> None:
    """Acceptance #3: at 07:00 UTC, only `london` is the firing preset."""
    s = AetheerScheduler(
        _FakeAgent(),
        _all_presets(),
        flags=_SchedulerFlags(enabled=True),
    )
    target = datetime(2026, 4, 25, 7, 0, tzinfo=timezone.utc)
    matching = [
        name for name in ("london", "ny", "daily")
        if s.next_run_at(name) == target
    ]
    # CronTrigger.get_next_fire_time at 07:00 returns 07:00 itself for london
    # (boundary-inclusive), but ny/daily are hours later — so only london matches.
    assert matching == ["london"]


# --- acceptance #4: errors don't kill the scheduler ---


async def test_run_preset_swallows_agent_exceptions() -> None:
    bad_agent = _FakeAgent(raises=RuntimeError("upstream LLM 503"))
    s = AetheerScheduler(
        bad_agent,
        _all_presets(),
        flags=_SchedulerFlags(enabled=True),
    )
    # _run_preset must NEVER raise — that's the whole point.
    result = await s._run_preset("london")
    assert result is None
    assert len(bad_agent.calls) == 1


async def test_run_preset_unknown_name_is_safe() -> None:
    s = AetheerScheduler(
        _FakeAgent(),
        _all_presets(),
        flags=_SchedulerFlags(enabled=True),
    )
    assert await s._run_preset("nonexistent") is None


# --- acceptance #5: result routing ---


async def test_result_sink_receives_response() -> None:
    seen: list[CognitiveResponse] = []

    async def sink(r: CognitiveResponse) -> None:
        seen.append(r)

    agent = _FakeAgent()
    s = AetheerScheduler(
        agent,
        [london_preset(time(7, 0))],
        result_sink=sink,
        flags=_SchedulerFlags(enabled=True),
    )
    response = await s._run_preset("london")
    assert response is not None and response.approved is True
    assert seen == [response]
    assert agent.calls[0].requested_by == "scheduler"
    assert agent.calls[0].trace_id.startswith("sched-london-")


async def test_no_result_sink_logs_summary(caplog: pytest.LogCaptureFixture) -> None:
    agent = _FakeAgent()
    s = AetheerScheduler(
        agent,
        [london_preset(time(7, 0))],
        result_sink=None,
        flags=_SchedulerFlags(enabled=True),
    )
    with caplog.at_level(logging.INFO, logger="aetheer.scheduler"):
        await s._run_preset("london")
    # Some log line should mention "no result_sink wired"
    assert any("no result_sink wired" in rec.getMessage() for rec in caplog.records)


async def test_result_sink_failure_is_isolated() -> None:
    """A flaky sink must not crash the scheduler call path."""
    async def bad_sink(r: CognitiveResponse) -> None:
        raise RuntimeError("redis down")

    agent = _FakeAgent()
    s = AetheerScheduler(
        agent,
        [london_preset(time(7, 0))],
        result_sink=bad_sink,
        flags=_SchedulerFlags(enabled=True),
    )
    # Should NOT raise even though the sink does
    response = await s._run_preset("london")
    assert response is not None and response.approved is True


# --- env-driven construction ---


async def test_from_env_loads_only_configured_presets() -> None:
    s = AetheerScheduler.from_env(
        _FakeAgent(),
        env={"AETHEER_SCHEDULE_LONDON": "07:00"},
    )
    assert s.configured_presets() == ["london"]
