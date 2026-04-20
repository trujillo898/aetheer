# Aetheer — Sistema de Inteligencia de Mercado para Forex

Copiloto cognitivo para trading de Forex que proporciona contexto macroeconómico, liquidez, eventos y comportamiento de precio. No ejecuta trades ni genera señales.

## Cobertura

- **Instrumento primario:** US Dollar Index (DXY)
- **Pares operativos:** EURUSD, GBPUSD
- **Horizonte:** Intradía y swing (hasta 1 mes)

## Requisitos

- Python 3.12+
- SQLite 3
- Claude Code CLI

## Instalación

```bash
cd ~/aetheer
bash scripts/bootstrap.sh
```

El bootstrap crea un entorno virtual (`.venv/`), instala dependencias, inicializa la base de datos SQLite y verifica que todos los componentes estén en su lugar.

### Configuración opcional

- **FRED API Key:** Para datos macroeconómicos de la Federal Reserve, configura `FRED_API_KEY` en `.mcp.json` bajo el server `macro-data`.

## Uso

```bash
cd ~/aetheer
claude
```

Aetheer responde a estos tipos de consulta:

| Input | Respuesta |
|---|---|
| "análisis completo" | Todas las secciones, 1000-1400 palabras |
| Pregunta puntual | Solo sección relevante, 50-200 palabras |
| Dato específico | 1-3 oraciones |
| "status" | Reporte de salud del sistema |

## Arquitectura

### Subagentes

| Agente | Función |
|---|---|
| `liquidity` | Volatilidad, volumen, ventanas de actividad intradía |
| `events` | Calendario económico, impacto de noticias |
| `price-behavior` | Patrones estructurales del precio |
| `macro` | Política monetaria, tasas, correlaciones, geopolítica |
| `context-orchestrator` | Gestión, compresión y distribución de contexto |
| `synthesis` | Integración final y generación de análisis |

### MCP Servers (D013 — 3 servers)

| Server | Función |
|---|---|
| `tv-unified` | Fuente ÚNICA de precio/OHLCV/correlaciones/news/calendar vía TradingView CDP (puerto 9222) + APIs TV + cache SQLite stale-window 30min |
| `macro-data` | FRED + CME FedWatch (posturas BC, yields US fallback) |
| `memory` | Memoria persistente SQLite con compresión y time decay |

Ver `Essence/06_DECISIONES.txt` (D013) para el razonamiento de la consolidación.

### Base de datos

SQLite en `db/aetheer.db` con tablas:

- `price_snapshots` — Historial de precios
- `events` — Calendario económico con impacto medido
- `session_stats` — Estadísticas por sesión
- `context_memory` — Memoria comprimida con time decay
- `user_profile` — Perfil contextual del usuario
- `agent_outputs` — Últimos outputs de cada agente
- `heartbeat_log` — Registro de salud

## Salud del sistema

```bash
bash scripts/heartbeat.sh
```

## Estructura del proyecto

```
aetheer/
├── CLAUDE.md                    # Contexto principal
├── .claude/agents/              # 7 subagentes (incluye governor)
├── mcp-servers/                 # 3 MCP servers Python (D013)
│   ├── tv-unified/              # Fuente única: TV CDP + APIs + cache
│   ├── macro-data/              # FRED + FedWatch
│   └── memory/                  # SQLite + time decay
├── db/
│   ├── aetheer.db               # Memoria persistente
│   └── tv_cache.sqlite          # Cache TradingView
├── Essence/                     # Documentación canónica del sistema
│   ├── 01_VISION.txt
│   ├── 05_ESTADO_ACTUAL.txt
│   └── 06_DECISIONES.txt        # DXXX — decisiones arquitectónicas
├── config/                      # YAML configs
├── scripts/                     # bootstrap, heartbeat, tv-health-monitor
└── .mcp.json                    # MCP server configuration
```

## Reglas del sistema

1. **KILL SWITCH (D013):** `mcp__tv-unified__get_system_health` → si `OFFLINE` (todos los canales caídos Y cache > 30min), error explícito. Sin cascada a fuentes externas.
2. **ANTI-ALUCINACIÓN:** Datos servidos desde cache stale marcados con `(cache N min)` en la respuesta final. `meta.stale=true` propagado end-to-end.
3. **NO EJECUTAR:** Jamás señales de compra/venta.
4. **FUENTES CON TIMESTAMP:** Todo precio con fuente (`tradingview_cdp` | `tradingview_cdp_stale`) y hora.
5. **JSON ENTRE AGENTES:** Comunicación inter-agente estructurada.
6. **BINARIO ONLINE/OFFLINE (D011):** Operating Modes colapsados de 5 a 2 estados. Degradación se expresa en `meta.stale`, no en un estado global.
# aetheer
