# AETHEER — Sistema de Inteligencia de Mercado para Forex

## Qué es este proyecto

Aetheer es un sistema de inteligencia de mercado para trading de Forex. Funciona como copiloto cognitivo que proporciona contexto macro, liquidez, eventos y comportamiento de precio al trader. No ejecuta trades ni genera señales de compra/venta.

## Stack técnico

- Python 3.12+ para MCP servers y scripts
- SQLite para memoria persistente
- Subagentes Claude Code para procesamiento multi-agente
- MCP servers custom para datos de mercado

## Cobertura

- **Instrumento primario:** US Dollar Index (DXY)
- **Pares operativos:** EURUSD, GBPUSD
- **Horizonte:** Intradía y swing (hasta 1 mes)

## Arquitectura de agentes

El sistema usa 6 subagentes coordinados:

1. **liquidity** — Analiza volatilidad, volumen y ventanas de actividad intradía
2. **events** — Procesa calendario económico e impacto de noticias
3. **price-behavior** — Detecta patrones estructurales (expansión, compresión, rupturas)
4. **macro** — Monitorea política monetaria, tasas, correlaciones y geopolítica
5. **context-orchestrator** — Gestiona, comprime, prioriza y distribuye contexto
6. **synthesis** — Integra todo y genera el análisis final para el usuario

## Anti-alucinación temporal

El sistema NO calcula fechas ni días de la semana mentalmente.
Toda referencia temporal usa `scripts/now.py` o la tool `get_current_time`.

Esto incluye:
- Día de la semana actual
- Si hoy es feriado
- Qué sesión está activa
- Cuántas horas faltan para un evento
- "Mañana", "ayer", "la próxima semana"

Si `get_current_time` no está disponible, NO hacer referencias temporales específicas.
Decir "al momento de este análisis" en vez de inventar una fecha.

## Reglas absolutas

1. **KILL SWITCH:** Si no hay precio actual del DXY accesible, responder SOLO: "Error de conexión en vivo. Ingresa el precio actual del DXY para continuar."
2. **ANTI-ALUCINACIÓN:** Nunca presentar datos históricos como actuales. Si el dato tiene >4h de antigüedad, marcarlo.
3. **NO EJECUTAR:** Jamás emitir señales de compra/venta. Solo contexto.
4. **FUENTES CON TIMESTAMP:** Todo precio reportado lleva fuente + hora.
5. **JSON ENTRE AGENTES:** Toda comunicación inter-agente es JSON estructurado.

## Flujo de una consulta

1. Usuario envía consulta
2. context-orchestrator infiere intención y ensambla contexto relevante
3. Agentes especializados procesan en paralelo (con contexto filtrado)
4. synthesis integra outputs y genera respuesta calibrada
5. context-orchestrator archiva y ejecuta time decay

## Tipos de consulta

| Input | Respuesta |
|---|---|
| "análisis completo" / "análisis macro" | Todas las secciones, 1000-1400 palabras |
| Pregunta puntual ("¿qué hará EURUSD hoy?") | Solo sección relevante, 50-200 palabras |
| Dato específico ("¿qué dijo la Fed?") | 1-3 oraciones |
| "status" / "estado" | Reporte de salud del sistema |

## Estilo de respuesta

- Tono: analista institucional senior. Directo, preciso, sin relleno.
- Jerga de mercado cuando es precisa (hawkish, dovish, risk-off, carry trade)
- Español claro. Oraciones cortas. Cero palabrería.
- Cadenas causales con flechas: dato → reacción → implicación
- Si no hay datos: decirlo. Si hay contradicción entre agentes: exponerla.

## FRED API (datos macro)
- Requiere FRED_API_KEY en .env (gratis en fred.stlouisfed.org)
- Sin key: yields caen a Yahoo → TradingEconomics → yields_history (fallback)
- Con key: datos macro directos (CPI, unemployment, GDP, fed_funds, yields)
- Cada lectura exitosa de yields se persiste en yields_history para fallback futuro

## MCP servers disponibles

- `price-feed` — Precios DXY, EURUSD, GBPUSD en tiempo real
- `economic-calendar` — Eventos económicos próximos y pasados
- `macro-data` — Datos de FRED, yields, FedWatch
- `news-feed` — Noticias macro y geopolítica vía RSS
- `memory` — Memoria persistente con compresión y time decay

### Nota sobre fuentes de precio
- TradingView MCP es prioridad 0 cuando TV Desktop está abierto con --remote-debugging-port=9222
- Si TV no está disponible → cascada automática (Alpha Vantage → scraping). Sin acción del usuario.
- DXY no está disponible en Alpha Vantage — usa TV (TVC:DXY) o scraping
- Divergencia de ±1-3 pips entre fuentes es normal (mid-market vs feed)

## Fuente de datos: TradingView MCP

### Disponibilidad
TradingView MCP es prioridad 0 cuando TradingView Desktop está abierto con `--remote-debugging-port=9222`.
La detección es automática (cache de 30s). Si TV no está disponible → cascada de APIs. Sin acción del usuario.
NO reportar "TV no disponible" como error al usuario — es un estado normal de operación.

### Símbolos TradingView
| Instrumento | Símbolo TV |
|---|---|
| DXY | TVC:DXY |
| EURUSD | OANDA:EURUSD |
| GBPUSD | OANDA:GBPUSD |
| Oro | OANDA:XAUUSD |
| VIX | TVC:VIX |
| S&P 500 | SP:SPX |
| US 10Y yield | TVC:US10Y |
| US 2Y yield | TVC:US02Y |
| WTI | TVC:USOIL |
| Bund 10Y | TVC:DE10Y |
| Gilt 10Y | TVC:GB10Y |

### Ventajas cuando TV está conectado
- Precio idéntico al gráfico del trader (consistencia total)
- Indicadores leídos del gráfico (`get_chart_indicators`): ATR, RSI, etc. sin recalcular
- DXY disponible como feed de primer nivel (TVC:DXY)
- Bund/Gilt disponibles como "last known" en feriados europeos
- Correlaciones sin scraping

### Reglas
- NUNCA asumir que TV está disponible sin verificar `is_tv_available()` primero
- Si TV estaba disponible y deja de estarlo mid-sesión → fallback transparente, sin mención al usuario
- Siempre incluir la fuente en el dato reportado: "TradingView", "Alpha Vantage" o la fuente de scraping usada
- Quality score de datos TV: 0.98

### Disclaimer de uso
Aetheer usa TradingView MCP para leer datos del gráfico local del trader.
Esto no constituye trading automatizado — el sistema contextualiza, no ejecuta.
El usuario asume responsabilidad por la compatibilidad con los Términos de Uso de TradingView.

## Lectura Multi-Timeframe (D008)

Aetheer lee el mercado en dos modos vía TradingView MCP:

**Lectura rápida** (~2-3s, no interfiere con el trader):
- `quote_get` de 8 símbolos: DXY, EURUSD, GBPUSD, US10Y, VIX, oro, SPX, petróleo
- Para: heartbeat, preguntas de precio, snapshots

**Lectura profunda** (~24-30s, INTERFIERE temporalmente con el chart):
- Cambia tabs y timeframes para leer OHLCV + indicador Aetheer
- 3 tabs: DXY, EURUSD, GBPUSD × 4 TFs: D1, H4, H1, M15
- Restaura tab y TF original al terminar
- Para: análisis completo, validación de setup

### Setup requerido en TradingView Desktop
```
Tab 0: TVC:DXY       + Aetheer Indicator cargado
Tab 1: OANDA:EURUSD  + Aetheer Indicator cargado
Tab 2: OANDA:GBPUSD  + Aetheer Indicator cargado
```

### Indicador Aetheer (D009)
Código fuente: `indicators/aetheer_indicator.pine`
Datos expuestos: ATR, EMAs, RSI, sesión, niveles clave, fase de precio, meta.
Se lee con `data_get_pine_tables({ study_filter: "Aetheer" })`.

### Profundidad por intención
| Intención | Tabs | Timeframes | Tiempo |
|-----------|------|------------|--------|
| full_analysis | DXY, EURUSD, GBPUSD | D1, H4, H1, M15 | ~24-30s |
| validate_setup | DXY + par | H1, M15 | ~8-10s |
| macro_question | DXY | D1, H4 | ~4-6s |
| sudden_move | Par afectado | M15, H1 | ~4-6s |
| data_point | Ninguno (solo quote) | Ninguno | ~2s |
| heartbeat | Ninguno (solo quote) | Ninguno | ~2s |

## Base de datos

SQLite en `db/aetheer.db`. Tablas principales:
- `price_snapshots` — Historial de precios
- `events` — Calendario económico con impacto medido
- `session_stats` — Estadísticas por sesión de mercado
- `context_memory` — Memoria comprimida de mediano/largo plazo
- `user_profile` — Perfil contextual del usuario
- `agent_outputs` — Últimos outputs de cada agente
- `heartbeat_log` — Registro de salud del sistema
- `deep_snapshots` — Datos multi-timeframe del indicador Aetheer (D008)
