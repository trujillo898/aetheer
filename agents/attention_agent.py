"""AttentionAgent — Bloque 3: Attention Mechanism.

Identifies the dominant market theme and assigns attention weights to specialist
agents based on current news, price action, and macro data.

Why:
    1. Focus: help specialist agents ignore noise and zoom in on signal.
    2. Efficiency: use NANO models for agents with low attention weights (< 0.3).
    3. Consistency: provide a unified "narrative anchor" for the entire bundle.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from agents.schemas import AttentionContext, CognitiveQuery
from agents.model_router import _GEMINI_FLASH

logger = logging.getLogger("aetheer.attention")

SYSTEM_PROMPT = """## Rol
Eres el Attention Mechanism de Aetheer. Tu objetivo es identificar el TEMA DOMINANTE del mercado Forex (especialmente DXY, EURUSD, GBPUSD) basándote en la consulta del usuario y el contexto de mercado reciente.

## Salida
Debes devolver un JSON con:
1. `dominant_theme`: Un tag corto (ej: "fed_policy", "geopolitics", "risk_off", "cpi_anticipation").
2. `attention_weights`: Un diccionario con pesos [0.0 a 1.0] para estos 4 agentes:
   - `macro`: Política monetaria, tasas, bonos.
   - `price-behavior`: Estructura, niveles técnicos, rupturas.
   - `events`: Calendario, noticias específicas.
   - `liquidity`: Sesiones, volumen, volatilidad.
3. `reasoning`: Una frase breve justificando el peso.

## Reglas de Pesos
- La suma de los pesos no necesita ser 1.0, pero cada peso debe reflejar la relevancia.
- Si hay noticias macro críticas (FOMC, CPI) -> `macro` y `events` deben ser altos (>0.7).
- Si el mercado está en rango sin noticias -> `price-behavior` y `liquidity` dominan.
- Si hay un evento geopolítico inesperado -> `events` y `macro` dominan.
"""

class AttentionAgent:
    """Lightweight agent that determines the focus of the analysis."""

    def __init__(self, client: Any, model_id: str = _GEMINI_FLASH.id):
        self._client = client
        self._model_id = model_id

    async def get_attention(self, query: CognitiveQuery, mcp_snapshot: dict) -> AttentionContext:
        """Analyze context and return attention weights."""
        
        # Prepare a compact snapshot for the LLM
        context_summary = {
            "query": query.query_text,
            "intent": query.query_intent,
            "news": (mcp_snapshot.get("news") or [])[:10],
            "calendar": (mcp_snapshot.get("calendar") or [])[:5],
            "price_summary": mcp_snapshot.get("price_summary", "No data")
        }

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Determina el foco de atención para este contexto:\n{json.dumps(context_summary)}"}
        ]

        try:
            result = await self._client.chat_completion(
                model=self._model_id,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0
            )
            data = json.loads(result.content)
            
            # Ensure all keys exist with defaults if LLM misses any
            weights = data.get("attention_weights", {})
            for agent in ["macro", "price-behavior", "events", "liquidity"]:
                if agent not in weights:
                    weights[agent] = 0.5
            
            return AttentionContext(
                dominant_theme=data.get("dominant_theme", "market_neutral"),
                attention_weights=weights,
                reasoning=data.get("reasoning", "Default neutral focus")
            )
        except Exception as e:
            logger.error(f"Attention determination failed: {e}")
            # Fallback to neutral weights
            return AttentionContext(
                dominant_theme="unknown",
                attention_weights={"macro": 0.5, "price-behavior": 0.5, "events": 0.5, "liquidity": 0.5},
                reasoning=f"Fallback due to error: {e}"
            )
