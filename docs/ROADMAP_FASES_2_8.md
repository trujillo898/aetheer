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

### Contexto para la sesión

Fase 1 dejó listo un `OpenRouterClient` async y un `AetheerModelRouter`.
Ahora construimos la capa que los orquesta: un agente cognitivo que recibe una
`CognitiveQuery`, consulta MCPs (`tv-unified`, `macro-data`, `memory`), enruta
por agente a OpenRouter, valida causal chains con `invalid_condition` y decide
aprobación según el quality_score ponderado.

No heredamos de `AgentS3`. Portamos sólo dos patrones:

- Patrón **LMMAgent** de `Agent-S/gui_agents/s3/core/mllm.py` — gestión de
  message history con `add_system_prompt` / `add_message` / `reset` (la versión
  text-only, sin imágenes).
- Patrón **reflection-on-trajectory** del `Worker` — después de que synthesis
  produce causal chains, un "reviewer" barato (gpt-5-nano o haiku) las revisa
  antes de que governor decida. `enable_reflection_loop` en
  `config/feature_flags.yaml` gobierna si corre.

### Archivos a crear

```
agents/
├── cognitive_agent.py           # Orquestador principal
├── llm_agent.py                 # Message-history wrapper (port de LMMAgent)
├── mcp_tool_registry.py         # Registro de tools MCP como JSON-Schema
├── schemas.py                   # Pydantic: CognitiveQuery, CognitiveResponse,
│                                # CausalChain, QualityScore, Contradiction
├── quality_score.py             # D012: 5-factor weighted calculator
├── causal_validator.py          # D012: invalid_condition gate
└── prompts/                     # Loader de docs/AGENT_PROTOCOL.json
    └── loader.py
tests/
├── test_cognitive_agent.py
├── test_quality_score.py        # casos: freshness extrema, breakdown exacto
└── test_causal_validator.py     # rechaza chain sin invalid_condition
```

### Contratos

```python
# agents/schemas.py (boceto)
class CognitiveQuery(BaseModel):
    query_text: str
    query_intent: Literal["full_analysis","punctual","data_point","system_health","validate_setup"]
    instruments: list[str]
    timeframes: list[str] = []
    requested_by: Literal["user","scheduler","telegram","webapp"]
    trace_id: str

class CausalChain(BaseModel):
    cause: str
    effect: str
    invalid_condition: str        # NO DEFAULT — validator rechaza si falta
    confidence: float = Field(ge=0.0, le=1.0)
    timeframe: Literal["M15","H1","H4","D1","W1"]
    supporting_evidence: list[str] = []
    contradicting_evidence: list[str] = []

class QualityBreakdown(BaseModel):
    freshness: float              # 0-1
    completeness: float
    consistency: float
    source_reliability: float
    aetheer_validity: float
    global_score: float           # = 0.30*fresh + 0.25*complete + 0.20*consist + 0.15*source + 0.10*aetheer

class CognitiveResponse(BaseModel):
    approved: bool
    operating_mode: Literal["ONLINE","OFFLINE"]
    quality: QualityBreakdown
    causal_chains: list[CausalChain]
    contradictions: list[Contradiction]
    synthesis_text: str | None    # None si OFFLINE
    cost_usd: float
    latency_ms: int
    trace_id: str
```

### Flujo de `cognitive_analysis()`

1. `tv-unified.get_system_health()` → si `OFFLINE`, devuelve `CognitiveResponse(approved=False, operating_mode="OFFLINE", synthesis_text=None)` con error estructurado. **No invoca ningún LLM**.
2. Si `ONLINE`, llama en paralelo a MCPs según `query_intent` (ver tabla D014 en CLAUDE.md).
3. Por cada agente del protocolo (`liquidity`, `events`, `price-behavior`, `macro`): carga su system prompt desde `docs/AGENT_PROTOCOL.json`, pide un modelo a `AetheerModelRouter`, ejecuta `OpenRouterClient.chat_completion(...)`, parsea JSON.
4. Opcional (`enable_reflection_loop=True`): reviewer recorre causal chains y marca sospechosas.
5. `causal_validator.py` rechaza chains sin `invalid_condition` (D012).
6. `quality_score.py` calcula breakdown y global.
7. `governor` prompt decide `approved`; si `global_score < 0.60` → `approved=False`, `operating_mode="OFFLINE"`.
8. Si approved: `synthesis` agent genera texto final.
9. Registrar costo total en `CostMonitor`.

### Criterios de aceptación

- [ ] `pytest tests/test_cognitive_agent.py` — al menos 8 casos: ONLINE happy path, OFFLINE por TV caído, OFFLINE por quality<0.60, rechazo por falta de `invalid_condition`, reflection loop on/off, fallback de router cuando primary 5xx, presupuesto agotado → aborto con error, concurrencia de 4 agentes en paralelo.
- [ ] `CognitiveResponse.synthesis_text` es `None` sí y sólo sí `approved=False`.
- [ ] Ninguna llamada a MCP fuera de `tv-unified`/`macro-data`/`memory`.
- [ ] `quality_score.global_score` coincide con la fórmula ponderada al 1e-4 en 3 fixtures.

---

## Fase 3 — TradingView CDP Drawer (automatización visual opcional)

### Contexto

Cuando `cdp_drawing.enabled=true` en feature_flags y el trader dio consentimiento
explícito (almacenado en `db/aetheer.db` tabla `user_consents`), synthesis puede
dibujar zonas, líneas y anotaciones en TradingView Desktop vía CDP. Todo draw
necesita rollback token y sanitización estricta.

### Archivos a crear

```
mcp-servers/tv-unified/
├── cdp_drawing.py               # TradingViewCDPDrawer
├── drawing_schemas.py           # Pydantic: PriceZone, HorizontalLine, TextAnnotation
└── rollback_store.py            # SQLite de rollback tokens
scripts/
└── tv_js_snippets/              # JS inyectado vía Runtime.evaluate
    ├── draw_rect.js
    ├── draw_hline.js
    ├── draw_text.js
    └── remove_by_id.js
tests/
├── test_cdp_drawing_sanitization.py  # SQL/JS/XSS injection attempts
├── test_cdp_rollback.py              # undo funciona end-to-end
└── test_cdp_max_drawings.py          # cap en 10
```

### Reglas de seguridad no negociables

- Validar `math.isfinite(price)` y rangos plausibles (0.0001 < price < 100_000).
- Labels pasan por `json.dumps(label)` antes de inyectarse en el JS.
- ID de cada drawing = `uuid4().hex` + prefijo `aetheer_`.
- `max_drawings_per_analysis=10`: cualquier excedente se rechaza.
- Rollback token = UUID que mapea a lista de `(drawing_id, chart_symbol)`. Se
  persiste por 24h; después se purga.
- `require_user_consent=True`: el primer `draw_*` lanza una excepción
  `ConsentRequiredError` hasta que el trader ejecute `grant_drawing_consent()`.

### Contratos

```python
class PriceZone(BaseModel):
    symbol: str
    timeframe: str
    price_top: float
    price_bottom: float
    label: str = Field(max_length=80)
    confidence: float = Field(ge=0.0, le=1.0)  # driva color: verde/amarillo/rojo

class DrawingResult(BaseModel):
    drawing_ids: list[str]
    rollback_token: str
    chart_symbol: str
    created_at: datetime
```

### Criterios de aceptación

- [ ] Inyección de `<script>` en `label` → sanitizada o rechazada.
- [ ] Precio `float("nan")` o `inf` → rechazado con mensaje explícito.
- [ ] Rollback reversa exactamente los dibujos hechos en esa llamada, sin tocar otros.
- [ ] Feature flag `enabled=false` → todas las funciones de drawing hacen noop y devuelven `DrawingResult(skipped=True)`.
- [ ] Consent requerido funciona: primera llamada sin consent tira excepción; tras grant, funciona.

---

## Fase 4 — Memory + Trajectory Learning (Bloque B de Fase 2 cognitiva)

### Contexto

Evolucionamos `mcp-servers/memory/` para guardar trayectorias de análisis
completas (query → MCP data → causal chains → quality → feedback) y recuperar
casos similares para mejorar routing y priors de quality.

**No reemplaza** el SQLite con time decay existente — lo extiende.

### Archivos a crear/modificar

```
mcp-servers/memory/
├── server.py                        # +tools: store_trajectory, retrieve_similar
├── schema.sql                       # +tablas: trajectories, trajectory_embeddings
├── embedding.py                     # wrapper sobre OpenRouter text-embedding-3-small
└── trajectory_store.py              # CRUD + KNN via sqlite-vss (o cosine manual)
agents/
└── memory_integration.py            # glue entre CognitiveAgent y trajectory store
tests/
├── test_trajectory_store.py
├── test_similar_case_retrieval.py
└── test_memory_learning.py          # dos queries similares mejoran routing
```

### Contratos

```python
class AnalysisTrajectory(BaseModel):
    trace_id: str
    query: CognitiveQuery
    response: CognitiveResponse
    mcp_data_snapshot: dict           # lo que tv-unified devolvió
    model_routing: dict               # agente → modelo usado + costo
    user_feedback: Literal["positive","negative","mixed","none"] = "none"
    created_at: datetime

class SimilarCase(BaseModel):
    trajectory: AnalysisTrajectory
    similarity: float                 # 0-1 cosine
```

### Criterios de aceptación

- [ ] `store_trajectory()` persiste y retorna `trace_id`.
- [ ] `retrieve_similar(query, k=5, min_quality=0.70)` devuelve k trajectories
  con `similarity >= umbral` y `quality.global_score >= min_quality`.
- [ ] Test demostrable: después de 20 trajectories de "full_analysis EURUSD",
  el router elige un modelo distinto (con mejor historial) que en el primer run.
- [ ] No se guardan trajectories con `approved=False` por quality — sí con
  `approved=False` por OFFLINE (para diagnóstico).

---

## Fase 5 — System Services (scheduler)

### Contexto

Análisis automáticos a horas configurables vía env vars (formato `HH:MM` UTC).

```bash
AETHEER_SCHEDULE_LONDON=07:00
AETHEER_SCHEDULE_NY=12:30
AETHEER_SCHEDULE_DAILY=22:00
```

### Archivos a crear

```
services/
├── scheduler.py                  # AetheerScheduler (APScheduler-based)
└── schedule_presets.py           # queries predefinidas por tipo (london/ny/daily)
tests/
├── test_scheduler.py
└── test_schedule_presets.py
```

### Contratos

```python
class ScheduleConfig(BaseModel):
    name: Literal["london","ny","daily","custom"]
    time_utc: time                  # datetime.time(7, 0)
    query: CognitiveQuery           # preset por tipo

class AetheerScheduler:
    def __init__(self, cognitive_agent, config: list[ScheduleConfig]): ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def next_run_at(self, name: str) -> datetime | None: ...
```

### Criterios de aceptación

- [ ] `feature_flags.yaml: scheduler.enabled=false` → `start()` es noop.
- [ ] Desactivar `AETHEER_SCHEDULE_LONDON` (vacío) no registra el job.
- [ ] Tests con `freezegun`: a las 07:00 UTC dispara `london` preset y solo ese.
- [ ] Errores en el análisis programado se loguean pero no matan el scheduler.
- [ ] Resultado se enruta a interfaces activas (Fase 6) o a logger si ninguna.

---

## Fase 6 — Interfaces (WebApp FastAPI + Telegram bot)

### Contexto

Dos interfaces sincronizadas vía Redis pub/sub. WebApp usa streaming; Telegram
usa batch con formato MarkdownV2.

### Archivos a crear

```
interfaces/
├── web_app.py                    # FastAPI app
├── web_streaming.py              # SSE/WebSocket para tokens
├── telegram_bot.py               # python-telegram-bot
├── message_formatter.py          # adapta CognitiveResponse a MD/HTML/MDv2
└── sync_bus.py                   # Redis pub/sub wrapper
tests/
├── test_web_endpoints.py
├── test_telegram_handlers.py
├── test_message_formatter.py     # mismo análisis, 3 formatos
└── test_sync_bus.py
```

### Endpoints WebApp

- `POST /api/analyze` → dispara `cognitive_analysis()` asincrónico, devuelve `trace_id`.
- `GET /api/analysis/{trace_id}` → estado + resultado final.
- `GET /api/analysis/{trace_id}/stream` → SSE con tokens de synthesis.
- `GET /api/health` → proxea `tv-unified.get_system_health`.
- `GET /api/cost/today` → `CostMonitor.spent_today_usd()` + breakdown por agente.

### Comandos Telegram

`/analyze <par?>` `/status` `/budget` `/settings` `/schedule`

### Criterios de aceptación

- [ ] Mismo análisis llega a ambas interfaces en <2s de diferencia.
- [ ] Formato Telegram escapa correctamente caracteres MDv2 (`_*[]()~>#+-=|{}.!`).
- [ ] OFFLINE en una interfaz se propaga a la otra vía pub/sub en <1s.
- [ ] WebApp sin auth (local-only en v3.0; auth en v3.1).
- [ ] Telegram restringido a `TELEGRAM_ALLOWED_CHAT_IDS`.

---

## Fase 7 — Testing end-to-end + Feature Flags live

### Contexto

Consolidar tests, agregar suite de regresión contra D011-D015 y load test contra
límites de OpenRouter free tier.

### Archivos a crear

```
tests/
├── test_d011_operating_modes.py
├── test_d012_causal_chains.py
├── test_d013_tv_unified_only.py      # grep prohibitions: "alpha_vantage", "yahoo"
├── test_d014_multi_timeframe.py
├── test_d015_pine_indicator.py
├── e2e/
│   ├── test_full_analysis_online.py
│   ├── test_full_analysis_offline.py
│   └── test_scheduled_london_session.py
└── load/
    └── locust_openrouter.py          # --users=20 --spawn-rate=5
agents/
└── feature_flags.py                   # live-reload loader con filesystem watch
```

### Criterios de aceptación

- [ ] `pytest tests/ -v --cov=agents --cov=services --cov-report=term-missing` → cobertura >80%.
- [ ] `test_d013_tv_unified_only.py` grep de prohibiciones pasa en todo `agents/`, `services/`, `mcp-servers/` (excepto `.env.example` donde está documentado como deprecated).
- [ ] Tests e2e funcionan contra un mock de `tv-unified` (no requieren TV Desktop).
- [ ] Load test: 20 usuarios concurrentes, p95 < 3s para `punctual`, < 45s para `full_analysis`.

---

## Fase 8 — Deploy + docs + rollback

### Contexto

Documentación final, scripts de deploy y rollback.

### Archivos a crear

```
scripts/
├── bootstrap_v3.sh                  # setup fresh (venv, deps, DB migrations)
└── test_rollback_to_v2.sh           # checkout main, restore backup, smoke test
docs/
├── MIGRATION_GUIDE.md               # paso a paso v1.2 → v3.0
├── OPERATIONS.md                    # runbook: qué hacer si X falla
└── COST_PLAYBOOK.md                 # cómo ajustar presupuestos y routing
README.md                            # reescrito con arquitectura v3
```

### Contenido esperado de `MIGRATION_GUIDE.md`

1. Backup actual (ya hecho en Fase 0: `aetheer-backup-v1.2-*.tar.gz`).
2. Checkout de `feature/v3-hybrid-rewrite`.
3. Recrear venv (nota: el `.venv` actual tiene shebangs hardcodeados a `/home/thomas/aetheer/`; recrear en ubicación actual).
4. Instalar deps: `python -m pip install -r requirements.txt`.
5. Copiar `.env.example` a `.env` y completar `OPENROUTER_API_KEY`.
6. Smoke test: `pytest tests/ -v`.
7. Activar agentes uno por uno editando `config/feature_flags.yaml`.
8. Monitorear costos: `python -c "from services.cost_monitor import CostMonitor; ..."`.

### Criterios de aceptación

- [ ] `bash scripts/test_rollback_to_v2.sh` completa en <15 min y deja el repo funcional en `main`.
- [ ] `README.md` menciona las 8 fases, feature flags y la ubicación de `docs/AGENT_PROTOCOL.json`.
- [ ] `OPERATIONS.md` cubre los 6 escenarios de `CLAUDE.md → Protocolos de Error`.
- [ ] Merge a main requiere tests verdes y aprobación explícita (no auto-merge).

---

## Checklist maestro (validación antes de considerar v3.0 lista)

Reglas críticas (pegar en el PR de release):

- [ ] D011 Operating Modes binario se comporta idéntico a v1.2 (tests de paridad).
- [ ] D012 `invalid_condition` obligatorio + quality_score 5-factor exacto.
- [ ] D013 `grep -rE "alpha_vantage|yahoo|investing" agents/ services/ | wc -l` = 0.
- [ ] D014 dos modos (rápida/profunda) respetados por `cognitive_agent`.
- [ ] D015 `aetheer_validity` en `quality_score` depende de presencia de indicador.
- [ ] Kill switch (TV caído + cache > 30min) devuelve OFFLINE con mensaje explícito.
- [ ] Rollback a v1.2 completo en <15 min demostrado.
- [ ] Costo mensual estimado coincide con ~$5.79 (individual) / $19.30 (pro) de `CONTEXT_FOR_CLAUDE.md` ± 20%.
