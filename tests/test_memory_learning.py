"""Acceptance criterion #3 — learning improves routing.

Setup:
    * Cold start: no trajectories. `MemoryIntegration.get_priors` returns no
      hints, so the router falls back to its nominal route.
    * Seed 20 successful "full_analysis EURUSD" trajectories where one model
      (`nvidia/nemotron-super-v1.5`) consistently scored higher than the
      route's nominal primary (`qwen/qwen3-plus`).
    * Hot path: `get_priors` for a similar query now returns a `RoutingHint`
      pointing at the better-historical model.

This is the "the second time you ask, the system picks differently" test.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ["AETHEER_EMBEDDING_STUB"] = "1"

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = REPO_ROOT / "mcp-servers" / "memory"
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import trajectory_store as ts  # noqa: E402

from agents.memory_integration import MemoryIntegration, RoutingHint  # noqa: E402
from agents.schemas import (  # noqa: E402
    CausalChain,
    CognitiveQuery,
    CognitiveResponse,
    QualityBreakdown,
)


SCHEMA_SQL = (MEMORY_DIR / "schema.sql").read_text()


@pytest.fixture
def integration(tmp_path: Path) -> MemoryIntegration:
    p = tmp_path / "test.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    return MemoryIntegration(db_path=p)


def _query() -> CognitiveQuery:
    return CognitiveQuery(
        query_text="analiza EURUSD durante el solapamiento London-NY",
        query_intent="full_analysis",
        instruments=["EURUSD"],
        timeframes=["H1", "H4"],
        requested_by="user",
        trace_id="learning-test",
    )


def _approved_response(trace_id: str, quality: float) -> CognitiveResponse:
    qb = QualityBreakdown(
        freshness=quality, completeness=quality, consistency=quality,
        source_reliability=quality, aetheer_validity=quality,
    )
    return CognitiveResponse(
        approved=True,
        operating_mode="ONLINE",
        quality=qb,
        causal_chains=[
            CausalChain(
                cause="rates differential narrowed",
                effect="EURUSD H1 structure intact",
                invalid_condition="EURUSD breaks 1.0750",
                confidence=0.7,
                timeframe="H1",
            )
        ],
        synthesis_text="Análisis EURUSD: estructura H1 alcista...",
        cost_usd=0.012,
        latency_ms=8000,
        trace_id=trace_id,
    )


async def test_cold_start_returns_no_hints(integration: MemoryIntegration) -> None:
    priors = await integration.get_priors(_query())
    assert priors.similar_cases == []
    assert priors.hints == {}


async def test_routing_hint_emerges_after_20_trajectories(integration: MemoryIntegration) -> None:
    """The headline acceptance test.

    Seed 20 cases. The "good" model (nemotron-super) scored 0.88 on average,
    the "bad" model (qwen/qwen3-plus) scored 0.72. After seeding, get_priors
    should recommend the nemotron model for `price-behavior`.
    """
    GOOD_MODEL = "nvidia/nemotron-super-v1.5"
    BAD_MODEL = "qwen/qwen3-plus"

    for i in range(10):
        await integration.persist(
            query=_query().model_copy(update={"trace_id": f"good-{i}"}),
            response=_approved_response(f"good-{i}", quality=0.88),
            model_routing={
                "price-behavior": {"model_id": GOOD_MODEL, "cost_usd": 0.0008, "latency_ms": 700},
                "synthesis": {"model_id": "anthropic/claude-sonnet-4.5", "cost_usd": 0.025, "latency_ms": 4000},
            },
        )
    for i in range(10):
        await integration.persist(
            query=_query().model_copy(update={"trace_id": f"bad-{i}"}),
            response=_approved_response(f"bad-{i}", quality=0.72),
            model_routing={
                "price-behavior": {"model_id": BAD_MODEL, "cost_usd": 0.0006, "latency_ms": 800},
                "synthesis": {"model_id": "anthropic/claude-sonnet-4.5", "cost_usd": 0.025, "latency_ms": 4000},
            },
        )

    assert integration.store_handle().count() == 20

    priors = await integration.get_priors(_query(), k=20, min_quality=0.70)
    assert len(priors.similar_cases) >= 10  # seeded > k=10 default earlier; here k=20
    assert "price-behavior" in priors.hints
    hint = priors.hints["price-behavior"]
    assert isinstance(hint, RoutingHint)
    # The router should now prefer the historically-better model
    assert hint.preferred_model_id == GOOD_MODEL, (
        f"expected {GOOD_MODEL}, got {hint.preferred_model_id}; sample={hint.sample_size}"
    )
    assert hint.expected_quality is not None and hint.expected_quality >= 0.85
    assert hint.sample_size >= 10


async def test_quality_floor_rejection_not_persisted(integration: MemoryIntegration) -> None:
    """Quality-floor rejections must not pollute the trajectory store."""
    qb = QualityBreakdown(
        freshness=0.4, completeness=0.4, consistency=0.4,
        source_reliability=0.4, aetheer_validity=0.4,
    )
    bad_response = CognitiveResponse(
        approved=False,
        operating_mode="OFFLINE",
        quality=qb,
        synthesis_text=None,
        rejection_reason=f"quality_score_global={qb.global_score:.2f} < floor=0.60",
        trace_id="bad-1",
    )
    rid = await integration.persist(
        query=_query().model_copy(update={"trace_id": "bad-1"}),
        response=bad_response,
    )
    assert rid is None
    assert integration.store_handle().count() == 0


async def test_offline_kill_switch_is_persisted(integration: MemoryIntegration) -> None:
    """OFFLINE for operational reasons → stored for diagnostics."""
    qb = QualityBreakdown(
        freshness=0.0, completeness=0.0, consistency=0.0,
        source_reliability=0.0, aetheer_validity=0.0,
    )
    off_response = CognitiveResponse(
        approved=False,
        operating_mode="OFFLINE",
        quality=qb,
        synthesis_text=None,
        rejection_reason="KILL_SWITCH: tv-unified offline",
        trace_id="off-1",
    )
    rid = await integration.persist(
        query=_query().model_copy(update={"trace_id": "off-1"}),
        response=off_response,
    )
    assert rid is not None
    assert integration.store_handle().count() == 1


async def test_hint_requires_minimum_samples(integration: MemoryIntegration) -> None:
    """A single lucky run shouldn't produce a hint — guards against noise."""
    await integration.persist(
        query=_query().model_copy(update={"trace_id": "solo"}),
        response=_approved_response("solo", quality=0.95),
        model_routing={"price-behavior": {"model_id": "lucky/model", "cost_usd": 0.001, "latency_ms": 500}},
    )
    priors = await integration.get_priors(_query())
    # 1 sample < threshold (3) → no hint emitted, even though quality is great
    assert "price-behavior" not in priors.hints
