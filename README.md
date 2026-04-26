# Aetheer — Sistema de Inteligencia de Mercado para Forex

Copiloto cognitivo para trading de Forex que proporciona contexto macroeconómico, liquidez, eventos y comportamiento de precio. **No ejecuta trades ni genera señales.**

> **Estado:** migración v1.2 → v3.0 con 8 fases implementadas en código (activación gradual por feature flags). v1.2 (Claude Code CLI + subagentes `.md`) sigue disponible en paralelo durante el rollout.

## Cobertura

- **Instrumento primario:** US Dollar Index (DXY)
- **Pares operativos:** EURUSD, GBPUSD
- **Correlaciones:** XAUUSD, VIX, SPX, US10Y, US02Y, USOIL, DE10Y, GB10Y
- **Horizonte:** Intradía (M15, H1, H4) y swing (D1, W1, hasta 1 mes)

## Requisitos

- Python 3.12+
- SQLite 3
- TradingView Desktop con `--remote-debugging-port=9222` (para CDP)
- Claude Code CLI (modo v1.2) **o** OpenRouter API key (modo v3.0)

## Instalación

```bash
cd ~/aetheer
bash scripts/bootstrap_v3.sh
```

El bootstrap v3 recrea `.venv`, instala dependencias, inicializa/migra SQLite y ejecuta smoke tests.

### Configuración

Variables de entorno relevantes (todas opcionales — se leen si están presentes):

| Variable | Uso | Fase |
|---|---|---|
| `OPENROUTER_API_KEY` | Routing v3.0 a modelos vía OpenRouter | 1 |
| `OPENAI_API_KEY` / `AETHEER_EMBEDDING_API_KEY` | Embeddings para retrieval semántico de trayectorias | 4 |
| `AETHEER_EMBEDDING_STUB=1` | Forzar embeddings determinísticos (tests / sin keys) | 4 |
| `AETHEER_SCHEDULE_LONDON` | Hora UTC `HH:MM` para análisis pre-Londres | 5 |
| `AETHEER_SCHEDULE_NY` | Hora UTC para análisis pre-NY (overlap) | 5 |
| `AETHEER_SCHEDULE_DAILY` | Hora UTC para cierre del día | 5 |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram | 6 |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Whitelist de chat IDs permitidos para el bot | 6 |
| `REDIS_URL` | Backend Redis para sync entre WebApp y Telegram | 6 |
| `FRED_API_KEY` | Datos macro de la Fed (en `.mcp.json` bajo `macro-data`) | 1.2 |
| `AETHEER_AUTORESTART` / `AETHEER_TV_LAUNCH_CMD` | `tv-health-monitor` relanza TV Desktop si cae | 1.2 |

## Uso

### Modo v1.2 (estable)

```bash
cd ~/aetheer
claude
```

| Input | Respuesta |
|---|---|
| "análisis completo" | Todas las secciones, 1000-1400 palabras |
| Pregunta puntual | Solo sección relevante, 50-200 palabras |
| Dato específico | 1-3 oraciones |
| "status" | Reporte de salud del sistema |

### Modo v3.0 (detrás de feature flags)

`config/feature_flags.yaml` controla la activación gradual por agente. Por defecto todo está en `false` — el trader migra de a un agente a la vez:

```yaml
openrouter:
  use_openrouter_by_agent:
    liquidity: false      # flip a true para migrar este agente
    events: false
    price-behavior: false
    macro: false
    synthesis: false
    governor: false
    context-orchestrator: false

memory:
  enable_trajectory_learning: false      # Fase 4
  enable_similar_case_retrieval: false   # Fase 4

scheduler:
  enabled: false                         # Fase 5
  timezone: "UTC"
```

Protocolo de prompts y contratos JSON: `docs/AGENT_PROTOCOL.json` (fuente de verdad portable; consumida por `agents/model_router.py` + OpenRouter). Se re-exporta desde `.claude/agents/*.md` con `python scripts/export_prompts.py`.

## Arquitectura

### Capas

```
┌─────────────────────────────────────────────────────────────┐
│  Interfaces (Fase 6 ✅): WebApp + Telegram + sync bus       │
├─────────────────────────────────────────────────────────────┤
│  Scheduler (Fase 5 ✅): cron diario → cognitive_analysis    │
├─────────────────────────────────────────────────────────────┤
│  Cognitive Agent (Fase 2 ✅): orquestación + governor +     │
│  causal_validator + quality_score + synthesis               │
├─────────────────────────────────────────────────────────────┤
│  Model Router (Fase 1 ✅): OpenRouter + budget gating       │
│  Memory Integration (Fase 4 ✅): trajectory learning        │
├─────────────────────────────────────────────────────────────┤
│  MCP Servers: tv-unified, macro-data, memory                │
└─────────────────────────────────────────────────────────────┘
```

### Subagentes (7 especialidades)

| Agente | Función |
|---|---|
| `liquidity` | Volatilidad, volumen, ventanas de actividad intradía |
| `events` | Calendario económico, impacto de noticias |
| `price-behavior` | Patrones estructurales del precio + indicador Aetheer |
| `macro` | Política monetaria, tasas, correlaciones, geopolítica |
| `context-orchestrator` | Gestión, compresión y distribución de contexto |
| `synthesis` | Integración final y generación de análisis |
| `governor` | Validación de calidad + decisión approve/reject |

### MCP Servers (D013 — 3 servers)

| Server | Función |
|---|---|
| `tv-unified` | Fuente ÚNICA de precio/OHLCV/correlaciones/news/calendar vía TradingView CDP (puerto 9222) + APIs TV + cache SQLite stale-window 30 min |
| `macro-data` | FRED + CME FedWatch (posturas BC, yields US fallback) |
| `memory` | SQLite con compresión + time decay + **trajectories** (Fase 4) |

Ver `Essence/06_DECISIONES.txt` (D013) para el razonamiento de la consolidación.

### Capa cognitiva v3.0 (`agents/`)

| Módulo | Función |
|---|---|
| `cognitive_agent.py` | Orquestador top-level; fan-out de agentes en paralelo, governor, synthesis |
| `model_router.py` | Selección de modelo por agente con fallback y budget gating |
| `openrouter_client.py` | Cliente async con retry exponencial + accounting por llamada |
| `quality_score.py` | Cálculo D012 (5 factores ponderados) |
| `causal_validator.py` | Garantiza `invalid_condition` en cada cadena causal |
| `memory_integration.py` | Glue con trajectory store: persiste runs, deriva priors de routing |
| `schemas.py` | Contratos Pydantic estrictos (CognitiveQuery, CognitiveResponse, etc.) |

### Servicios (`services/`)

| Módulo | Función |
|---|---|
| `cost_monitor.py` | Tracking de gasto, alertas, downgrade automático |
| `scheduler.py` | APScheduler async con feature flag gate y error isolation |
| `schedule_presets.py` | Presets London / NY / daily → `CognitiveQuery` |

### Base de datos

SQLite en `db/aetheer.db`:

| Tabla | Propósito |
|---|---|
| `price_snapshots`, `events`, `session_stats` | Datos de mercado |
| `context_memory` | Memoria comprimida con time decay |
| `agent_outputs`, `heartbeat_log` | Trazabilidad y salud |
| `causal_chains` | Cadenas causales con invalidación + status |
| `trade_log` | Journal de operaciones del trader |
| `trajectories`, `trajectory_embeddings` | Casos completos para retrieval semántico (Fase 4) |
| `yields_history`, `fedwatch_history`, `feed_status`, `deep_snapshots` | Snapshots multi-timeframe + monitoreo |

## Salud del sistema

```bash
bash scripts/heartbeat.sh
```

Para monitorear / reiniciar TradingView Desktop automáticamente:

```bash
python3 scripts/tv-health-monitor.py
```

## Tests

```bash
AETHEER_EMBEDDING_STUB=1 python3 -m pytest -v
```

Suite actual: 136 tests verdes (cognitive_agent, model_router, openrouter_client, quality_score, causal_validator, cost_monitor, CDP drawing/rollback/sanitization, trajectory store, similar case retrieval, memory learning, scheduler, schedule presets).

## Estructura del proyecto

```
aetheer/
├── CLAUDE.md                       # Contexto principal (v1.2; v3.0 mueve protocolo a docs/AGENT_PROTOCOL.json)
├── .claude/agents/                 # 7 subagentes en formato .md (modo CLI)
├── docs/
│   ├── AGENT_PROTOCOL.json         # Fuente de verdad de prompts y contratos v3.0
│   ├── ROADMAP_FASES_2_8.md        # Plan de migración fase por fase
│   ├── MIGRATION_GUIDE.md          # Paso a paso v1.2 -> v3.0
│   ├── OPERATIONS.md               # Runbook de incidentes y recuperación
│   └── COST_PLAYBOOK.md            # Ajuste de presupuesto y routing
├── agents/                         # Capa cognitiva v3.0 (Python)
├── services/                       # Cost monitor, scheduler
├── interfaces/                     # WebApp FastAPI + Telegram + streaming + sync bus
├── mcp-servers/                    # 3 MCP servers Python (D013)
│   ├── tv-unified/
│   ├── macro-data/
│   └── memory/                     # ahora con trajectory_store.py + embedding.py
├── db/
│   ├── aetheer.db                  # Memoria persistente
│   └── tv_cache.sqlite             # Cache TradingView
├── Essence/                        # Documentación canónica
│   ├── 01_VISION.txt
│   ├── 05_ESTADO_ACTUAL.txt
│   └── 06_DECISIONES.txt           # DXXX — decisiones arquitectónicas
├── config/
│   ├── feature_flags.yaml          # Activación gradual por agente
│   ├── decay.yaml
│   ├── context-buffer.yaml
│   └── sources.yaml
├── tests/                          # pytest suite
├── scripts/                        # bootstrap v3, rollback drill, heartbeat, tv-health-monitor
└── .mcp.json                       # MCP server configuration
```

## Roadmap (8 fases)

| Fase | Bloque | Estado |
|---|---|---|
| 1 | Infraestructura: OpenRouter client, model router, cost monitor | ✅ |
| 2 | Cognitive agent + governor + causal validator + quality score | ✅ |
| 3 | CDP drawing (anotación opcional sobre TradingView) | parcial |
| 4 | Memory: trajectory store + similar case retrieval + learning | ✅ |
| 5 | Scheduler con presets London / NY / daily | ✅ |
| 6 | Interfaces: WebApp FastAPI + Telegram bot + Redis pub/sub | ✅ |
| 7 | Tests E2E + regresión D011-D015 + load testing | ✅ |
| 8 | Deploy + docs + rollback + recreación de venv | ✅ |

Documentación clave:
- `docs/AGENT_PROTOCOL.json` (protocolo de agentes y contratos)
- `docs/ROADMAP_FASES_2_8.md` (plan fase a fase)
- `docs/MIGRATION_GUIDE.md` / `docs/OPERATIONS.md` / `docs/COST_PLAYBOOK.md`

## Reglas del sistema

1. **KILL SWITCH (D013):** `mcp__tv-unified__get_system_health` → si `OFFLINE` (todos los canales caídos Y cache > 30 min), error explícito. Sin cascada a fuentes externas.
2. **ANTI-ALUCINACIÓN:** Datos servidos desde cache stale marcados con `(cache N min)` en la respuesta final. `meta.stale=true` propagado end-to-end.
3. **NO EJECUTAR:** Jamás señales de compra/venta.
4. **FUENTES CON TIMESTAMP:** Todo precio con fuente (`tradingview_cdp` | `tradingview_cdp_stale`) y hora.
5. **JSON ENTRE AGENTES:** Comunicación inter-agente estructurada (Pydantic en v3.0).
6. **BINARIO ONLINE/OFFLINE (D011):** Operating Modes colapsados de 5 a 2 estados. Degradación se expresa en `meta.stale`, no en un estado global.
7. **`invalid_condition` OBLIGATORIO (D012):** Cada cadena causal debe declarar qué la invalidaría. Sin esto, se descarta antes de synthesis.
