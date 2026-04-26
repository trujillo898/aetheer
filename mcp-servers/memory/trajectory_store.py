"""Trajectory store: full analysis cases with semantic retrieval.

A *trajectory* is the complete record of one cognitive_analysis run:
query, mcp data snapshot, model routing decisions, agent outputs / causal
chains / quality, and (later) user feedback. We persist these so the
router can learn from past outcomes — when a similar case shows up, we
can bias model selection toward routes that scored well.

This module owns:

    * Pydantic schemas (AnalysisTrajectory, SimilarCase) — used by both
      the MCP `server.py` tools and the `agents/memory_integration.py`
      glue layer.
    * `TrajectoryStore` — CRUD + KNN cosine retrieval against the SQLite
      tables `trajectories` and `trajectory_embeddings`.

Storage policy (acceptance criterion #4):

    * Trajectories with `approved=False` AND a quality-related
      rejection_reason are **not** persisted (they're noise — synthesis
      didn't even run). The marker is a rejection_reason that begins with
      `quality_score_global=` (the deterministic floor message emitted by
      `cognitive_agent._build_response`).
    * Trajectories with `approved=False` AND `operating_mode=OFFLINE` for
      *operational* reasons (KILL_SWITCH, BUDGET_EXCEEDED, etc.) ARE kept
      — they're useful for diagnosing infra outages.
    * Approved trajectories are always kept.

Embeddings are computed at store-time from a deterministic text
representation of the query (intent + text + instruments + timeframes)
so the same query repeated tomorrow lands close in vector space.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from embedding import (
    Embedding,
    cosine,
    embed_text,
    pack_vector,
    unpack_vector,
)

logger = logging.getLogger("aetheer.memory.trajectory")

UserFeedback = Literal["positive", "negative", "mixed", "none"]


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────── schemas ───────────────────────────


class AnalysisTrajectory(BaseModel):
    """One complete analysis run, persistable + queryable.

    `query` and `response` are kept as plain dicts here (not strict
    `CognitiveQuery` / `CognitiveResponse`) so this module can be imported
    by the MCP server without dragging in the whole `agents` package. The
    glue layer in `agents/memory_integration.py` validates the typed
    versions before / after passing through this store.
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(min_length=1)
    query: dict
    response: dict
    mcp_data_snapshot: dict = Field(default_factory=dict)
    model_routing: dict = Field(default_factory=dict)
    user_feedback: UserFeedback = "none"
    created_at: str = Field(default_factory=_now_utc_iso)


class SimilarCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trajectory: AnalysisTrajectory
    similarity: float = Field(ge=-1.0, le=1.0)


# ─────────────────────────── store ───────────────────────────


class TrajectoryStore:
    """CRUD + KNN over `trajectories` + `trajectory_embeddings`.

    Cosine search is done in Python — the dataset is small (thousands of
    rows, not millions) and adding a vector index would mean a hard dep
    on `sqlite-vss` which doesn't ship on every platform. When the store
    crosses ~50k rows we'll revisit; until then linear scan is fine.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    # ───── connection helper ─────

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA journal_mode = WAL")
        return c

    # ───── policy: should this be persisted? ─────

    @staticmethod
    def should_persist(response: dict) -> bool:
        """Acceptance criterion #4 — drop quality-floor rejections only.

        - approved=True              → keep
        - approved=False, OFFLINE
            and rejection_reason starts with 'quality_score_global=' → drop
        - approved=False, OFFLINE for operational reasons → keep (diagnostic)
        """
        if response.get("approved"):
            return True
        reason = (response.get("rejection_reason") or "").strip()
        if reason.startswith("quality_score_global="):
            return False
        return True

    # ───── store ─────

    async def store(
        self,
        trajectory: AnalysisTrajectory,
        *,
        embedding_text: str | None = None,
    ) -> int:
        """Persist a trajectory and its embedding. Returns the row id.

        `embedding_text` lets the caller override what gets embedded;
        default is `_query_to_embedding_text(trajectory.query)`.
        """
        if not self.should_persist(trajectory.response):
            raise ValueError(
                "trajectory rejected by store policy "
                "(quality-floor rejection — caller should not persist)"
            )

        text = embedding_text or _query_to_embedding_text(trajectory.query)
        emb = await embed_text(text)

        return self._insert(trajectory, emb)

    def _insert(self, trajectory: AnalysisTrajectory, emb: Embedding) -> int:
        query = trajectory.query
        response = trajectory.response
        instruments = query.get("instruments") or []
        instruments_csv = ",".join(str(i).upper() for i in instruments)
        approved = 1 if response.get("approved") else 0
        operating_mode = response.get("operating_mode") or "OFFLINE"
        quality_score = float(
            (response.get("quality") or {}).get("global_score") or 0.0
        )

        conn = self._conn()
        try:
            cur = conn.execute(
                """INSERT INTO trajectories (
                    trace_id, query_intent, instruments_csv,
                    query_json, response_json, mcp_data_json, routing_json,
                    approved, operating_mode, quality_score, user_feedback,
                    created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trajectory.trace_id,
                    query.get("query_intent", "unknown"),
                    instruments_csv,
                    json.dumps(query, ensure_ascii=False),
                    json.dumps(response, ensure_ascii=False),
                    json.dumps(trajectory.mcp_data_snapshot, ensure_ascii=False),
                    json.dumps(trajectory.model_routing, ensure_ascii=False),
                    approved,
                    operating_mode,
                    quality_score,
                    trajectory.user_feedback,
                    trajectory.created_at,
                ),
            )
            traj_id = cur.lastrowid
            conn.execute(
                """INSERT INTO trajectory_embeddings (
                    trajectory_id, model, dim, vector, norm
                ) VALUES (?,?,?,?,?)""",
                (
                    traj_id,
                    emb.model,
                    emb.dim,
                    pack_vector(emb.vector),
                    emb.norm,
                ),
            )
            conn.commit()
            return int(traj_id)
        finally:
            conn.close()

    # ───── retrieve ─────

    async def retrieve_similar(
        self,
        query: dict,
        *,
        k: int = 5,
        min_quality: float = 0.70,
        min_similarity: float = 0.30,
        only_approved: bool = True,
        same_intent: bool = True,
    ) -> list[SimilarCase]:
        """Return up to `k` similar cases meeting the quality floor.

        Filters:
            * `min_quality`  — quality_score >= floor
            * `only_approved`— skip non-approved trajectories
            * `same_intent`  — restrict to query_intent match (cheap pre-filter)
            * `min_similarity` — cosine threshold; cases below are dropped

        Sorted by similarity desc.
        """
        text = _query_to_embedding_text(query)
        emb = await embed_text(text)
        return self._knn(
            emb,
            query.get("query_intent"),
            k=k,
            min_quality=min_quality,
            min_similarity=min_similarity,
            only_approved=only_approved,
            same_intent=same_intent,
        )

    def _knn(
        self,
        query_emb: Embedding,
        query_intent: str | None,
        *,
        k: int,
        min_quality: float,
        min_similarity: float,
        only_approved: bool,
        same_intent: bool,
    ) -> list[SimilarCase]:
        conn = self._conn()
        try:
            sql = (
                "SELECT t.*, e.dim AS emb_dim, e.vector AS emb_vec, "
                "e.norm AS emb_norm, e.model AS emb_model "
                "FROM trajectories t "
                "JOIN trajectory_embeddings e ON e.trajectory_id = t.id "
                "WHERE 1=1"
            )
            params: list[Any] = []
            if only_approved:
                sql += " AND t.approved = 1"
            if min_quality > 0:
                sql += " AND t.quality_score >= ?"
                params.append(min_quality)
            if same_intent and query_intent:
                sql += " AND t.query_intent = ?"
                params.append(query_intent)
            sql += " AND e.model = ?"
            params.append(query_emb.model)

            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            vec = unpack_vector(row["emb_vec"], row["emb_dim"])
            sim = cosine(
                query_emb.vector, vec,
                query_emb.norm, row["emb_norm"],
            )
            if sim >= min_similarity:
                scored.append((sim, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        scored = scored[:k]

        return [
            SimilarCase(
                trajectory=_row_to_trajectory(row),
                similarity=round(sim, 4),
            )
            for sim, row in scored
        ]

    # ───── feedback ─────

    def update_feedback(self, trace_id: str, feedback: UserFeedback) -> bool:
        conn = self._conn()
        try:
            cur = conn.execute(
                "UPDATE trajectories SET user_feedback = ? WHERE trace_id = ?",
                (feedback, trace_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ───── introspection (used by tests / memory_integration) ─────

    def count(self) -> int:
        conn = self._conn()
        try:
            r = conn.execute("SELECT COUNT(*) AS n FROM trajectories").fetchone()
            return int(r["n"])
        finally:
            conn.close()

    def get_by_trace_id(self, trace_id: str) -> AnalysisTrajectory | None:
        conn = self._conn()
        try:
            r = conn.execute(
                "SELECT * FROM trajectories WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
            if not r:
                return None
            return _row_to_trajectory(r)
        finally:
            conn.close()


# ─────────────────────────── helpers ───────────────────────────


def _query_to_embedding_text(query: dict) -> str:
    """Deterministic text representation of a query for embedding.

    Order matters for stub determinism — keep it stable. Field choices:
    intent (most discriminating), instruments, timeframes, then the raw
    query_text. We deliberately omit trace_id / requested_by — they're
    metadata, not topical signal.
    """
    parts = [
        f"intent:{query.get('query_intent', 'unknown')}",
    ]
    instruments = query.get("instruments") or []
    if instruments:
        parts.append("instruments:" + ",".join(sorted(str(i).upper() for i in instruments)))
    tfs = query.get("timeframes") or []
    if tfs:
        parts.append("timeframes:" + ",".join(sorted(str(t) for t in tfs)))
    text = (query.get("query_text") or "").strip()
    if text:
        parts.append(text)
    return " | ".join(parts)


def _row_to_trajectory(row: sqlite3.Row) -> AnalysisTrajectory:
    return AnalysisTrajectory(
        trace_id=row["trace_id"],
        query=json.loads(row["query_json"]),
        response=json.loads(row["response_json"]),
        mcp_data_snapshot=json.loads(row["mcp_data_json"] or "{}"),
        model_routing=json.loads(row["routing_json"] or "{}"),
        user_feedback=row["user_feedback"] or "none",
        created_at=row["created_at"],
    )


__all__ = [
    "AnalysisTrajectory",
    "SimilarCase",
    "TrajectoryStore",
    "UserFeedback",
]
