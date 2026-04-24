"""Unit tests for services.cost_monitor."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.cost_monitor import BudgetConfig, CostMonitor


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "cost.sqlite"


def test_empty_monitor_reports_zero(tmp_db):
    m = CostMonitor(tmp_db)
    assert m.spent_today_usd() == 0.0
    assert m.remaining_budget_usd() == 10.00
    assert not m.should_downgrade()
    assert not m.should_block()


def test_record_accumulates(tmp_db):
    m = CostMonitor(tmp_db)
    m.record(agent_name="synthesis", cost_usd=0.30, prompt_tokens=1000, completion_tokens=500)
    m.record(agent_name="synthesis", cost_usd=0.20, prompt_tokens=800, completion_tokens=400)
    m.record(agent_name="governor", cost_usd=0.01, prompt_tokens=500, completion_tokens=50)
    assert m.spent_today_usd() == pytest.approx(0.51)
    by_agent = m.spent_by_agent_today()
    assert by_agent["synthesis"] == pytest.approx(0.50)
    assert by_agent["governor"] == pytest.approx(0.01)


def test_soft_threshold_triggers_downgrade(tmp_db):
    m = CostMonitor(tmp_db, config=BudgetConfig(daily_cap_usd=1.0, soft_threshold_pct=0.5))
    m.record(agent_name="x", cost_usd=0.40, prompt_tokens=0, completion_tokens=0)
    assert not m.should_downgrade()
    m.record(agent_name="x", cost_usd=0.15, prompt_tokens=0, completion_tokens=0)
    assert m.should_downgrade()
    assert not m.should_block()


def test_hard_cap_blocks(tmp_db):
    m = CostMonitor(tmp_db, config=BudgetConfig(daily_cap_usd=1.0))
    m.record(agent_name="x", cost_usd=1.00, prompt_tokens=0, completion_tokens=0)
    assert m.should_block()
    assert m.remaining_budget_usd() == 0.0


def test_alert_fires_once_at_threshold(tmp_db):
    received: list[tuple[str, dict]] = []

    def sink(code, payload):
        received.append((code, payload))

    m = CostMonitor(
        tmp_db,
        config=BudgetConfig(daily_cap_usd=1.0, alert_threshold_pct=0.8),
        alert_sink=sink,
    )
    m.record(agent_name="x", cost_usd=0.5, prompt_tokens=0, completion_tokens=0)
    assert received == []
    m.record(agent_name="x", cost_usd=0.4, prompt_tokens=0, completion_tokens=0)
    assert len(received) == 1
    assert received[0][0] == "OPENROUTER_BUDGET_ALERT"
    assert received[0][1]["pct_of_cap"] == pytest.approx(0.9)
    # Next record same day must NOT re-alert.
    m.record(agent_name="x", cost_usd=0.05, prompt_tokens=0, completion_tokens=0)
    assert len(received) == 1


def test_day_rollover_resets_alert_flag(tmp_db):
    clock = {"t": datetime(2026, 4, 24, 23, 50, tzinfo=timezone.utc)}
    received: list[tuple[str, dict]] = []
    m = CostMonitor(
        tmp_db,
        config=BudgetConfig(daily_cap_usd=1.0, alert_threshold_pct=0.8),
        alert_sink=lambda c, p: received.append((c, p)),
        clock=lambda: clock["t"],
    )
    m.record(agent_name="x", cost_usd=0.9, prompt_tokens=0, completion_tokens=0)
    assert len(received) == 1

    # Advance to next UTC day; spent_today resets, alert flag cleared.
    clock["t"] = datetime(2026, 4, 25, 0, 5, tzinfo=timezone.utc)
    assert m.spent_today_usd() == 0.0
    m.record(agent_name="x", cost_usd=0.85, prompt_tokens=0, completion_tokens=0)
    assert len(received) == 2, "second day should be able to alert again"


def test_negative_cost_rejected(tmp_db):
    m = CostMonitor(tmp_db)
    with pytest.raises(ValueError):
        m.record(agent_name="x", cost_usd=-0.01, prompt_tokens=0, completion_tokens=0)


def test_alert_sink_exception_does_not_break_record(tmp_db):
    def bad_sink(*_):
        raise RuntimeError("telegram down")

    m = CostMonitor(
        tmp_db,
        config=BudgetConfig(daily_cap_usd=1.0, alert_threshold_pct=0.5),
        alert_sink=bad_sink,
    )
    m.record(agent_name="x", cost_usd=0.6, prompt_tokens=0, completion_tokens=0)
    assert m.spent_today_usd() == pytest.approx(0.6)


def test_persistence_across_instances(tmp_db):
    m1 = CostMonitor(tmp_db)
    m1.record(agent_name="x", cost_usd=0.123, prompt_tokens=10, completion_tokens=5)
    m2 = CostMonitor(tmp_db)
    assert m2.spent_today_usd() == pytest.approx(0.123)
