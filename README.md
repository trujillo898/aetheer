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

### MCP Servers

| Server | Función |
|---|---|
| `price-feed` | Precios en tiempo real (cascada: TradingEconomics → Investing → XE → Yahoo) |
| `economic-calendar` | Calendario económico (Investing.com, ForexFactory) |
| `macro-data` | Datos macro, yields, FedWatch, correlaciones |
| `news-feed` | Noticias macro y geopolítica vía RSS |
| `memory` | Memoria persistente SQLite con compresión y time decay |

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
├── .claude/agents/              # 6 subagentes
├── mcp-servers/                 # 5 MCP servers Python
│   ├── price-feed/
│   ├── economic-calendar/
│   ├── macro-data/
│   ├── news-feed/
│   └── memory/
├── db/                          # SQLite database
├── config/                      # YAML configs
├── scripts/                     # Bootstrap, heartbeat
└── .mcp.json                    # MCP server configuration
```

## Reglas del sistema

1. **KILL SWITCH:** Sin precio DXY accesible → error explícito
2. **ANTI-ALUCINACIÓN:** Datos >4h marcados como obsoletos
3. **NO EJECUTAR:** Jamás señales de compra/venta
4. **FUENTES CON TIMESTAMP:** Todo precio con fuente y hora
5. **JSON ENTRE AGENTES:** Comunicación inter-agente estructurada
# aetheer
