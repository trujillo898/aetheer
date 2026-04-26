"""Glue between `AetheerCognitiveAgent` and the trajectory store.

Two responsibilities:

    1. **Persist** a trajectory after a cognitive_analysis call. The cognitive
       agent shouldn't know about embeddings or SQLite; it just hands us the
       query, response, and the routing log it built up. We assemble an
       `AnalysisTrajectory` and let `TrajectoryStore` decide whether to keep it.

    2. **Bias the router** with priors derived from similar past cases. The
       headline use case (acceptance criterion #3): after enough successful
       runs of "full_analysis EURUSD" with one model, a fresh router pick
       favors that model over the route's nominal primary.

The bias surface is a thin `RoutingHint` object that downstream code can
fold into the router's `prefer_cheap` / `budget_remaining_usd` inputs, or
override the route's primary entirely. We don't reach into `AetheerModelRouter`
to mutate state — the router stays pure.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.schemas import CognitiveQuery, CognitiveResponse

logger = logging.getLogger("aetheer.memory_integration")


# The trajectory store lives under mcp-servers/memory/ which has a dash in the
# path so plain `import` doesn't work. Mirror conftest's loader pattern.
def _load_memory_pkg() -> Any:
    if "aetheer_memory_pkg" in sys.modules:
        return sys.modules["aetheer_memory_pkg"]
    pkg_dir = Path(__file__).resolve().parent.parent / "mcp-servers" / "memory"
    if not pkg_dir.exists():
        raise ImportError(f"memory package dir missing: {pkg_dir}")

    # We need both `embedding` and `trajectory_store` importable. The latter
    # imports `embedding` by bare name — easiest path is to add the dir to
    # sys.path and import the modules directly under stable aliases.
    if str(pkg_dir) not in sys.path:
        sys.path.insert(0, str(pkg_dir))

    import embedding as _embedding  # noqa: F401  (registers it for trajectory_store)
    import trajectory_store as _trajectory_store

    # Stash a stable handle so re-imports are free.
    pkg = type("MemoryPkg", (), {})()
    pkg.embedding = _embedding
    pkg.trajectory_store = _trajectory_store
    sys.modules["aetheer_memory_pkg"] = pkg
    return pkg


_MEMORY = _load_memory_pkg()
AnalysisTrajectory = _MEMORY.trajectory_store.AnalysisTrajectory
SimilarCase = _MEMORY.trajectory_store.SimilarCase
TrajectoryStore = _MEMORY.trajectory_store.TrajectoryStore


# ─────────────────────────── public API ───────────────────────────


@dataclass(frozen=True, slots=True)
class RoutingHint:
    """Per-agent override the orchestrator can apply before calling the router.

    `preferred_model_id` (when set) is the OpenRouter model id that scored
    best on similar past cases. The orchestrator can pass it as the route's
    primary by selecting on a route variant, or simply log it for now and
    walk the normal route — both interpretations are valid.
    """

    agent_name: str
    preferred_model_id: str | None = None
    expected_quality: float | None = None
    expected_cost_usd: float | None = None
    sample_size: int = 0


@dataclass(slots=True)
class LearnedPriors:
    """All hints + the raw similar cases that produced them. Returned as a
    bundle so the orchestrator can log/inspect both."""

    similar_cases: list[Any]   # list[SimilarCase] — kept loose to avoid Pydantic re-import
    hints: dict[str, RoutingHint]


class MemoryIntegration:
    """Stateful wrapper around a `TrajectoryStore`.

    Construct once per process; share across requests. All methods are async
    because storing involves an embedding call.
    """

    def __init__(self, store: TrajectoryStore | None = None, *, db_path: str | Path | None = None) -> None:
        if store is None:
            if db_path is None:
                raise ValueError("either store or db_path must be provided")
            store = TrajectoryStore(db_path)
        self._store = store

    # ───── persist ─────

    async def persist(
        self,
        *,
        query: CognitiveQuery,
        response: CognitiveResponse,
        mcp_data_snapshot: dict | None = None,
        model_routing: dict | None = None,
    ) -> int | None:
        """Store the trajectory if policy allows. Returns the row id, or
        None if dropped by policy."""
        traj = AnalysisTrajectory(
            trace_id=query.trace_id,
            query=query.model_dump(),
            response=response.model_dump(),
            mcp_data_snapshot=mcp_data_snapshot or {},
            model_routing=model_routing or {},
        )
        if not self._store.should_persist(traj.response):
            return None
        try:
            return await self._store.store(traj)
        except Exception as e:
            # Memory writes are best-effort — never break the response path.
            logger.warning("trajectory persist failed: %s", e)
            return None

    # ───── retrieve & build priors ─────

    async def get_priors(
        self,
        query: CognitiveQuery,
        *,
        k: int = 10,
        min_quality: float = 0.70,
        min_similarity: float = 0.30,
    ) -> LearnedPriors:
        """Look up similar past cases and reduce them to per-agent hints.

        Reduction rule: per agent, group cases by `model_id`, then pick the
        `model_id` with the highest mean quality_score (weighted by similarity).
        Tie-broken by lower mean cost. We require at least 3 cases per
        recommendation to avoid one-off luck.
        """
        try:
            cases = await self._store.retrieve_similar(
                query.model_dump(),
                k=k,
                min_quality=min_quality,
                min_similarity=min_similarity,
                only_approved=True,
                same_intent=True,
            )
        except Exception as e:
            logger.warning("retrieve_similar failed: %s", e)
            cases = []

        hints = _reduce_to_hints(cases)
        return LearnedPriors(similar_cases=cases, hints=hints)

    # ───── feedback / introspection (mostly for tests) ─────

    def store_handle(self) -> TrajectoryStore:
        return self._store


# ─────────────────────────── reduction logic ───────────────────────────


_MIN_SAMPLES_FOR_HINT = 3


def _reduce_to_hints(cases: list[Any]) -> dict[str, RoutingHint]:
    """Aggregate similar cases into per-agent model preferences.

    Each `case.trajectory.model_routing` is `{agent: {model_id, cost_usd,
    latency_ms, ...}}`. We walk all cases, accumulate stats per (agent,
    model_id), and pick the best model per agent.

    Stats: similarity-weighted mean quality, plain mean cost, count.
    """
    # agent → model_id → {weighted_quality, weight_sum, cost_sum, n}
    accum: dict[str, dict[str, dict[str, float]]] = {}

    for c in cases:
        sim = float(getattr(c, "similarity", 0.0))
        if sim <= 0:
            continue
        traj = c.trajectory
        quality = float((traj.response.get("quality") or {}).get("global_score") or 0.0)
        if quality <= 0:
            continue
        routing = traj.model_routing or {}
        for agent, info in routing.items():
            if not isinstance(info, dict):
                continue
            model_id = info.get("model_id")
            if not model_id:
                continue
            cost = float(info.get("cost_usd") or 0.0)
            slot = accum.setdefault(agent, {}).setdefault(
                model_id, {"weighted_quality": 0.0, "weight_sum": 0.0, "cost_sum": 0.0, "n": 0.0}
            )
            slot["weighted_quality"] += sim * quality
            slot["weight_sum"] += sim
            slot["cost_sum"] += cost
            slot["n"] += 1

    hints: dict[str, RoutingHint] = {}
    for agent, by_model in accum.items():
        viable = [
            (model_id, stats)
            for model_id, stats in by_model.items()
            if stats["n"] >= _MIN_SAMPLES_FOR_HINT and stats["weight_sum"] > 0
        ]
        if not viable:
            continue
        # Sort by weighted-mean quality desc, then cost asc
        viable.sort(
            key=lambda kv: (
                -(kv[1]["weighted_quality"] / kv[1]["weight_sum"]),
                kv[1]["cost_sum"] / kv[1]["n"],
            )
        )
        best_model, best_stats = viable[0]
        hints[agent] = RoutingHint(
            agent_name=agent,
            preferred_model_id=best_model,
            expected_quality=round(best_stats["weighted_quality"] / best_stats["weight_sum"], 4),
            expected_cost_usd=round(best_stats["cost_sum"] / best_stats["n"], 6),
            sample_size=int(best_stats["n"]),
        )
    return hints


__all__ = [
    "AnalysisTrajectory",
    "SimilarCase",
    "TrajectoryStore",
    "MemoryIntegration",
    "RoutingHint",
    "LearnedPriors",
]
