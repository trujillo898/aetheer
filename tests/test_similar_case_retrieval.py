"""Retrieval tests for `TrajectoryStore.retrieve_similar`.

Verifies the acceptance criterion:
    retrieve_similar(query, k=5, min_quality=0.70) returns up to k cases
    with similarity >= min_similarity AND quality.global_score >= min_quality.
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

import trajectory_store as ts  # noqa: E402


SCHEMA_SQL = (MEMORY_DIR / "schema.sql").read_text()


@pytest.fixture
def store(tmp_path: Path) -> ts.TrajectoryStore:
    p = tmp_path / "test.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    return ts.TrajectoryStore(p)


def _response(quality: float, approved: bool = True) -> dict:
    return {
        "approved": approved,
        "operating_mode": "ONLINE" if approved else "OFFLINE",
        "quality": {
            "freshness": quality, "completeness": quality, "consistency": quality,
            "source_reliability": quality, "aetheer_validity": quality,
            "global_score": quality,
        },
        "causal_chains": [],
        "contradictions": [],
        "rejection_reason": None if approved else "operational",
        "synthesis_text": "ok" if approved else None,
        "cost_usd": 0.01,
        "latency_ms": 1000,
        "trace_id": "x",
        "protocol_version": "3.0.0",
    }


def _query(text: str, intent: str = "full_analysis", instruments=("EURUSD",)) -> dict:
    return {
        "query_text": text,
        "query_intent": intent,
        "instruments": list(instruments),
        "timeframes": [],
        "requested_by": "user",
        "trace_id": "ignored",
    }


async def _seed(store: ts.TrajectoryStore, n: int, *, text: str, quality: float, model: str) -> None:
    for i in range(n):
        traj = ts.AnalysisTrajectory(
            trace_id=f"{text[:6]}-{quality}-{model}-{i}",
            query=_query(text),
            response=_response(quality),
            model_routing={"price-behavior": {"model_id": model, "cost_usd": 0.001, "latency_ms": 700}},
        )
        await store.store(traj)


async def test_retrieve_similar_returns_top_k(store: ts.TrajectoryStore) -> None:
    await _seed(store, 8, text="analiza EURUSD overlap london", quality=0.85, model="qwen/qwen3-plus")
    cases = await store.retrieve_similar(
        _query("analiza EURUSD overlap london"),
        k=5, min_quality=0.70,
    )
    assert len(cases) == 5
    assert all(c.similarity >= 0.30 for c in cases)
    # All seeded with same text → similarity should saturate near 1.0
    assert cases[0].similarity > 0.99


async def test_retrieve_similar_filters_by_quality(store: ts.TrajectoryStore) -> None:
    """Cases below the quality floor are dropped even if similar."""
    await _seed(store, 4, text="analiza EURUSD london", quality=0.85, model="qwen/qwen3-plus")
    await _seed(store, 4, text="analiza EURUSD london", quality=0.55, model="nemotron-nano")
    cases = await store.retrieve_similar(
        _query("analiza EURUSD london"), k=10, min_quality=0.70,
    )
    assert len(cases) == 4
    assert all(c.trajectory.response["quality"]["global_score"] >= 0.70 for c in cases)


async def test_retrieve_similar_drops_low_similarity(store: ts.TrajectoryStore) -> None:
    """Disjoint texts get filtered by min_similarity.

    Note: the embedding text always carries `intent:full_analysis instruments:EURUSD`,
    so even fully-disjoint user text shares ~30% cosine via that prefix.
    A realistic min_similarity for "the meaningful content overlaps" is ~0.5.
    """
    await _seed(store, 5, text="cats dogs felines mammals furry pets", quality=0.85, model="qwen/qwen3-plus")
    cases = await store.retrieve_similar(
        _query("treasury yields hawkish fed minutes"),
        k=10, min_quality=0.70, min_similarity=0.55,
    )
    assert cases == []


async def test_retrieve_similar_only_approved(store: ts.TrajectoryStore) -> None:
    """only_approved=True hides OFFLINE diagnostic rows."""
    await _seed(store, 3, text="analiza EURUSD", quality=0.85, model="qwen/qwen3-plus")
    # Add an OFFLINE row that *would* match by text
    off = ts.AnalysisTrajectory(
        trace_id="off-1",
        query=_query("analiza EURUSD"),
        response=_response(0.0, approved=False),
    )
    await store.store(off)

    visible = await store.retrieve_similar(_query("analiza EURUSD"), only_approved=True, min_quality=0.0)
    assert all(c.trajectory.response["approved"] for c in visible)
    assert len(visible) == 3

    visible_all = await store.retrieve_similar(_query("analiza EURUSD"), only_approved=False, min_quality=0.0)
    # OFFLINE has quality=0 — fails default min_quality=0.7. With min_quality=0.0 it shows up.
    assert any(not c.trajectory.response["approved"] for c in visible_all)


async def test_retrieve_similar_intent_filter(store: ts.TrajectoryStore) -> None:
    """same_intent=True restricts by query_intent."""
    await _seed(store, 4, text="analiza EURUSD overlap", quality=0.85, model="qwen/qwen3-plus")
    # Same text, different intent
    traj = ts.AnalysisTrajectory(
        trace_id="dp-1",
        query=_query("analiza EURUSD overlap", intent="data_point"),
        response=_response(0.85),
    )
    await store.store(traj)

    cases = await store.retrieve_similar(
        _query("analiza EURUSD overlap", intent="full_analysis"),
        k=10, min_quality=0.70, same_intent=True,
    )
    assert all(c.trajectory.query["query_intent"] == "full_analysis" for c in cases)
    assert len(cases) == 4

    cases_any = await store.retrieve_similar(
        _query("analiza EURUSD overlap", intent="full_analysis"),
        k=10, min_quality=0.70, same_intent=False,
    )
    intents = {c.trajectory.query["query_intent"] for c in cases_any}
    assert "full_analysis" in intents and "data_point" in intents
