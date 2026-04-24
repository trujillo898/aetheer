"""Unit tests for agents.model_router."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.model_router import (
    AetheerModelRouter,
    BudgetExceededError,
    DEFAULT_ROUTES,
    ModelSpec,
)


def test_all_seven_agents_registered():
    assert set(DEFAULT_ROUTES) == {
        "context-orchestrator", "liquidity", "events", "price-behavior",
        "macro", "synthesis", "governor",
    }


def test_select_returns_primary_when_cost_fits():
    r = AetheerModelRouter()
    sel = r.select(agent_name="synthesis", context_tokens=3000, expected_output_tokens=500)
    assert sel.primary.id == "anthropic/claude-sonnet-4.5"
    assert sel.fallbacks[0].id == "nvidia/nemotron-super-v1.5"
    assert sel.estimated_cost_usd > 0
    # sonnet 4.5: 3000*3 + 500*15 = 9k + 7.5k = 16.5k/1M = $0.0165
    assert sel.estimated_cost_usd == pytest.approx(0.0165, abs=1e-6)


def test_prefer_cheap_forces_cheap_override():
    r = AetheerModelRouter()
    sel = r.select(
        agent_name="synthesis",
        context_tokens=2000,
        expected_output_tokens=500,
        prefer_cheap=True,
    )
    assert sel.primary.id == "qwen/qwen3-plus"
    assert "prefer_cheap=on" in sel.reason


def test_primary_dropped_if_exceeds_max_per_call():
    r = AetheerModelRouter()
    # Liquidity cap = $0.005.
    #   nano:  (100k * 0.04 + 10k * 0.16) / 1M = $0.0056 > cap
    #   qwen:  (100k * 0.325 + 10k * 1.95) / 1M = $0.052  > cap
    # Every candidate exceeds the cap → BudgetExceededError.
    with pytest.raises(BudgetExceededError):
        r.select(
            agent_name="liquidity",
            context_tokens=100_000,
            expected_output_tokens=10_000,
        )


def test_budget_remaining_gate():
    r = AetheerModelRouter()
    # Governor cap is $0.005; primary gpt-5-nano is ~$0.0005 for 1000/300
    # With budget_remaining_usd=0.0001, every candidate is rejected.
    with pytest.raises(BudgetExceededError):
        r.select(
            agent_name="governor",
            context_tokens=1000,
            expected_output_tokens=300,
            budget_remaining_usd=0.0001,
        )


def test_fallbacks_preserved_in_order():
    r = AetheerModelRouter()
    sel = r.select(agent_name="price-behavior", context_tokens=2000, expected_output_tokens=800)
    # primary qwen + one fallback (nemotron-super) in DEFAULT_ROUTES
    assert sel.primary.id == "qwen/qwen3-plus"
    assert [m.id for m in sel.fallbacks] == ["nvidia/nemotron-super-v1.5"]


def test_unknown_agent_raises():
    r = AetheerModelRouter()
    with pytest.raises(KeyError):
        r.select(agent_name="nonexistent", context_tokens=100)


def test_custom_routes_honored():
    custom = {
        "my-agent": DEFAULT_ROUTES["synthesis"],
    }
    r = AetheerModelRouter(routes=custom)
    assert r.available_agents() == ["my-agent"]
    sel = r.select(agent_name="my-agent", context_tokens=1000)
    assert sel.primary.id == "anthropic/claude-sonnet-4.5"


def test_modelspec_cost_arithmetic():
    spec = ModelSpec("x/y", input_per_m=1.0, output_per_m=2.0)
    assert spec.estimate_cost(1_000_000, 0) == pytest.approx(1.0)
    assert spec.estimate_cost(0, 1_000_000) == pytest.approx(2.0)
    assert spec.estimate_cost(500_000, 500_000) == pytest.approx(1.5)
