"""Built-in schedule presets and the `ScheduleConfig` schema.

Presets cover the three trading-session boundaries the analyst typically
wants: London open, NY open (≈ overlap start), and end-of-day review.
Each preset translates into a `CognitiveQuery` with a stable shape so
the cognitive agent treats them like any other request.

Env-var bindings (read by `services.scheduler.AetheerScheduler`):

    AETHEER_SCHEDULE_LONDON  → preset name "london"
    AETHEER_SCHEDULE_NY      → preset name "ny"
    AETHEER_SCHEDULE_DAILY   → preset name "daily"

Empty / unset env vars mean "don't register that job" (acceptance #2).
Custom schedules are constructed at the caller side via `ScheduleConfig(
name="custom", time_utc=..., query=...)`.
"""
from __future__ import annotations

from datetime import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.schemas import CognitiveQuery, QueryIntent

PresetName = Literal["london", "ny", "daily", "custom"]


class ScheduleConfig(BaseModel):
    """A single scheduled job.

    The trace_id on `query` is regenerated on every fire by the scheduler —
    we don't want all London runs to share one trace_id. The query stored
    here is the *template*; see `materialize_query()`.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    name: PresetName
    time_utc: time
    query: CognitiveQuery

    @field_validator("time_utc")
    @classmethod
    def _strip_microseconds(cls, v: time) -> time:
        # Cron triggers don't care about sub-second precision — keep the
        # field readable in logs.
        return v.replace(microsecond=0, second=0)


# ─────────────────────────── preset queries ───────────────────────────


def _preset_query(
    name: PresetName,
    *,
    query_text: str,
    intent: QueryIntent,
    instruments: list[str],
    timeframes: list[str],
) -> CognitiveQuery:
    return CognitiveQuery(
        query_text=query_text,
        query_intent=intent,
        instruments=instruments,
        timeframes=timeframes,  # type: ignore[arg-type]
        requested_by="scheduler",
        trace_id=f"sched-{name}-{uuid4().hex[:12]}",
    )


def london_preset(time_utc: time) -> ScheduleConfig:
    """Pre-Londres: estructura y posible direccionalidad de la sesión."""
    return ScheduleConfig(
        name="london",
        time_utc=time_utc,
        query=_preset_query(
            "london",
            query_text=(
                "Análisis pre-apertura Londres: estructura D1/H4/H1 de DXY, "
                "EURUSD y GBPUSD. Niveles clave del día previo, sesgo macro "
                "y eventos de alto impacto en la próxima ventana de 8h."
            ),
            intent="full_analysis",
            instruments=["DXY", "EURUSD", "GBPUSD"],
            timeframes=["D1", "H4", "H1"],
        ),
    )


def ny_preset(time_utc: time) -> ScheduleConfig:
    """Pre-NY / inicio de overlap: foco en liquidez y reacciones a datos US."""
    return ScheduleConfig(
        name="ny",
        time_utc=time_utc,
        query=_preset_query(
            "ny",
            query_text=(
                "Análisis pre-NY (inicio overlap London-NY): liquidez intradía, "
                "estado de DXY, eventos US de la sesión y posibles patrones "
                "de continuación o reversión en EURUSD/GBPUSD."
            ),
            intent="full_analysis",
            instruments=["DXY", "EURUSD", "GBPUSD"],
            timeframes=["H4", "H1", "M15"],
        ),
    )


def daily_preset(time_utc: time) -> ScheduleConfig:
    """End-of-day review: cierre del día, balance, calendario para mañana."""
    return ScheduleConfig(
        name="daily",
        time_utc=time_utc,
        query=_preset_query(
            "daily",
            query_text=(
                "Cierre del día: cómo cerraron DXY/EURUSD/GBPUSD, qué "
                "cadenas causales se validaron o invalidaron, y el "
                "calendario macro de las próximas 24h."
            ),
            intent="full_analysis",
            instruments=["DXY", "EURUSD", "GBPUSD"],
            timeframes=["D1", "H4"],
        ),
    )


PRESET_BUILDERS = {
    "london": london_preset,
    "ny": ny_preset,
    "daily": daily_preset,
}


# ─────────────────────────── env var loader ───────────────────────────


def parse_hhmm(raw: str | None) -> time | None:
    """Parse "HH:MM" (UTC) → datetime.time. Empty / None → None."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        hh, mm = s.split(":", 1)
        h = int(hh)
        m = int(mm)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"out of range: {s}")
        return time(hour=h, minute=m)
    except Exception as e:
        raise ValueError(f"invalid HH:MM value {raw!r}: {e}") from e


def load_presets_from_env(env: dict[str, str] | None = None) -> list[ScheduleConfig]:
    """Build the configured preset list from env vars.

    Acceptance #2: env var unset *or* empty → preset NOT registered.
    """
    import os
    src = env if env is not None else dict(os.environ)
    bindings = [
        ("london", "AETHEER_SCHEDULE_LONDON"),
        ("ny",     "AETHEER_SCHEDULE_NY"),
        ("daily",  "AETHEER_SCHEDULE_DAILY"),
    ]
    configs: list[ScheduleConfig] = []
    for name, env_key in bindings:
        t = parse_hhmm(src.get(env_key))
        if t is None:
            continue
        configs.append(PRESET_BUILDERS[name](t))
    return configs


__all__ = [
    "ScheduleConfig",
    "PresetName",
    "PRESET_BUILDERS",
    "london_preset",
    "ny_preset",
    "daily_preset",
    "parse_hhmm",
    "load_presets_from_env",
]
