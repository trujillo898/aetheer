"""APScheduler-based runner for the daily Aetheer presets.

Why APScheduler instead of a hand-rolled `asyncio.sleep_until` loop:

    * Cron-style "every day at 07:00 UTC" is what `CronTrigger` already
      models — including DST awareness if we ever add a non-UTC timezone.
    * `next_run_time` is exposed per job, which we surface via
      `next_run_at()` for observability.
    * `coalesce=True` + `misfire_grace_time` give us a sane recovery if
      the process slept across a fire window (laptop closed, deploy mid-fire,
      etc.) — the job runs once on resume and skips the missed batch.

Error isolation: every preset runs inside `_run_preset()` which catches
`Exception` and logs it. APScheduler's default behavior on exception is to
keep the job scheduled (it would only drop a *one-off* job on error), so a
single failed analysis does not unschedule the daily preset.

Feature flag: `config/feature_flags.yaml: scheduler.enabled` gates `start()`.
When the flag is False, `start()` is a no-op (acceptance #1) and we don't
even register the cron jobs — the AsyncIOScheduler instance is still
constructed (cheap) but never started.

Result routing (acceptance #5): a `result_sink` async callable is invoked
with each `CognitiveResponse`. If no sink is wired, we log a one-line
summary at INFO. Fase 6 will wire `interfaces.sync_bus` as the sink.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.schemas import CognitiveQuery, CognitiveResponse
from services.schedule_presets import (
    PRESET_BUILDERS,
    ScheduleConfig,
    load_presets_from_env,
)

logger = logging.getLogger("aetheer.scheduler")


class CognitiveAgentLike(Protocol):
    """Subset of `AetheerCognitiveAgent` we depend on."""

    async def cognitive_analysis(self, query: CognitiveQuery) -> CognitiveResponse: ...


ResultSink = Callable[[CognitiveResponse], Awaitable[None]]


@dataclass(slots=True)
class _SchedulerFlags:
    enabled: bool = False
    timezone: str = "UTC"


def _load_flags(path: str | Path | None) -> _SchedulerFlags:
    """Read scheduler flags from feature_flags.yaml. Defaults are conservative."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config" / "feature_flags.yaml"
    p = Path(path)
    if not p.exists():
        logger.warning("feature_flags.yaml missing at %s; scheduler disabled", p)
        return _SchedulerFlags(enabled=False)
    try:
        import yaml
        data = yaml.safe_load(p.read_text()) or {}
        block = data.get("scheduler") or {}
        return _SchedulerFlags(
            enabled=bool(block.get("enabled", False)),
            timezone=str(block.get("timezone", "UTC")),
        )
    except Exception as e:
        logger.warning("failed to read feature flags %s: %s", p, e)
        return _SchedulerFlags(enabled=False)


class AetheerScheduler:
    """Owns one AsyncIOScheduler and N preset jobs.

    Lifecycle:
        s = AetheerScheduler(agent, configs)
        await s.start()
        ...
        await s.stop()

    Construction never starts the scheduler — that's `start()`'s job. This
    means tests can probe `next_run_at()` without spinning a real event loop.
    """

    def __init__(
        self,
        cognitive_agent: CognitiveAgentLike,
        config: list[ScheduleConfig],
        *,
        result_sink: ResultSink | None = None,
        feature_flags_path: str | Path | None = None,
        flags: _SchedulerFlags | None = None,
    ) -> None:
        self._agent = cognitive_agent
        self._configs: dict[str, ScheduleConfig] = {c.name: c for c in config}
        self._result_sink = result_sink
        self._flags = flags if flags is not None else _load_flags(feature_flags_path)
        self._scheduler = AsyncIOScheduler(timezone=self._flags.timezone)
        self._started = False

        if self._flags.enabled and self._configs:
            self._register_jobs()
        elif not self._flags.enabled:
            logger.info("scheduler disabled by feature flag — no jobs registered")

    # ─────────────────────── public API ───────────────────────

    @classmethod
    def from_env(
        cls,
        cognitive_agent: CognitiveAgentLike,
        *,
        result_sink: ResultSink | None = None,
        env: dict[str, str] | None = None,
        feature_flags_path: str | Path | None = None,
    ) -> "AetheerScheduler":
        """Build from `AETHEER_SCHEDULE_*` env vars.

        Use this in production; tests usually construct directly with
        explicit configs to avoid touching real env state.
        """
        configs = load_presets_from_env(env if env is not None else dict(os.environ))
        return cls(
            cognitive_agent,
            configs,
            result_sink=result_sink,
            feature_flags_path=feature_flags_path,
        )

    async def start(self) -> None:
        """Start the underlying scheduler. No-op when disabled by flag."""
        if not self._flags.enabled:
            logger.info("scheduler.start: disabled by feature flag (no-op)")
            return
        if self._started:
            return
        if not self._configs:
            logger.info("scheduler.start: no presets configured (no-op)")
            return
        self._scheduler.start()
        self._started = True
        logger.info(
            "scheduler started — jobs=%s",
            sorted(j.id for j in self._scheduler.get_jobs()),
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False
        logger.info("scheduler stopped")

    def next_run_at(self, name: str) -> datetime | None:
        """Next firing time for preset `name`, or None if not registered.

        Computed from the trigger directly (not from `Job.next_run_time`)
        so this works whether or not `start()` has been called — tests
        rely on this to probe timing under freezegun without spinning
        a real event loop.
        """
        job = self._scheduler.get_job(name)
        if job is None:
            return None
        from datetime import datetime as _dt
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self._flags.timezone)
        except Exception:
            tz = timezone.utc
        now_local = _dt.now(tz)
        next_fire = job.trigger.get_next_fire_time(None, now_local)
        if next_fire is None:
            return None
        return next_fire.astimezone(timezone.utc)

    def configured_presets(self) -> list[str]:
        return sorted(self._configs)

    # ─────────────────────── internals ───────────────────────

    def _register_jobs(self) -> None:
        for name, cfg in self._configs.items():
            trigger = CronTrigger(
                hour=cfg.time_utc.hour,
                minute=cfg.time_utc.minute,
                second=0,
                timezone=self._flags.timezone,
            )
            self._scheduler.add_job(
                self._run_preset,
                trigger=trigger,
                id=name,
                name=f"aetheer-{name}",
                args=[name],
                coalesce=True,            # missed runs collapse to one
                max_instances=1,          # never overlap with itself
                misfire_grace_time=600,   # 10 min grace before silently dropping
                replace_existing=True,
            )

    async def _run_preset(self, name: str) -> CognitiveResponse | None:
        """Execute one preset. Catches all exceptions (acceptance #4).

        Public-ish (still underscore-prefixed) because tests call it
        directly to verify behavior without spinning the scheduler loop.
        """
        cfg = self._configs.get(name)
        if cfg is None:
            logger.warning("scheduler._run_preset: unknown preset %r", name)
            return None

        # Materialize a fresh trace_id per fire so trajectories stay distinct.
        query = self._materialize_query(cfg)
        logger.info(
            "scheduler firing preset=%s trace_id=%s instruments=%s",
            name, query.trace_id, query.instruments,
        )

        try:
            response = await self._agent.cognitive_analysis(query)
        except Exception as e:
            # Never let a runtime error kill the scheduler. The exception
            # log is the only place this surfaces — Fase 6 will also push
            # it to the alert sink.
            logger.exception(
                "scheduler preset=%s failed (suppressed to keep job alive): %s",
                name, e,
            )
            return None

        await self._dispatch(name, response)
        return response

    def _materialize_query(self, cfg: ScheduleConfig) -> CognitiveQuery:
        # Regenerate trace_id; keep everything else from the template.
        from uuid import uuid4
        return cfg.query.model_copy(update={
            "trace_id": f"sched-{cfg.name}-{uuid4().hex[:12]}",
        })

    async def _dispatch(self, name: str, response: CognitiveResponse) -> None:
        if self._result_sink is None:
            logger.info(
                "scheduler preset=%s done approved=%s mode=%s quality=%.2f trace=%s "
                "(no result_sink wired — logging only)",
                name, response.approved, response.operating_mode,
                response.quality.global_score, response.trace_id,
            )
            return
        try:
            await self._result_sink(response)
        except Exception as e:
            logger.warning(
                "result_sink raised for preset=%s trace=%s (suppressed): %s",
                name, response.trace_id, e,
            )


__all__ = [
    "AetheerScheduler",
    "CognitiveAgentLike",
    "ResultSink",
]
