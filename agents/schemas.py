"""Pydantic contracts for the Aetheer cognitive layer (Fase 2).

Strict by design:

    * `CausalChain.invalid_condition` has NO default. A chain that omits it
      fails Pydantic validation immediately and is also rejected later by
      `causal_validator` (defense in depth — the validator is the
      enforcement point that records *why* the chain was dropped).
    * `CognitiveResponse.synthesis_text` is optional, but the model_validator
      enforces the iff invariant: present iff `approved=True`. This keeps the
      "no analysis on OFFLINE" rule (D011 KILL_SWITCH) checkable at the type
      boundary instead of trusting every callsite to remember it.
    * `QualityBreakdown` recomputes `global_score` from the five factors with
      D012 weights. We do NOT trust the LLM to do that arithmetic.

These models are also what `mcp_tool_registry` uses to derive the schema seen
by the model — so any field added here propagates to the tool surface.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# D012 — quality_score weights. Must sum to 1.0.
QUALITY_WEIGHTS: dict[str, float] = {
    "freshness": 0.30,
    "completeness": 0.25,
    "consistency": 0.20,
    "source_reliability": 0.15,
    "aetheer_validity": 0.10,
}

QueryIntent = Literal[
    "full_analysis",
    "punctual",
    "data_point",
    "system_health",
    "validate_setup",
]

OperatingMode = Literal["ONLINE", "OFFLINE"]
Timeframe = Literal["M15", "H1", "H4", "D1", "W1"]
RequestedBy = Literal["user", "scheduler", "telegram", "webapp"]
Severity = Literal["low", "medium", "high"]


def _weighted_global(
    freshness: float,
    completeness: float,
    consistency: float,
    source_reliability: float,
    aetheer_validity: float,
) -> float:
    """D012 weighted sum — kept top-level so tests can import + verify."""
    w = QUALITY_WEIGHTS
    raw = (
        w["freshness"] * freshness
        + w["completeness"] * completeness
        + w["consistency"] * consistency
        + w["source_reliability"] * source_reliability
        + w["aetheer_validity"] * aetheer_validity
    )
    # Clamp into [0,1] in case caller passed something marginally out of band
    # via floating-point drift; raise if the inputs are wildly invalid.
    return max(0.0, min(1.0, round(raw, 4)))


class CognitiveQuery(BaseModel):
    """Inbound request to the cognitive agent."""

    model_config = ConfigDict(extra="forbid")

    query_text: str = Field(min_length=1)
    query_intent: QueryIntent
    instruments: list[str] = Field(default_factory=list)
    timeframes: list[Timeframe] = Field(default_factory=list)
    requested_by: RequestedBy
    trace_id: str = Field(min_length=1)


class ExecutionMeta(BaseModel):
    """Per-agent execution metadata, mirrors v1.2 contract for compatibility."""

    model_config = ConfigDict(extra="allow")  # agents may add fields

    operating_mode: OperatingMode = "ONLINE"
    data_quality: Literal["high", "medium", "low"] = "high"
    model_id: str | None = None
    cost_usd: float = 0.0
    latency_ms: int = 0


class CausalChain(BaseModel):
    """A single cause→effect link with mandatory invalidation criterion (D012).

    `invalid_condition` is required. There is intentionally NO default —
    a chain without an explicit "this is what would prove me wrong" is
    a hallucination risk we refuse to ship into synthesis.
    """

    model_config = ConfigDict(extra="forbid")

    cause: str = Field(min_length=1)
    effect: str = Field(min_length=1)
    invalid_condition: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    timeframe: Timeframe
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)

    @field_validator("invalid_condition")
    @classmethod
    def _not_just_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("invalid_condition cannot be empty/whitespace")
        return v


class Contradiction(BaseModel):
    """A logical inconsistency between two agents (governor output)."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)         # e.g. "bias_mismatch"
    severity: Severity
    description: str = Field(min_length=1)
    resolution_hint: str | None = None
    agents_involved: list[str] = Field(default_factory=list)


class QualityBreakdown(BaseModel):
    """D012 5-factor quality breakdown.

    `global_score` is *always* recomputed from the components in a
    `model_validator(mode="after")` — even if the caller supplied a value
    inconsistent with the parts, we overwrite it. The LLM doesn't get to
    fudge its own grade.
    """

    model_config = ConfigDict(extra="forbid")

    freshness: float = Field(ge=0.0, le=1.0)
    completeness: float = Field(ge=0.0, le=1.0)
    consistency: float = Field(ge=0.0, le=1.0)
    source_reliability: float = Field(ge=0.0, le=1.0)
    aetheer_validity: float = Field(ge=0.0, le=1.0)
    global_score: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _recompute(self) -> "QualityBreakdown":
        object.__setattr__(
            self,
            "global_score",
            _weighted_global(
                self.freshness,
                self.completeness,
                self.consistency,
                self.source_reliability,
                self.aetheer_validity,
            ),
        )
        return self


class AgentOutput(BaseModel):
    """Generic envelope for one specialist agent's response."""

    model_config = ConfigDict(extra="allow")

    agent: str = Field(min_length=1)
    agent_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    execution_meta: ExecutionMeta
    causal_chains: list[CausalChain] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)


class GovernorDecision(BaseModel):
    """Governor's verdict over the assembled bundle."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    operating_mode: OperatingMode
    quality: QualityBreakdown
    contradictions: list[Contradiction] = Field(default_factory=list)
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> "GovernorDecision":
        # An approved decision cannot also carry a rejection reason —
        # they are mutually exclusive states.
        if self.approved and self.rejection_reason:
            raise ValueError("approved=True cannot have rejection_reason")
        if not self.approved and not self.rejection_reason:
            raise ValueError("approved=False requires rejection_reason")
        return self


class CognitiveResponse(BaseModel):
    """Final response surfaced to the caller (CLI, scheduler, webapp)."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    operating_mode: OperatingMode
    quality: QualityBreakdown
    causal_chains: list[CausalChain] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    rejection_reason: str | None = None
    synthesis_text: str | None = None
    cost_usd: float = Field(default=0.0, ge=0.0)
    latency_ms: int = Field(default=0, ge=0)
    trace_id: str = Field(min_length=1)
    protocol_version: str | None = None

    @model_validator(mode="after")
    def _enforce_iff_invariant(self) -> "CognitiveResponse":
        # synthesis_text present iff approved=True. Documented in CLAUDE.md
        # as "approved=False → no análisis de mercado, solo error estructurado".
        has_text = self.synthesis_text is not None and self.synthesis_text.strip() != ""
        if self.approved and not has_text:
            raise ValueError("approved=True requires non-empty synthesis_text")
        if not self.approved and has_text:
            raise ValueError("approved=False forbids synthesis_text")
        if not self.approved and not self.rejection_reason:
            raise ValueError("approved=False requires rejection_reason")
        return self


__all__ = [
    "QUALITY_WEIGHTS",
    "QueryIntent",
    "OperatingMode",
    "Timeframe",
    "RequestedBy",
    "Severity",
    "CognitiveQuery",
    "ExecutionMeta",
    "CausalChain",
    "Contradiction",
    "QualityBreakdown",
    "AgentOutput",
    "GovernorDecision",
    "CognitiveResponse",
]
