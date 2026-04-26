"""Per-agent model routing with fallback, cost gating and cache-stale hints.

The router answers ONE question per call:

    For this agent, on this request, within this budget context,
    which model do I try first, and which list of fallbacks do I walk
    if it 4xx/5xx/times out / exceeds max_cost_per_call?

It does NOT perform the actual chat completion — that's `OpenRouterClient`'s
job. Separation of concerns keeps the router unit-testable without HTTP mocks.

Routing inputs:
    - agent_name: one of the 7 agents in docs/AGENT_PROTOCOL.json
    - context_tokens: estimated prompt size (for future context-aware selection)
    - cost_monitor: CostMonitor instance → allows budget-aware downgrade
    - prefer_cheap: forced downgrade (e.g. during scheduled-off-hours analysis)

Pricing table source: OpenRouter's /models endpoint (pinned snapshot below).
Prices are $ per 1M tokens. Refresh by re-running scripts/refresh_pricing.py
(Fase 1.5 — not yet implemented; pin date in LAST_PRICING_REFRESH).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger("aetheer.router")

LAST_PRICING_REFRESH = "2026-04-24"  # update with scripts/refresh_pricing.py

AgentName = Literal[
    "context-orchestrator",
    "attention",
    "liquidity",
    "events",
    "price-behavior",
    "macro",
    "synthesis",
    "governor",
]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str                 # OpenRouter model id, e.g. "anthropic/claude-sonnet-4.5"
    input_per_m: float      # USD per 1M prompt tokens
    output_per_m: float     # USD per 1M completion tokens

    def estimate_cost(self, prompt_tokens: int, output_tokens: int) -> float:
        return (prompt_tokens * self.input_per_m + output_tokens * self.output_per_m) / 1_000_000


@dataclass(frozen=True, slots=True)
class AgentRoute:
    primary: ModelSpec
    fallbacks: tuple[ModelSpec, ...]
    max_cost_per_call: float        # USD — hard cap; router refuses if est > cap
    quality_threshold: float        # informational; governor owns the gate
    cheap_override: ModelSpec | None = None  # used when prefer_cheap=True


# Pricing snapshot (2026-04-24). Source: openrouter.ai/models.
# These can be overridden via environment variables for development (e.g. to use FREE models).
def _get_spec(name: str, default_id: str, in_m: float, out_m: float) -> ModelSpec:
    return ModelSpec(
        id=os.getenv(f"AETHEER_MODEL_{name}", default_id),
        input_per_m=float(os.getenv(f"AETHEER_MODEL_{name}_IN", in_m)),
        output_per_m=float(os.getenv(f"AETHEER_MODEL_{name}_OUT", out_m)),
    )

_CLAUDE_SONNET_45 = _get_spec("SONNET", "anthropic/claude-sonnet-4.5", 3.00, 15.00)
_CLAUDE_HAIKU_45 = _get_spec("HAIKU", "anthropic/claude-haiku-4.5", 0.80, 4.00)
_QWEN_PLUS = _get_spec("QWEN_PLUS", "qwen/qwen3-plus", 0.325, 1.95)
_NEMOTRON_SUPER = _get_spec("NEMOTRON_SUPER", "nvidia/nemotron-super-v1.5", 0.30, 0.60)
_NEMOTRON_NANO = _get_spec("NEMOTRON_NANO", "nvidia/nemotron-nano-9b-v2", 0.04, 0.16)
_GEMINI_FLASH = _get_spec("GEMINI_FLASH", "google/gemini-2.5-flash", 0.30, 2.50)
_GPT5_NANO = _get_spec("GPT5_NANO", "openai/gpt-5-nano", 0.05, 0.40)


DEFAULT_ROUTES: dict[str, AgentRoute] = {
    # Attention: fast analyzer to decide focus; cheap and reliable
    "attention": AgentRoute(
        primary=_GEMINI_FLASH,
        fallbacks=(_QWEN_PLUS, _NEMOTRON_SUPER),
        max_cost_per_call=0.005,
        quality_threshold=0.80,
        cheap_override=_NEMOTRON_NANO,
    ),
    # Synthesis: narrative quality matters; pay for Sonnet, fall back to Nemotron
    "synthesis": AgentRoute(
        primary=_CLAUDE_SONNET_45,
        fallbacks=(_NEMOTRON_SUPER, _QWEN_PLUS),
        max_cost_per_call=0.05,
        quality_threshold=0.85,
        cheap_override=_QWEN_PLUS,
    ),
    # Price behavior: technical + indicator reasoning; Qwen is good and cheap
    "price-behavior": AgentRoute(
        primary=_QWEN_PLUS,
        fallbacks=(_NEMOTRON_SUPER,),
        max_cost_per_call=0.02,
        quality_threshold=0.80,
        cheap_override=_NEMOTRON_NANO,
    ),
    # Macro: breadth + reasoning; Nemotron super gives best $/quality
    "macro": AgentRoute(
        primary=_NEMOTRON_SUPER,
        fallbacks=(_QWEN_PLUS, _GEMINI_FLASH),
        max_cost_per_call=0.02,
        quality_threshold=0.80,
        cheap_override=_NEMOTRON_NANO,
    ),
    # Liquidity: shallow structured output; nano is enough
    "liquidity": AgentRoute(
        primary=_NEMOTRON_NANO,
        fallbacks=(_QWEN_PLUS,),
        max_cost_per_call=0.005,
        quality_threshold=0.75,
        cheap_override=_NEMOTRON_NANO,
    ),
    # Events: long-ish calendar summaries; Gemini Flash handles well
    "events": AgentRoute(
        primary=_GEMINI_FLASH,
        fallbacks=(_QWEN_PLUS, _NEMOTRON_SUPER),
        max_cost_per_call=0.01,
        quality_threshold=0.80,
        cheap_override=_NEMOTRON_NANO,
    ),
    # Governor: short, deterministic validation; needs to be cheap & reliable
    "governor": AgentRoute(
        primary=_GPT5_NANO,
        fallbacks=(_CLAUDE_HAIKU_45,),
        max_cost_per_call=0.005,
        quality_threshold=0.90,
        cheap_override=_GPT5_NANO,
    ),
    # Orchestrator: routing decisions + pruning; keep it cheap
    "context-orchestrator": AgentRoute(
        primary=_NEMOTRON_NANO,
        fallbacks=(_QWEN_PLUS,),
        max_cost_per_call=0.005,
        quality_threshold=0.80,
        cheap_override=_NEMOTRON_NANO,
    ),
}


@dataclass(slots=True)
class ModelSelection:
    primary: ModelSpec
    fallbacks: list[ModelSpec] = field(default_factory=list)
    estimated_cost_usd: float = 0.0
    reason: str = ""


class BudgetExceededError(RuntimeError):
    """Every model on the route exceeds max_cost_per_call. Caller should abort."""


class AetheerModelRouter:
    """Stateless router over `DEFAULT_ROUTES` with cost-aware downgrade."""

    def __init__(
        self,
        routes: dict[str, AgentRoute] | None = None,
        *,
        default_expected_output_tokens: int = 1000,
    ) -> None:
        self._routes = routes if routes is not None else DEFAULT_ROUTES
        self._default_output_tokens = default_expected_output_tokens

    def available_agents(self) -> list[str]:
        return sorted(self._routes)

    def select(
        self,
        *,
        agent_name: str,
        context_tokens: int,
        expected_output_tokens: int | None = None,
        prefer_cheap: bool = False,
        budget_remaining_usd: float | None = None,
    ) -> ModelSelection:
        """Pick primary + ordered fallbacks that fit within the call cap.

        Raises BudgetExceededError if every model on the route exceeds
        max_cost_per_call for this estimated token usage.
        """
        route = self._routes.get(agent_name)
        if route is None:
            raise KeyError(f"no route registered for agent '{agent_name}'")

        output_tokens = expected_output_tokens or self._default_output_tokens

        candidates: list[ModelSpec] = []
        if prefer_cheap and route.cheap_override is not None:
            candidates.append(route.cheap_override)
        candidates.append(route.primary)
        candidates.extend(m for m in route.fallbacks if m not in candidates)

        viable: list[tuple[ModelSpec, float]] = []
        rejected: list[tuple[str, float]] = []
        for spec in candidates:
            est = spec.estimate_cost(context_tokens, output_tokens)
            if est > route.max_cost_per_call:
                rejected.append((spec.id, est))
                continue
            if budget_remaining_usd is not None and est > budget_remaining_usd:
                rejected.append((spec.id, est))
                continue
            viable.append((spec, est))

        if not viable:
            raise BudgetExceededError(
                f"agent={agent_name} all candidates exceed caps "
                f"(max_per_call={route.max_cost_per_call}, "
                f"budget_remaining={budget_remaining_usd}); rejected={rejected}"
            )

        primary_spec, primary_cost = viable[0]
        reason_parts = [f"primary={primary_spec.id}@${primary_cost:.5f}"]
        if prefer_cheap:
            reason_parts.append("prefer_cheap=on")
        if rejected:
            reason_parts.append(f"skipped={[m for m, _ in rejected]}")
        return ModelSelection(
            primary=primary_spec,
            fallbacks=[m for m, _ in viable[1:]],
            estimated_cost_usd=primary_cost,
            reason=" ".join(reason_parts),
        )
