# Aetheer v3.0 — Roadmap Fases 2-8 (prompts self-contained)

> **Cómo usar este documento.** Cada sección es un prompt completo y autónomo
> para una nueva sesión de Claude/Codex. Abrí una sesión nueva, pegá **sólo**
> la sección de la fase que querés ejecutar, y verificá el criterio de
> aceptación al final. No ejecutes fases fuera de orden — cada una asume el
> estado que dejó la anterior.
>
> **Invariantes que NUNCA pueden violarse (D011-D015 de `Essence/06_DECISIONES.txt`):**
>
> 1. TradingView CDP (`mcp-servers/tv-unified/`) es la **única** fuente de
>    precio/OHLCV/news/calendar. Prohibido agregar Alpha Vantage, Yahoo,
>    Investing, scraping, o cualquier otro fallback de mercado.
> 2. Operating Modes es **binario** (ONLINE / OFFLINE). `tv-unified.get_system_health`
>    es la única fuente de verdad. No reintroducir estados intermedios.
> 3. Toda afirmación direccional lleva `causal_chain` con `invalid_condition`
>    obligatorio. Si un agente no puede articularlo, `governor.approved=false`.
> 4. `quality_score` con 5 factores ponderados: Freshness 30% / Completeness 25% /
>    Consistency 20% / Source reliability 15% / Aetheer validity 10%. `< 0.60` → OFFLINE.
> 5. Indicador Pine (`indicators/aetheer_indicator.pine` v1.2.0) es la fuente
>    estructurada; `aetheer_validity` (10% del score) depende de su disponibilidad.
> 6. Kill Switch: TV caído + cache > 30min → OFFLINE total con error explícito.
>
> **Estado actual del repo cuando empieces una fase:** rama `feature/v3-hybrid-rewrite`,
> con Fase 0 (backup + export de prompts a `docs/AGENT_PROTOCOL.json`) y Fase 1
> (`agents/openrouter_client.py`, `agents/model_router.py`, `services/cost_monitor.py`,
> `tests/test_*.py` 25/25 verde, `config/feature_flags.yaml`) ya commited.
>
> **Lo que NO hacés en ninguna fase:** heredar de `AgentS3`/`Worker` de
> `Agent-S/`. La decisión (Opción C) fue extraer patrones como biblioteca
> conceptual y construir el orquestador propio. Agent-S es referencia documental.

---

## Fase 2 — AetheerCognitiveAgent (wrapper cognitivo, sin Agent-S)

---

## Fase 3 — TradingView CDP Drawer (automatización visual opcional)

---

## Fase 4 — Memory + Trajectory Learning (Bloque B de Fase 2 cognitiva)

---

## Fase 5 — System Services (scheduler)

