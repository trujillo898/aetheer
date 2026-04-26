"""D012 quality_score — pure-function tests.

Three fixtures verify the weighted formula at 1e-4. Plus targeted tests for
freshness extremes and the consistency-on-contradictions monotonicity rule.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.quality_score import (
    AetheerSnapshot,
    aetheer_validity_score,
    calculate,
    completeness_score,
    consistency_score,
    freshness_score,
    source_reliability_score,
)
from agents.schemas import (
    QUALITY_WEIGHTS,
    AgentOutput,
    Contradiction,
    ExecutionMeta,
    _weighted_global,
)


def _agent(
    name: str,
    *,
    quality: str = "high",
    payload: dict | None = None,
    chains: list | None = None,
) -> AgentOutput:
    return AgentOutput(
        agent=name,
        agent_version="2.0.0",
        execution_meta=ExecutionMeta(operating_mode="ONLINE", data_quality=quality),
        causal_chains=chains or [],
        payload=payload or {"foo": "bar"},
    )


def _full_bundle() -> dict[str, AgentOutput]:
    return {
        "liquidity": _agent("liquidity"),
        "events": _agent("events"),
        "price-behavior": _agent("price-behavior"),
        "macro": _agent("macro"),
    }


def test_weights_sum_to_one():
    assert sum(QUALITY_WEIGHTS.values()) == pytest.approx(1.0, abs=1e-9)


def test_freshness_perfect_when_all_high_no_stale():
    assert freshness_score(_full_bundle()) == 1.0


def test_freshness_zero_when_no_agents():
    assert freshness_score({}) == 0.0


def test_freshness_penalized_by_stale_meta():
    bundle = _full_bundle()
    bundle["events"] = _agent(
        "events",
        payload={"meta": {"stale_seconds": 1800}},  # full max → -0.25 share
    )
    score = freshness_score(bundle)
    # base = 1.0 (all high), penalty = 1.0/4 from one fully stale agent
    assert score == pytest.approx(0.75, abs=1e-6)


def test_completeness_full_when_all_agents_have_payload():
    assert completeness_score(_full_bundle()) == 1.0


def test_completeness_partial_when_one_agent_missing():
    bundle = _full_bundle()
    bundle.pop("macro")
    assert completeness_score(bundle) == pytest.approx(0.75)


def test_completeness_zero_when_payloads_empty():
    # _agent's `payload or {...}` falls back when payload={}, so build directly.
    def _empty(name: str) -> AgentOutput:
        return AgentOutput(
            agent=name, agent_version="2.0.0",
            execution_meta=ExecutionMeta(operating_mode="ONLINE", data_quality="high"),
            causal_chains=[], payload={},
        )
    bundle = {n: _empty(n) for n in ("liquidity", "events", "price-behavior", "macro")}
    assert completeness_score(bundle) == 0.0


def test_consistency_one_when_no_contradictions():
    assert consistency_score([]) == 1.0


def test_consistency_decreases_with_severity():
    low = [Contradiction(type="x", severity="low", description="d")]
    high = [Contradiction(type="x", severity="high", description="d")]
    assert consistency_score(high) < consistency_score(low)


def test_consistency_clamped_at_zero_with_many_high():
    many = [
        Contradiction(type=str(i), severity="high", description="d")
        for i in range(10)
    ]
    assert consistency_score(many) == 0.0


def test_source_reliability_drops_for_stale_cache():
    bundle = _full_bundle()
    bundle["macro"] = _agent("macro", payload={"meta": {"source": "tradingview_cdp_stale"}})
    score = source_reliability_score(bundle)
    # 3 agents at 1.0 + 1 at 0.7 = 3.7 / 4 = 0.925
    assert score == pytest.approx(0.925, abs=1e-6)


def test_aetheer_validity_neutral_when_no_snapshots():
    assert aetheer_validity_score([]) == 0.5


def test_aetheer_validity_full_when_all_fresh():
    snaps = [
        AetheerSnapshot(instrument="DXY", present=True, age_hours=0.0),
        AetheerSnapshot(instrument="EURUSD", present=True, age_hours=0.2),
    ]
    assert aetheer_validity_score(snaps) == 1.0


def test_aetheer_validity_zero_when_all_missing():
    snaps = [
        AetheerSnapshot(instrument="DXY", present=False),
        AetheerSnapshot(instrument="EURUSD", present=False),
    ]
    assert aetheer_validity_score(snaps) == 0.0


# ─────────── 3 fixture combinations matching the formula at 1e-4 ───────────

def test_global_score_fixture_one_perfect():
    bundle = _full_bundle()
    qb = calculate(agent_outputs=bundle, contradictions=[], aetheer_snapshots=[
        AetheerSnapshot(instrument="DXY", present=True, age_hours=0.0),
    ])
    expected = _weighted_global(1.0, 1.0, 1.0, 1.0, 1.0)
    assert qb.global_score == pytest.approx(expected, abs=1e-4)
    assert qb.global_score == pytest.approx(1.0, abs=1e-4)


def test_global_score_fixture_two_mixed():
    bundle = _full_bundle()
    bundle["events"] = _agent("events", quality="medium", payload={"meta": {"source": "tradingview_cdp_stale"}})
    contradictions = [Contradiction(type="bias_mismatch", severity="medium", description="d")]
    snaps = [
        AetheerSnapshot(instrument="DXY", present=True, age_hours=2.0),  # half
        AetheerSnapshot(instrument="EURUSD", present=False),              # 0
    ]
    qb = calculate(agent_outputs=bundle, contradictions=contradictions, aetheer_snapshots=snaps)

    # Recompute by hand:
    expected_freshness = 3 / 4   # 3 of 4 agents are "high" data_quality
    expected_completeness = 1.0  # all agents have payloads
    # severity medium = 0.5; max_penalty = 4*1.0 = 4 → 1 - 0.5/4 = 0.875
    expected_consistency = 1.0 - 0.5 / 4.0
    # 3 tradingview (1.0) + 1 stale (0.7) → (3+0.7)/4 = 0.925
    expected_source = (3 * 1.0 + 0.7) / 4
    # one snap age_hours=2 → 0.5; one missing → 0; total / 2 = 0.25
    expected_aetheer = 0.5 / 2

    expected = _weighted_global(
        expected_freshness, expected_completeness, expected_consistency,
        expected_source, expected_aetheer,
    )
    assert qb.global_score == pytest.approx(expected, abs=1e-4)


def test_global_score_fixture_three_degraded():
    # Two agents missing entirely; one with a high-severity contradiction.
    bundle = {
        "liquidity": _agent("liquidity", quality="low", payload={"meta": {"source": "fallback"}}),
        "events": _agent("events", quality="medium"),
    }
    contradictions = [
        Contradiction(type="x", severity="high", description="d"),
        Contradiction(type="y", severity="low", description="d"),
    ]
    qb = calculate(agent_outputs=bundle, contradictions=contradictions, aetheer_snapshots=[])

    # Hand math:
    expected_freshness = 0.0     # 0 of 2 are "high"
    expected_completeness = 2 / 4  # only 2 of 4 expected agents present
    # contradictions weight = 1.0 + 0.2 = 1.2; max=4 → 1 - 1.2/4 = 0.7
    expected_consistency = 1.0 - 1.2 / 4.0
    # liquidity → fallback (0.4); events → tradingview default (1.0)
    expected_source = (0.4 + 1.0) / 2
    expected_aetheer = 0.5       # neutral on empty

    expected = _weighted_global(
        expected_freshness, expected_completeness, expected_consistency,
        expected_source, expected_aetheer,
    )
    assert qb.global_score == pytest.approx(expected, abs=1e-4)
    # And this fixture is below the 0.60 floor — sanity-check that the
    # math agrees so cognitive_agent's deterministic-reject logic actually
    # fires here.
    assert qb.global_score < 0.60
