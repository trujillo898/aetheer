from __future__ import annotations

from agents.quality_score import AetheerSnapshot, calculate
from agents.schemas import AgentOutput, ExecutionMeta


def _agent_output(name: str) -> AgentOutput:
    return AgentOutput(
        agent=name,
        agent_version="2.0.0",
        execution_meta=ExecutionMeta(
            operating_mode="ONLINE",
            data_quality="high",
            model_id="m",
            cost_usd=0.0,
            latency_ms=10,
        ),
        causal_chains=[],
        payload={"meta": {"source": "tradingview"}},
    )


def _bundle() -> dict[str, AgentOutput]:
    return {
        "liquidity": _agent_output("liquidity"),
        "events": _agent_output("events"),
        "price-behavior": _agent_output("price-behavior"),
        "macro": _agent_output("macro"),
    }


def test_d015_aetheer_validity_depends_on_indicator_presence() -> None:
    base = _bundle()

    with_indicator = calculate(
        agent_outputs=base,
        contradictions=[],
        aetheer_snapshots=[AetheerSnapshot(instrument="EURUSD", present=True, age_hours=0.1)],
    )
    missing_indicator = calculate(
        agent_outputs=base,
        contradictions=[],
        aetheer_snapshots=[AetheerSnapshot(instrument="EURUSD", present=False, age_hours=0.1)],
    )

    assert with_indicator.aetheer_validity == 1.0
    assert missing_indicator.aetheer_validity == 0.0
    assert with_indicator.global_score > missing_indicator.global_score


def test_d015_stale_indicator_penalizes_to_half_credit() -> None:
    score = calculate(
        agent_outputs=_bundle(),
        contradictions=[],
        aetheer_snapshots=[AetheerSnapshot(instrument="DXY", present=True, age_hours=2.0)],
    )
    assert score.aetheer_validity == 0.5
