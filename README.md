# Aetheer — Sistema de Inteligencia de Mercado para Forex (v3.0 Standalone)

Copiloto cognitivo para trading de Forex que proporciona contexto macroeconómico, liquidez, eventos y comportamiento de precio. **v3.0 es ahora un sistema independiente (Standalone) que funciona sin Claude CLI.**

## Novedades v3.0 (Standalone & Agentic)

- **Orquestador Standalone:** Ejecución directa vía `cli.py` con interfaz profesional (Rich UI).
- **Attention Mechanism (Bloque 3):** Un agente de pre-procesado que identifica el tema dominante del mercado y asigna pesos de atención para optimizar el foco de los especialistas.
- **Regime Detection (Bloque 4):** Clasificación automática del régimen de mercado (trending/ranging/transition) integrada en el flujo causal.
- **Optimización de Costos:** Uso inteligente de modelos (Premium vs Nano) basado en los pesos del Attention Mechanism.
- **Independencia de Claude:** Puente MCP integrado (`StandardMcpBridge`) para comunicación directa con los servidores de datos.

## Requisitos

- Python 3.12+
- SQLite 3
- TradingView Desktop (con `--remote-debugging-port=9222` para CDP)
- OpenRouter API key

## Instalación rápida

```bash
cd ~/aetheer
bash scripts/bootstrap_v3.sh
export OPENROUTER_API_KEY="tu_key_aqui"
```

## Uso (Modo Standalone)

Ejecuta un análisis completo desde tu terminal:

```bash
python3 cli.py "Analiza el contexto actual de EURUSD tras los datos de inflación de hoy"
```

O entra en modo interactivo:

```bash
python3 cli.py
```

## Arquitectura v3

```
┌─────────────────────────────────────────────────────────────┐
│  Interfaces: cli.py (Standalone) / WebApp / Telegram Bot    │
├─────────────────────────────────────────────────────────────┤
│  Gateway / Control Plane: StandardMcpBridge (Direct MCP)    │
├─────────────────────────────────────────────────────────────┤
│  Attention Mechanism: Identifica Tema Dominante y Pesos     │
├─────────────────────────────────────────────────────────────┤
│  Cognitive Agent v3: Orquestación + Regime Detection        │
│  + Governor + Causal Engine + Synthesis                     │
├─────────────────────────────────────────────────────────────┤
│  MCP Servers: tv-unified, macro-data, memory                │
└─────────────────────────────────────────────────────────────┘
```

### Componentes Clave

| Módulo | Función |
|---|---|
| `cli.py` | Punto de entrada principal standalone con Rich UI. |
| `attention_agent.py` | Determina qué área (macro, técnica, etc.) requiere más atención. |
| `cognitive_agent.py` | Coordina a los especialistas inyectando régimen y atención. |
| `mcp_bridge.py` | Conecta el orquestador con los servidores MCP locales. |

## Roadmap v3.x

- [x] Independencia de Claude CLI (v3.0)
- [x] Attention Mechanism y Regime Detection (v3.0)
- [ ] Computer Use (Agent-S Integration) para TradingView GUI avanzada.
- [ ] Multi-agent Autonomous Loop (ReAct) para resolución de dudas complejas.

## Reglas de Seguridad

1. **KILL SWITCH:** Si TradingView está offline, el sistema se bloquea automáticamente.
2. **INVALID CONDITION:** Cada insight causal DEBE tener un criterio de invalidación claro.
3. **NO EXECUTION:** El sistema jamás genera señales de compra/venta ni opera por el usuario.
