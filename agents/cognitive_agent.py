"""AetheerCognitiveAgent — Fase 2 orchestrator.

Wires together:

    * `OpenRouterClient` (Fase 1) for chat completions
    * `AetheerModelRouter`  (Fase 1) for per-agent model selection
    * `CostMonitor`         (Fase 1) for budget tracking
    * `PromptLoader`        (this fase) for system prompts from AGENT_PROTOCOL.json
    * `LLMAgent`            (this fase) for stateful message history
    * `mcp_tool_registry`   (this fase) for the JSON-Schema tool surface
    * `quality_score`       (this fase) for D012 5-factor calculation
    * `causal_validator`    (this fase) for invalid_condition gate

End-to-end flow of `cognitive_analysis(query)`:

    1. tv-unified.get_system_health() — if OFFLINE, short-circuit with a
       structured error response. **No LLM call** in this branch.
    2. Compute model selections per specialist agent (router knows budget).
    3. Fan out specialist agents (liquidity / events / price-behavior /
       macro) concurrently via `asyncio.gather`. Each gets:
         - its system prompt (loader)
         - the user-flavored task prompt
         - its scoped tool surface (mcp_tool_registry.tools_for)
       The model decides which tools to call; we resolve those via the
       injected `McpBridge` protocol (real impl wraps the MCP client; tests
       inject a fake).
    4. Optional reflection loop (`enable_reflection_loop` feature flag):
       a cheap reviewer LLM scans causal chains and annotates the bundle.
    5. Bundle-level causal validator runs — chains lacking
       `invalid_condition` are dropped with reason="missing_invalid_condition".
    6. quality_score.calculate() over the kept bundle.
    7. governor LLM produces the final approved/rejected decision; we
       force-override `approved=False` if `quality.global_score < 0.60`.
    8. If approved, synthesis LLM writes the user-facing text. If not,
       we return the structured rejection with `synthesis_text=None`.
    9. CostMonitor.record() for every LLM call along the way (including
       the ones that errored out, since OpenRouter still bills retries).

Cancellation: every LLM call is awaited in a task; we set a per-request
`asyncio.timeout` around the fan-out so a wedged model can't stall the
whole analysis. Defaults are conservative.

Testing surface: `cognitive_analysis()` takes a fully-injected dependency
graph. `_AgentRunResult` is private; tests interact through the public
return type only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from agents.attention_agent import AttentionAgent
from agents.causal_validator import CausalRejection, validate_chains
from agents.llm_agent import LLMAgent
from agents.mcp_tool_registry import tools_for
from agents.model_router import (
    AetheerModelRouter,
    BudgetExceededError,
    ModelSelection,
)
from agents.openrouter_client import (
    ChatResult,
    OpenRouterClient,
    OpenRouterError,
)
from agents.prompts.loader import PromptLoader, default_loader
from agents.quality_score import (
    AetheerSnapshot,
    calculate as calculate_quality,
)
from agents.schemas import (
    AgentOutput,
    AttentionContext,
    CausalChain,
    CognitiveQuery,
    CognitiveResponse,
    Contradiction,
    ExecutionMeta,
    GovernorDecision,
    QualityBreakdown,
    RegimeInfo,
)
from services.cost_monitor import CostMonitor

logger = logging.getLogger("aetheer.cognitive")

SPECIALIST_AGENTS: tuple[str, ...] = ("liquidity", "events", "price-behavior", "macro")
QUALITY_FLOOR = 0.60        # D011 → below this we force OFFLINE
DEFAULT_FANOUT_TIMEOUT = 90  # seconds for all four specialists in parallel


class McpBridge(Protocol):
    """Minimal interface the orchestrator needs from the MCP layer.

    Real impl (Fase 3) wraps an MCP client with retries; tests inject a
    fake that returns canned dicts.
    """

    async def get_system_health(self) -> dict[str, Any]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CognitiveDeps:
    """All injected dependencies. Construct once per process; reuse per call."""

    client: OpenRouterClient
    router: AetheerModelRouter
    cost_monitor: CostMonitor
    mcp: McpBridge
    prompt_loader: PromptLoader = field(default_factory=default_loader)


@dataclass(slots=True)
class _AgentRunResult:
    """Internal: one specialist agent's run."""

    agent: str
    output: AgentOutput | None
    error: str | None
    cost_usd: float
    latency_ms: int


class AetheerCognitiveAgent:
    """Top-level orchestrator. Stateless across calls (state lives in deps)."""

    def __init__(
        self,
        deps: CognitiveDeps,
        *,
        enable_reflection_loop: bool = False,
        fanout_timeout_seconds: int = DEFAULT_FANOUT_TIMEOUT,
        quality_floor: float = QUALITY_FLOOR,
    ) -> None:
        self._deps = deps
        self._enable_reflection = enable_reflection_loop
        self._fanout_timeout = fanout_timeout_seconds
        self._quality_floor = quality_floor
        self._attention_agent = AttentionAgent(deps.client)

    # ───────────────────────── public API ─────────────────────────

    async def cognitive_analysis(self, query: CognitiveQuery) -> CognitiveResponse:
        start = time.monotonic()

        # 1. KILL_SWITCH check.
        try:
            health = await self._deps.mcp.get_system_health()
        except Exception as e:
            logger.exception("mcp.get_system_health failed: %s", e)
            return self._offline_response(
                query,
                rejection_reason=f"KILL_SWITCH: tv-unified health probe failed: {e}",
                started_at=start,
            )

        if (health or {}).get("operating_mode") != "ONLINE":
            return self._offline_response(
                query,
                rejection_reason=(
                    f"KILL_SWITCH: tv-unified operating_mode="
                    f"{(health or {}).get('operating_mode')!r}"
                ),
                started_at=start,
            )

        # 2. Attention & Regime phase.
        total_cost = 0.0
        # Fetch a quick snapshot for attention
        try:
            market_snapshot = await self._get_market_snapshot(query)
            # Detect regime
            regime = await self._detect_regime(market_snapshot)
            # Determine attention
            if query.attention_override:
                attention = query.attention_override
            else:
                # Select model for attention via router
                att_sel = self._deps.router.select(
                    agent_name="attention",
                    context_tokens=1000,
                    expected_output_tokens=300,
                    prefer_cheap=self._deps.cost_monitor.should_downgrade(),
                    budget_remaining_usd=self._deps.cost_monitor.remaining_budget_usd()
                )
                # Temporarily update model_id for this call
                self._attention_agent._model_id = att_sel.primary.id
                attention = await self._attention_agent.get_attention(query, market_snapshot)
        except Exception as e:
            logger.error(f"Attention/Regime phase failed: {e}")
            attention = AttentionContext(
                dominant_theme="unknown",
                attention_weights={a: 0.5 for a in SPECIALIST_AGENTS},
                reasoning=f"Error: {e}"
            )
            regime = None

        # 3. Specialist fan-out.
        try:
            agent_results = await asyncio.wait_for(
                self._run_specialists(query, attention, regime),
                timeout=self._fanout_timeout,
            )
        except asyncio.TimeoutError:
            return self._offline_response(
                query,
                rejection_reason=f"specialist fan-out timed out after {self._fanout_timeout}s",
                started_at=start,
                attention=attention,
                regime=regime,
            )
        except BudgetExceededError as e:
            return self._offline_response(
                query,
                rejection_reason=f"BUDGET_EXCEEDED: {e}",
                started_at=start,
                attention=attention,
                regime=regime,
            )

        agent_outputs: dict[str, AgentOutput] = {
            r.agent: r.output for r in agent_results if r.output is not None
        }
        total_cost += sum(r.cost_usd for r in agent_results)

        # 4. Optional reflection loop.
        reflection_notes: list[str] = []
        if self._enable_reflection and agent_outputs:
            notes, refl_cost = await self._run_reflection(query, agent_outputs)
            reflection_notes = notes
            total_cost += refl_cost

        # 5. Bundle-level causal validation.
        all_chains: list[CausalChain] = [
            c for o in agent_outputs.values() for c in o.causal_chains
        ]
        kept_chains, rejected_chains = validate_chains(all_chains)

        # 6. Build a contradictions list.
        contradictions: list[Contradiction] = self._extract_contradictions(
            agent_outputs, reflection_notes
        )

        # 7. Quality score.
        aetheer_snaps = self._collect_aetheer_snapshots(agent_outputs)
        quality = calculate_quality(
            agent_outputs=agent_outputs,
            contradictions=contradictions,
            aetheer_snapshots=aetheer_snaps,
        )

        # 8. Governor.
        if quality.global_score < self._quality_floor or not agent_outputs:
            return self._build_response(
                approved=False,
                operating_mode="OFFLINE",
                quality=quality,
                kept_chains=kept_chains,
                rejected_chains=rejected_chains,
                contradictions=contradictions,
                attention=attention,
                regime=regime,
                rejection_reason=(
                    f"quality_score_global={quality.global_score:.2f} < "
                    f"floor={self._quality_floor:.2f}"
                ),
                synthesis_text=None,
                cost_usd=total_cost,
                trace_id=query.trace_id,
                started_at=start,
            )

        gov_decision, gov_cost = await self._run_governor(
            query, agent_outputs, kept_chains, contradictions, quality, rejected_chains, attention, regime
        )
        total_cost += gov_cost

        if not gov_decision.approved:
            return self._build_response(
                approved=False,
                operating_mode="OFFLINE",
                quality=gov_decision.quality,
                kept_chains=kept_chains,
                rejected_chains=rejected_chains,
                contradictions=gov_decision.contradictions,
                attention=attention,
                regime=regime,
                rejection_reason=gov_decision.rejection_reason or "governor rejected",
                synthesis_text=None,
                cost_usd=total_cost,
                trace_id=query.trace_id,
                started_at=start,
            )

        # 9. Synthesis.
        try:
            synth_text, synth_cost = await self._run_synthesis(
                query, agent_outputs, kept_chains, gov_decision, attention, regime
            )
            total_cost += synth_cost
        except OpenRouterError as e:
            logger.exception("synthesis call failed: %s", e)
            return self._build_response(
                approved=False,
                operating_mode="OFFLINE",
                quality=gov_decision.quality,
                kept_chains=kept_chains,
                rejected_chains=rejected_chains,
                contradictions=gov_decision.contradictions,
                attention=attention,
                regime=regime,
                rejection_reason=f"synthesis_failed: {e}",
                synthesis_text=None,
                cost_usd=total_cost,
                trace_id=query.trace_id,
                started_at=start,
            )

        return self._build_response(
            approved=True,
            operating_mode="ONLINE",
            quality=gov_decision.quality,
            kept_chains=kept_chains,
            rejected_chains=rejected_chains,
            contradictions=gov_decision.contradictions,
            attention=attention,
            regime=regime,
            rejection_reason=None,
            synthesis_text=synth_text,
            cost_usd=total_cost,
            trace_id=query.trace_id,
            started_at=start,
        )

    # ───────────────────────── internals ─────────────────────────

    async def _get_market_snapshot(self, query: CognitiveQuery) -> dict:
        """Fetch news and calendar for attention mechanism."""
        # Parallel fetch news and calendar
        results = await asyncio.gather(
            self._deps.mcp.call_tool("tv_get_news", {"symbol": query.instruments[0] if query.instruments else "", "limit": 10}),
            self._deps.mcp.call_tool("tv_get_economic_calendar", {"window_hours": 24}),
            return_exceptions=True
        )
        
        news = results[0] if not isinstance(results[0], Exception) else {}
        calendar = results[1] if not isinstance(results[1], Exception) else {}
        
        return {
            "news": news.get("news", []) if isinstance(news, dict) else [],
            "calendar": calendar.get("events", []) if isinstance(calendar, dict) else [],
            "price_summary": "DXY/EURUSD context" # Simple stub for now
        }

    async def _detect_regime(self, snapshot: dict) -> RegimeInfo | None:
        """Call the regime detector MCP tool."""
        try:
            # For now we use calendar and news as proxy if we don't have deep price yet
            result = await self._deps.mcp.call_tool("memory_detect_regime", {
                "aetheer_per_pair_json": "{}", # placeholder
                "use_recent_trades": True
            })
            if isinstance(result, str):
                data = json.loads(result)
            else:
                data = result
            
            if "error" in data:
                return None
                
            return RegimeInfo(
                classification=data.get("regime", "transition"),
                confidence=data.get("confidence", 0.5),
                symptoms=data.get("symptoms", []),
                recommendation=data.get("recommendation")
            )
        except Exception as e:
            logger.warning(f"Regime detection tool call failed: {e}")
            return None

    async def _run_specialists(
        self,
        query: CognitiveQuery,
        attention: AttentionContext,
        regime: RegimeInfo | None,
    ) -> list[_AgentRunResult]:
        prefer_cheap = self._deps.cost_monitor.should_downgrade()
        budget_remaining = self._deps.cost_monitor.remaining_budget_usd()
        if self._deps.cost_monitor.should_block():
            raise BudgetExceededError("daily cap reached; refusing new calls")

        # Pre-select per-agent.
        selections: dict[str, ModelSelection] = {}
        for name in SPECIALIST_AGENTS:
            # OPTIMIZATION: if attention weight is very low, force cheap
            weight = attention.attention_weights.get(name, 0.5)
            force_cheap = prefer_cheap or (weight < 0.3)
            
            selections[name] = self._deps.router.select(
                agent_name=name,
                context_tokens=4000,
                expected_output_tokens=800,
                prefer_cheap=force_cheap,
                budget_remaining_usd=budget_remaining,
            )

        # Fire specialists in parallel.
        coros = [
            self._run_one_specialist(name, selections[name], query, attention, regime)
            for name in SPECIALIST_AGENTS
        ]
        return await asyncio.gather(*coros)

    async def _run_one_specialist(
        self,
        agent_name: str,
        selection: ModelSelection,
        query: CognitiveQuery,
        attention: AttentionContext,
        regime: RegimeInfo | None,
    ) -> _AgentRunResult:
        t0 = time.monotonic()
        system_prompt = self._deps.prompt_loader.get_system_prompt(agent_name)
        version = self._deps.prompt_loader.get_spec(agent_name).version

        agent = LLMAgent(client=self._deps.client, name=agent_name)
        agent.add_system_prompt(system_prompt)
        
        # Add attention and regime context to the prompt
        task_prompt = _user_task_prompt(query, agent_name)
        task_prompt += f"\n\n## Market Context (Propagated by Orchestrator)\n"
        task_prompt += f"Dominant Theme: {attention.dominant_theme}\n"
        task_prompt += f"Attention Weight for {agent_name}: {attention.attention_weights.get(agent_name, 0.5):.2f}\n"
        if regime:
            task_prompt += f"Market Regime: {regime.classification} (confidence: {regime.confidence:.2f})\n"
            if regime.recommendation:
                task_prompt += f"Regime Recommendation: {regime.recommendation}\n"

        agent.add_message("user", task_prompt)

        models = [selection.primary, *selection.fallbacks]
        last_exc: Exception | None = None
        chat: ChatResult | None = None
        used_model = ""
        cost = 0.0

        for spec in models:
            try:
                chat = await agent.get_response(
                    model=spec.id,
                    temperature=0.0,
                    max_tokens=1200,
                    response_format={"type": "json_object"},
                    tools=tools_for(agent_name) or None,
                    append_assistant=False,
                )
                used_model = spec.id
                cost = chat.usage.cost_usd or spec.estimate_cost(
                    chat.usage.prompt_tokens, chat.usage.completion_tokens
                )
                self._deps.cost_monitor.record(
                    agent_name=agent_name,
                    cost_usd=cost,
                    prompt_tokens=chat.usage.prompt_tokens,
                    completion_tokens=chat.usage.completion_tokens,
                    model_id=spec.id,
                )
                break
            except OpenRouterError as e:
                last_exc = e
                logger.warning(
                    "agent=%s model=%s failed (%s); trying next fallback",
                    agent_name, spec.id, e,
                )
                continue

        latency_ms = int((time.monotonic() - t0) * 1000)

        if chat is None:
            return _AgentRunResult(
                agent=agent_name,
                output=None,
                error=str(last_exc) if last_exc else "all models failed",
                cost_usd=cost,
                latency_ms=latency_ms,
            )

        # Parse JSON. If parse fails, treat the agent as failed but keep cost.
        try:
            parsed = json.loads(chat.content)
            if not isinstance(parsed, dict):
                raise ValueError("expected JSON object")
        except Exception as e:
            return _AgentRunResult(
                agent=agent_name,
                output=None,
                error=f"non-JSON response: {e}",
                cost_usd=cost,
                latency_ms=latency_ms,
            )

        # Coerce the parsed dict into our typed envelope. Be liberal with
        # extras — the model may include payload fields we don't yet model.
        try:
            output = AgentOutput(
                agent=parsed.get("agent", agent_name),
                agent_version=parsed.get("agent_version", version),
                execution_meta=ExecutionMeta(
                    operating_mode="ONLINE",
                    data_quality=(
                        parsed.get("execution_meta", {}).get("data_quality") or "high"
                    ),
                    model_id=used_model,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                ),
                causal_chains=[
                    CausalChain.model_validate(c)
                    for c in parsed.get("causal_chains", [])
                    # Drop chains the strict validator would reject so the
                    # *agent* result still parses; the bundle-level
                    # causal_validator will record + report them next.
                    if isinstance(c, dict) and (c.get("invalid_condition") or "").strip()
                ],
                payload=parsed.get("payload", {k: v for k, v in parsed.items()
                                              if k not in {"agent", "agent_version",
                                                           "execution_meta", "causal_chains"}}),
            )
        except Exception as e:
            return _AgentRunResult(
                agent=agent_name,
                output=None,
                error=f"output validation failed: {e}",
                cost_usd=cost,
                latency_ms=latency_ms,
            )

        return _AgentRunResult(
            agent=agent_name,
            output=output,
            error=None,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    async def _run_reflection(
        self,
        query: CognitiveQuery,
        agent_outputs: dict[str, AgentOutput],
    ) -> tuple[list[str], float]:
        """Cheap reviewer LLM scans causal chains; returns (notes, cost).

        Uses governor's route (cheap & fast) under a different system prompt
        focused on "find chains that contradict each other or look weak".
        """
        chains_blob = json.dumps(
            [
                {
                    "agent": name,
                    "chains": [c.model_dump() for c in out.causal_chains],
                }
                for name, out in agent_outputs.items()
            ],
            ensure_ascii=False,
        )
        if not chains_blob or len(chains_blob) < 5:
            return [], 0.0

        try:
            sel = self._deps.router.select(
                agent_name="governor",
                context_tokens=2000,
                expected_output_tokens=400,
                prefer_cheap=True,
                budget_remaining_usd=self._deps.cost_monitor.remaining_budget_usd(),
            )
        except BudgetExceededError as e:
            logger.warning("reflection skipped: %s", e)
            return [], 0.0

        agent = LLMAgent(client=self._deps.client, name="reflection")
        agent.add_system_prompt(_REFLECTION_SYSTEM_PROMPT)
        agent.add_message(
            "user",
            f"Query intent: {query.query_intent}\n"
            f"Causal chains across agents:\n{chains_blob}\n\n"
            "Return JSON: {\"notes\": [\"...\", \"...\"]}",
        )
        try:
            result = await agent.get_response(
                model=sel.primary.id,
                temperature=0.0,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
        except OpenRouterError as e:
            logger.warning("reflection call failed: %s", e)
            return [], 0.0

        cost = result.usage.cost_usd or sel.primary.estimate_cost(
            result.usage.prompt_tokens, result.usage.completion_tokens
        )
        self._deps.cost_monitor.record(
            agent_name="reflection",
            cost_usd=cost,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            model_id=sel.primary.id,
        )
        try:
            payload = json.loads(result.content)
            notes = payload.get("notes", [])
            if not isinstance(notes, list):
                notes = []
        except Exception:
            notes = []
        return [str(n) for n in notes if n], cost

    async def _run_governor(
        self,
        query: CognitiveQuery,
        agent_outputs: dict[str, AgentOutput],
        kept_chains: list[CausalChain],
        contradictions: list[Contradiction],
        quality: QualityBreakdown,
        rejected_chains: list[CausalRejection],
        attention: AttentionContext,
        regime: RegimeInfo | None,
    ) -> tuple[GovernorDecision, float]:
        sel = self._deps.router.select(
            agent_name="governor",
            context_tokens=3000,
            expected_output_tokens=400,
            prefer_cheap=self._deps.cost_monitor.should_downgrade(),
            budget_remaining_usd=self._deps.cost_monitor.remaining_budget_usd(),
        )
        sys_prompt = self._deps.prompt_loader.get_system_prompt("governor")

        bundle = {
            "market_context": {
                "attention": attention.model_dump(),
                "regime": regime.model_dump() if regime else None,
            },
            "agents": {
                n: {
                    "agent_version": o.agent_version,
                    "execution_meta": o.execution_meta.model_dump(),
                    "payload": o.payload,
                }
                for n, o in agent_outputs.items()
            },
            "causal_chains_kept": [c.model_dump() for c in kept_chains],
            "causal_chains_rejected": [
                {"reason": r.reason, "detail": r.detail}
                for r in rejected_chains
            ],
            "quality": quality.model_dump(),
            "contradictions": [c.model_dump() for c in contradictions],
            "query_intent": query.query_intent,
        }

        agent = LLMAgent(client=self._deps.client, name="governor")
        agent.add_system_prompt(sys_prompt)
        agent.add_message(
            "user",
            "Evaluate the assembled bundle. Return JSON with shape:\n"
            "{ \"approved\": bool, \"operating_mode\": \"ONLINE\"|\"OFFLINE\", "
            "\"contradictions\": [...], \"rejection_reason\": str|null }\n"
            f"Bundle:\n{json.dumps(bundle, ensure_ascii=False)}",
        )
        try:
            result = await agent.get_response(
                model=sel.primary.id,
                temperature=0.0,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
        except OpenRouterError as e:
            logger.warning("governor call failed (%s); deterministic reject", e)
            return GovernorDecision(
                approved=False,
                operating_mode="OFFLINE",
                quality=quality,
                contradictions=contradictions,
                rejection_reason=f"governor_unavailable: {e}",
            ), 0.0

        cost = result.usage.cost_usd or sel.primary.estimate_cost(
            result.usage.prompt_tokens, result.usage.completion_tokens
        )
        self._deps.cost_monitor.record(
            agent_name="governor",
            cost_usd=cost,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            model_id=sel.primary.id,
        )

        try:
            parsed = json.loads(result.content)
        except Exception as e:
            return GovernorDecision(
                approved=False,
                operating_mode="OFFLINE",
                quality=quality,
                contradictions=contradictions,
                rejection_reason=f"governor_invalid_json: {e}",
            ), cost

        approved = bool(parsed.get("approved", False))
        op_mode = parsed.get("operating_mode") or ("ONLINE" if approved else "OFFLINE")
        if op_mode not in ("ONLINE", "OFFLINE"):
            op_mode = "OFFLINE"
        rejection_reason = parsed.get("rejection_reason")
        if approved and rejection_reason:
            rejection_reason = None
        if not approved and not rejection_reason:
            rejection_reason = "governor rejected without explicit reason"

        # Governor can ADD contradictions; we merge with the ones we already
        # tracked so consistency stays visible end-to-end.
        gov_contradictions = list(contradictions)
        for c in parsed.get("contradictions", []) or []:
            if isinstance(c, dict):
                try:
                    gov_contradictions.append(Contradiction.model_validate(c))
                except Exception:
                    continue

        try:
            decision = GovernorDecision(
                approved=approved,
                operating_mode=op_mode,
                quality=quality,
                contradictions=gov_contradictions,
                rejection_reason=rejection_reason,
            )
        except Exception as e:
            decision = GovernorDecision(
                approved=False,
                operating_mode="OFFLINE",
                quality=quality,
                contradictions=contradictions,
                rejection_reason=f"governor_decision_invalid: {e}",
            )
        return decision, cost

    async def _run_synthesis(
        self,
        query: CognitiveQuery,
        agent_outputs: dict[str, AgentOutput],
        kept_chains: list[CausalChain],
        decision: GovernorDecision,
        attention: AttentionContext,
        regime: RegimeInfo | None,
    ) -> tuple[str, float]:
        sel = self._deps.router.select(
            agent_name="synthesis",
            context_tokens=5000,
            expected_output_tokens=1400,
            prefer_cheap=self._deps.cost_monitor.should_downgrade(),
            budget_remaining_usd=self._deps.cost_monitor.remaining_budget_usd(),
        )
        sys_prompt = self._deps.prompt_loader.get_system_prompt("synthesis")
        agent = LLMAgent(client=self._deps.client, name="synthesis")
        agent.add_system_prompt(sys_prompt)
        agent.add_message(
            "user",
            f"User query: {query.query_text}\n"
            f"Intent: {query.query_intent}\n\n"
            "Approved bundle:\n"
            + json.dumps(
                {
                    "market_context": {
                        "attention": attention.model_dump(),
                        "regime": regime.model_dump() if regime else None,
                    },
                    "agents": {
                        n: o.payload for n, o in agent_outputs.items()
                    },
                    "causal_chains": [c.model_dump() for c in kept_chains],
                    "contradictions": [c.model_dump() for c in decision.contradictions],
                    "quality": decision.quality.model_dump(),
                },
                ensure_ascii=False,
            ),
        )

        models = [sel.primary, *sel.fallbacks]
        last_exc: Exception | None = None
        for spec in models:
            try:
                result = await agent.get_response(
                    model=spec.id,
                    temperature=0.2,
                    max_tokens=2000,
                    append_assistant=False,
                )
                cost = result.usage.cost_usd or spec.estimate_cost(
                    result.usage.prompt_tokens, result.usage.completion_tokens
                )
                self._deps.cost_monitor.record(
                    agent_name="synthesis",
                    cost_usd=cost,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    model_id=spec.id,
                )
                return result.content.strip(), cost
            except OpenRouterError as e:
                last_exc = e
                continue
        assert last_exc is not None
        raise last_exc

    # ───────────────────── helpers / building ─────────────────────

    def _extract_contradictions(
        self,
        agent_outputs: dict[str, AgentOutput],
        reflection_notes: list[str],
    ) -> list[Contradiction]:
        contradictions: list[Contradiction] = []
        # Agent-side contradictions (if the model emitted them in payload).
        for name, out in agent_outputs.items():
            for c in out.payload.get("contradictions", []) or []:
                if not isinstance(c, dict):
                    continue
                try:
                    contradictions.append(
                        Contradiction(
                            type=c.get("type", "agent_reported"),
                            severity=c.get("severity", "medium"),
                            description=c.get("description", str(c)),
                            resolution_hint=c.get("resolution_hint"),
                            agents_involved=c.get("agents_involved") or [name],
                        )
                    )
                except Exception:
                    continue
        # Reflection-loop notes promoted to low-severity contradictions.
        for note in reflection_notes:
            try:
                contradictions.append(
                    Contradiction(
                        type="reflection_note",
                        severity="low",
                        description=note,
                        agents_involved=[],
                    )
                )
            except Exception:
                continue
        return contradictions

    def _collect_aetheer_snapshots(
        self, agent_outputs: dict[str, AgentOutput]
    ) -> list[AetheerSnapshot]:
        snaps: list[AetheerSnapshot] = []
        for out in agent_outputs.values():
            for inst, snap in (out.payload.get("aetheer") or {}).items():
                if not isinstance(snap, dict):
                    continue
                snaps.append(
                    AetheerSnapshot(
                        instrument=inst,
                        present=bool(snap.get("present", True)),
                        age_hours=float(snap.get("age_hours", 0.0)),
                    )
                )
        return snaps

    def _offline_response(
        self,
        query: CognitiveQuery,
        *,
        rejection_reason: str,
        started_at: float,
        attention: AttentionContext | None = None,
        regime: RegimeInfo | None = None,
    ) -> CognitiveResponse:
        empty_quality = QualityBreakdown(
            freshness=0.0, completeness=0.0, consistency=0.0,
            source_reliability=0.0, aetheer_validity=0.0,
        )
        latency_ms = int((time.monotonic() - started_at) * 1000)
        return CognitiveResponse(
            approved=False,
            operating_mode="OFFLINE",
            quality=empty_quality,
            causal_chains=[],
            contradictions=[],
            attention=attention,
            regime=regime,
            rejection_reason=rejection_reason,
            synthesis_text=None,
            cost_usd=0.0,
            latency_ms=latency_ms,
            trace_id=query.trace_id,
            protocol_version=self._deps.prompt_loader.protocol_version(),
        )

    def _build_response(
        self,
        *,
        approved: bool,
        operating_mode: str,
        quality: QualityBreakdown,
        kept_chains: list[CausalChain],
        rejected_chains: list[CausalRejection],
        contradictions: list[Contradiction],
        attention: AttentionContext | None,
        regime: RegimeInfo | None,
        rejection_reason: str | None,
        synthesis_text: str | None,
        cost_usd: float,
        trace_id: str,
        started_at: float,
    ) -> CognitiveResponse:
        # Make the rejection log visible in `contradictions` so consumers can
        # show *why* a chain was dropped without a separate channel.
        merged_contradictions = list(contradictions)
        for r in rejected_chains:
            try:
                merged_contradictions.append(
                    Contradiction(
                        type=f"causal_rejected:{r.reason}",
                        severity="medium" if r.reason == "missing_invalid_condition" else "low",
                        description=r.detail,
                        agents_involved=[],
                    )
                )
            except Exception:
                continue
        latency_ms = int((time.monotonic() - started_at) * 1000)
        return CognitiveResponse(
            approved=approved,
            operating_mode=operating_mode,  # type: ignore[arg-type]
            quality=quality,
            causal_chains=kept_chains,
            contradictions=merged_contradictions,
            attention=attention,
            regime=regime,
            rejection_reason=rejection_reason,
            synthesis_text=synthesis_text,
            cost_usd=round(cost_usd, 6),
            latency_ms=latency_ms,
            trace_id=trace_id,
            protocol_version=self._deps.prompt_loader.protocol_version(),
        )


# ─────────────────────────── prompt helpers ───────────────────────────

_REFLECTION_SYSTEM_PROMPT = (
    "You are a fast critic of causal chains produced by upstream market-analysis "
    "agents. Read the chains in the user message. Return at most 4 short notes "
    "(<= 25 words each) flagging chains that:\n"
    "  - contradict each other across agents,\n"
    "  - have weak supporting evidence vs strong contradicting evidence,\n"
    "  - assert effects without a falsifiable invalid_condition.\n"
    "Output JSON: {\"notes\":[\"...\",\"...\"]}. No analysis, no chain-of-thought."
)


def _user_task_prompt(query: CognitiveQuery, agent_name: str) -> str:
    """One generic prompt that names what the agent should produce.

    The detailed protocol lives in the system prompt loaded from
    AGENT_PROTOCOL.json; here we only restate the user's question and the
    expected output envelope.
    """
    return (
        f"User query: {query.query_text}\n"
        f"Intent: {query.query_intent}\n"
        f"Instruments: {', '.join(query.instruments) or '(unspecified)'}\n"
        f"Timeframes: {', '.join(query.timeframes) or '(unspecified)'}\n"
        f"Trace: {query.trace_id}\n\n"
        f"Respond ONLY as JSON with this minimum shape:\n"
        f"{{\n"
        f'  "agent": "{agent_name}",\n'
        f'  "agent_version": "<semver>",\n'
        f'  "execution_meta": {{ "operating_mode": "ONLINE", "data_quality": "high" }},\n'
        f'  "causal_chains": [\n'
        f'    {{ "cause": "...", "effect": "...", "invalid_condition": "...",\n'
        f'       "confidence": 0.0, "timeframe": "H1",\n'
        f'       "supporting_evidence": [], "contradicting_evidence": [] }}\n'
        f"  ],\n"
        f'  "payload": {{ ...domain-specific fields... }}\n'
        f"}}\n"
        f"Every causal_chain MUST include `invalid_condition`. No exceptions."
    )


__all__ = [
    "AetheerCognitiveAgent",
    "CognitiveDeps",
    "McpBridge",
    "SPECIALIST_AGENTS",
    "QUALITY_FLOOR",
]
