"""Tests for `services.schedule_presets`.

Cover:
    * Preset builders return well-formed `CognitiveQuery` instances.
    * `parse_hhmm` rejects garbage and accepts blanks → None.
    * `load_presets_from_env` skips empty / unset env vars (acceptance #2).
"""
from __future__ import annotations

from datetime import time

import pytest

from agents.schemas import CognitiveQuery
from services.schedule_presets import (
    PRESET_BUILDERS,
    ScheduleConfig,
    daily_preset,
    load_presets_from_env,
    london_preset,
    ny_preset,
    parse_hhmm,
)


def test_london_preset_shape() -> None:
    cfg = london_preset(time(7, 0))
    assert isinstance(cfg, ScheduleConfig)
    assert cfg.name == "london"
    assert cfg.time_utc == time(7, 0)
    assert isinstance(cfg.query, CognitiveQuery)
    assert cfg.query.query_intent == "full_analysis"
    assert cfg.query.requested_by == "scheduler"
    assert "DXY" in cfg.query.instruments
    assert "EURUSD" in cfg.query.instruments
    assert "GBPUSD" in cfg.query.instruments


def test_ny_preset_shape() -> None:
    cfg = ny_preset(time(12, 30))
    assert cfg.name == "ny"
    assert cfg.time_utc == time(12, 30)
    assert "M15" in cfg.query.timeframes  # NY focuses on intraday liquidity


def test_daily_preset_shape() -> None:
    cfg = daily_preset(time(22, 0))
    assert cfg.name == "daily"
    assert cfg.time_utc == time(22, 0)
    assert "D1" in cfg.query.timeframes


def test_preset_builders_table_is_complete() -> None:
    assert set(PRESET_BUILDERS) == {"london", "ny", "daily"}


def test_preset_trace_id_unique_across_calls() -> None:
    c1 = london_preset(time(7, 0))
    c2 = london_preset(time(7, 0))
    assert c1.query.trace_id != c2.query.trace_id


def test_schedule_config_strips_seconds_and_microseconds() -> None:
    cfg = ScheduleConfig(
        name="custom",
        time_utc=time(7, 0, 30, 12345),
        query=london_preset(time(7, 0)).query,
    )
    assert cfg.time_utc == time(7, 0)


@pytest.mark.parametrize("raw,expected", [
    ("07:00", time(7, 0)),
    ("12:30", time(12, 30)),
    ("0:5",   time(0, 5)),
    ("23:59", time(23, 59)),
    ("",      None),
    ("   ",   None),
    (None,    None),
])
def test_parse_hhmm_valid(raw, expected) -> None:
    assert parse_hhmm(raw) == expected


@pytest.mark.parametrize("raw", [
    "24:00",   # hour out of range
    "07:60",   # minute out of range
    "garbage",
    "7-30",
    "07:00:00",  # we only accept HH:MM (split on first ':')
])
def test_parse_hhmm_invalid(raw) -> None:
    if raw == "07:00:00":
        # "07:00:00" splits with maxsplit=1 → "07", "00:00" which int() rejects
        with pytest.raises(ValueError):
            parse_hhmm(raw)
    else:
        with pytest.raises(ValueError):
            parse_hhmm(raw)


def test_load_presets_from_env_full() -> None:
    env = {
        "AETHEER_SCHEDULE_LONDON": "07:00",
        "AETHEER_SCHEDULE_NY":     "12:30",
        "AETHEER_SCHEDULE_DAILY":  "22:00",
    }
    configs = load_presets_from_env(env)
    names = [c.name for c in configs]
    assert names == ["london", "ny", "daily"]


def test_load_presets_from_env_partial_skips_empty() -> None:
    """Acceptance #2: empty env var → preset NOT registered."""
    env = {
        "AETHEER_SCHEDULE_LONDON": "07:00",
        "AETHEER_SCHEDULE_NY":     "",          # blank → skip
        # AETHEER_SCHEDULE_DAILY unset → skip
    }
    configs = load_presets_from_env(env)
    assert [c.name for c in configs] == ["london"]


def test_load_presets_from_env_empty() -> None:
    assert load_presets_from_env({}) == []
