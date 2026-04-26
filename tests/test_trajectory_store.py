"""CRUD tests for `TrajectoryStore`.

We avoid the live embedding endpoint by setting AETHEER_EMBEDDING_STUB=1.
The stub gives deterministic 256-dim vectors so similarity numbers are
reproducible across runs.
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
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    return p


def _approved_response(quality: float = 0.85) -> dict:
    return {
        "approved": True,
        "operating_mode": "ONLINE",
        "quality": {
            "freshness": quality,
            "completeness": quality,
            "consistency": quality,
            "source_reliability": quality,
            "aetheer_validity": quality,
            "global_score": quality,
        },
        "causal_chains": [],
        "contradictions": [],
        "rejection_reason": None,
        "synthesis_text": "DXY alcista ...",
        "cost_usd": 0.012,
        "latency_ms": 8400,
        "trace_id": "t-abc",
        "protocol_version": "3.0.0",
    }


def _quality_floor_rejection() -> dict:
    return {
        "approved": False,
        "operating_mode": "OFFLINE",
        "quality": {
            "freshness": 0.4, "completeness": 0.4, "consistency": 0.4,
            "source_reliability": 0.4, "aetheer_validity": 0.4,
            "global_score": 0.4,
        },
        "causal_chains": [],
        "contradictions": [],
        "rejection_reason": "quality_score_global=0.40 < floor=0.60",
        "synthesis_text": None,
        "cost_usd": 0.003,
        "latency_ms": 1200,
        "trace_id": "t-bad",
        "protocol_version": "3.0.0",
    }


def _offline_kill_switch_response() -> dict:
    return {
        "approved": False,
        "operating_mode": "OFFLINE",
        "quality": {
            "freshness": 0.0, "completeness": 0.0, "consistency": 0.0,
            "source_reliability": 0.0, "aetheer_validity": 0.0,
            "global_score": 0.0,
        },
        "causal_chains": [],
        "contradictions": [],
        "rejection_reason": "KILL_SWITCH: tv-unified offline",
        "synthesis_text": None,
        "cost_usd": 0.0,
        "latency_ms": 200,
        "trace_id": "t-off",
        "protocol_version": "3.0.0",
    }


def _query(text: str = "analiza EURUSD overlap london", instruments=None) -> dict:
    return {
        "query_text": text,
        "query_intent": "full_analysis",
        "instruments": instruments or ["EURUSD"],
        "timeframes": ["H1", "H4"],
        "requested_by": "user",
        "trace_id": "t-abc",
    }


async def test_store_returns_trace_id_and_persists(db_path: Path) -> None:
    store = ts.TrajectoryStore(db_path)
    traj = ts.AnalysisTrajectory(
        trace_id="t-abc",
        query=_query(),
        response=_approved_response(),
        model_routing={"price-behavior": {"model_id": "qwen/qwen3-plus", "cost_usd": 0.001, "latency_ms": 800}},
    )

    new_id = await store.store(traj)
    assert isinstance(new_id, int) and new_id > 0
    assert store.count() == 1
    fetched = store.get_by_trace_id("t-abc")
    assert fetched is not None
    assert fetched.trace_id == "t-abc"
    assert fetched.query["instruments"] == ["EURUSD"]
    assert fetched.response["approved"] is True


async def test_store_rejects_quality_floor_drop(db_path: Path) -> None:
    """approved=False due to quality floor → not persisted (acceptance #4)."""
    store = ts.TrajectoryStore(db_path)
    bad = ts.AnalysisTrajectory(
        trace_id="t-bad",
        query=_query(),
        response=_quality_floor_rejection(),
    )

    assert ts.TrajectoryStore.should_persist(bad.response) is False
    with pytest.raises(ValueError, match="quality-floor rejection"):
        await store.store(bad)
    assert store.count() == 0


async def test_store_keeps_offline_for_diagnostics(db_path: Path) -> None:
    """approved=False due to OFFLINE/KILL_SWITCH → persisted (acceptance #4)."""
    store = ts.TrajectoryStore(db_path)
    off = ts.AnalysisTrajectory(
        trace_id="t-off",
        query=_query(),
        response=_offline_kill_switch_response(),
    )
    assert ts.TrajectoryStore.should_persist(off.response) is True
    new_id = await store.store(off)
    assert new_id > 0
    assert store.count() == 1


async def test_update_feedback(db_path: Path) -> None:
    store = ts.TrajectoryStore(db_path)
    traj = ts.AnalysisTrajectory(
        trace_id="t-fb",
        query=_query(),
        response=_approved_response(),
    )
    await store.store(traj)
    assert store.update_feedback("t-fb", "positive") is True
    refetched = store.get_by_trace_id("t-fb")
    assert refetched is not None
    assert refetched.user_feedback == "positive"
    # Unknown trace → no-op
    assert store.update_feedback("nope", "negative") is False


async def test_stub_embedding_is_deterministic(db_path: Path) -> None:
    """Same input → identical vector. Foundation for the learning test."""
    from embedding import embed_text  # type: ignore[import-not-found]

    e1 = await embed_text("full_analysis EURUSD overlap")
    e2 = await embed_text("full_analysis EURUSD overlap")
    assert e1.vector == e2.vector
    assert e1.dim == e2.dim == 256
    assert e1.model == "stub-hash-v1"
