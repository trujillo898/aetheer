"""D012 — five-factor weighted quality_score calculator.

Pure function over the assembled agent bundle. Inputs are the raw outputs
from `liquidity`, `events`, `price-behavior`, `macro` plus the bundle-level
`ExecutionMeta` (operating_mode, age of TV data, …). Output is a fully
populated `QualityBreakdown`.

Why a separate module: governor is an LLM call; we never want to ask the LLM
"what's the quality score" because:

    1. It would mark its own homework.
    2. The arithmetic is deterministic and trivially testable.
    3. Operating-mode transitions hinge on a hard threshold (0.60); making
       that threshold dependent on a non-deterministic call would be insane.

Each factor returns a float in [0,1]:

    freshness        — fraction of agents with data_quality=="high" plus
                       a stale-cache penalty if any payload carries
                       meta.stale_seconds > 0.
    completeness     — fraction of agents that returned non-empty causal
                       chains (or, for events/liquidity, non-empty payload).
    consistency      — 1 - (severity-weighted contradiction count / max).
    source_reliability — 1.0 if all sources are tradingview live, lower if
                       any agent ran on cache stale or fallback.
    aetheer_validity — 1.0 if every requested instrument has Aetheer
                       indicator data fresh; 0.5 if stale; 0.0 if missing.

The exact heuristics are tunable; the *weights* (D012) are not.
"""
from __future__ import annotations

from dataclasses import dataclass

from agents.schemas import (
    AgentOutput,
    Contradiction,
    QualityBreakdown,
)

EXPECTED_AGENTS = ("liquidity", "events", "price-behavior", "macro")

# Severity → numeric weight (higher = worse impact on consistency).
_SEVERITY_WEIGHT: dict[str, float] = {"low": 0.20, "medium": 0.50, "high": 1.00}

# A contradictions-saturated bundle (4 high-severity) caps consistency near 0.
_MAX_CONSISTENCY_PENALTY = 4 * _SEVERITY_WEIGHT["high"]


@dataclass(frozen=True, slots=True)
class AetheerSnapshot:
    """Per-instrument indicator availability used by `aetheer_validity`."""

    instrument: str
    present: bool
    age_hours: float = 0.0  # 0 means "fresh"; large means stale


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def freshness_score(
    agent_outputs: dict[str, AgentOutput],
    *,
    max_stale_seconds: int = 1800,  # 30 min — matches D013 stale window
) -> float:
    """Higher when no cache-stale data is present and all data_quality is high."""
    if not agent_outputs:
        return 0.0
    high_count = sum(
        1 for o in agent_outputs.values()
        if o.execution_meta.data_quality == "high"
    )
    base = high_count / len(agent_outputs)

    # Penalize per-payload meta.stale_seconds (linear up to max_stale_seconds).
    stale_penalty = 0.0
    for o in agent_outputs.values():
        meta = o.payload.get("meta") or {}
        secs = float(meta.get("stale_seconds") or 0)
        if secs > 0:
            stale_penalty += min(1.0, secs / max_stale_seconds) / len(agent_outputs)

    return _clamp(base - stale_penalty)


def completeness_score(agent_outputs: dict[str, AgentOutput]) -> float:
    """Fraction of expected agents that returned a non-trivial response.

    "Non-trivial" = at least one causal chain OR a non-empty payload.
    Agents that errored out and returned only execution_meta count as 0.
    """
    if not agent_outputs:
        return 0.0
    score = 0.0
    for name in EXPECTED_AGENTS:
        out = agent_outputs.get(name)
        if out is None:
            continue
        has_chains = bool(out.causal_chains)
        has_payload = bool(out.payload) and out.payload != {"meta": {}}
        if has_chains or has_payload:
            score += 1
    return _clamp(score / len(EXPECTED_AGENTS))


def consistency_score(contradictions: list[Contradiction]) -> float:
    """1.0 minus severity-weighted contradiction load, clamped to [0,1]."""
    if not contradictions:
        return 1.0
    weight_sum = sum(_SEVERITY_WEIGHT.get(c.severity, 0.5) for c in contradictions)
    return _clamp(1.0 - weight_sum / _MAX_CONSISTENCY_PENALTY)


def source_reliability_score(agent_outputs: dict[str, AgentOutput]) -> float:
    """Penalize cache-stale or fallback sources.

    Heuristic per agent:
        meta.source == "tradingview" or unset           → 1.0
        meta.source == "tradingview_cdp_stale" / cache  → 0.7
        meta.source other (fallback / unknown)          → 0.4
    """
    if not agent_outputs:
        return 0.0
    total = 0.0
    for o in agent_outputs.values():
        meta = o.payload.get("meta") or {}
        src = (meta.get("source") or "tradingview").lower()
        if "stale" in src or "cache" in src:
            total += 0.7
        elif "tradingview" in src:
            total += 1.0
        else:
            total += 0.4
    return _clamp(total / len(agent_outputs))


def aetheer_validity_score(snapshots: list[AetheerSnapshot]) -> float:
    """Aetheer indicator availability across requested instruments.

    Empty input → 0.5 (neutral — caller didn't request indicator data, so
    its absence is not informative either way).
    """
    if not snapshots:
        return 0.5
    total = 0.0
    for s in snapshots:
        if not s.present:
            continue
        if s.age_hours <= 0.5:
            total += 1.0
        elif s.age_hours <= 4.0:
            total += 0.5
        # else: indicator is too stale to count
    return _clamp(total / len(snapshots))


def calculate(
    *,
    agent_outputs: dict[str, AgentOutput],
    contradictions: list[Contradiction],
    aetheer_snapshots: list[AetheerSnapshot] | None = None,
) -> QualityBreakdown:
    """Run all five factors and assemble a `QualityBreakdown` (global recomputed)."""
    return QualityBreakdown(
        freshness=freshness_score(agent_outputs),
        completeness=completeness_score(agent_outputs),
        consistency=consistency_score(contradictions),
        source_reliability=source_reliability_score(agent_outputs),
        aetheer_validity=aetheer_validity_score(aetheer_snapshots or []),
    )


__all__ = [
    "AetheerSnapshot",
    "EXPECTED_AGENTS",
    "freshness_score",
    "completeness_score",
    "consistency_score",
    "source_reliability_score",
    "aetheer_validity_score",
    "calculate",
]
