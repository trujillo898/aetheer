"""causal_chain_validator.py — Validador automático de causal chains.

Diseño:
  - Cada chain tiene un `trigger_struct` JSON con shape estructurado para
    permitir validación programática (sin LLM en el loop).
  - El validador compara estado de mercado actual vs trigger; si dispara,
    marca la chain como `invalidated` y registra el motivo.
  - Chains sin trigger estructurado → validación manual o expiran.
  - Ejecución típica: cron horario llamando `scripts/validate_chains.py`.

Triggers soportados:
  - price_break: {type, level, side('above'|'below'), instrument}
  - ema_cross: {type, ema(20|50|200), side, instrument, timeframe}
  - atr_threshold: {type, multiplier, baseline_atr, instrument}
  - manual_only: {type} — sólo se cierra a mano
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger("aetheer.cc_validator")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def evaluate_trigger(trigger: dict, market_snapshot: dict) -> Optional[str]:
    """Evalúa si el trigger estructurado disparó dado el snapshot actual.

    Args:
        trigger: dict con type + parámetros
        market_snapshot: {instrument: {price, aetheer_indicator: {...}}}

    Returns:
        None si el trigger NO disparó (chain sigue válida).
        str con el motivo si SÍ disparó (chain invalidada).
    """
    if not isinstance(trigger, dict):
        return None
    ttype = trigger.get("type")

    if ttype == "manual_only":
        return None

    instrument = trigger.get("instrument", "").upper()
    snap = market_snapshot.get(instrument) or market_snapshot.get(instrument.lower())
    if not snap:
        return None  # sin datos, no podemos evaluar

    if ttype == "price_break":
        try:
            level = float(trigger["level"])
            side = trigger.get("side", "below")
            price = snap.get("price") or snap.get("close")
            if price is None:
                return None
            price = float(price)
            if side == "below" and price < level:
                return f"price_break: {instrument} {price} < {level}"
            if side == "above" and price > level:
                return f"price_break: {instrument} {price} > {level}"
        except (ValueError, KeyError, TypeError) as e:
            logger.debug(f"price_break eval falló: {e}")
        return None

    if ttype == "ema_cross":
        try:
            ema_n = int(trigger.get("ema", 50))
            side = trigger.get("side", "below")
            tf = trigger.get("timeframe", "60")
            ind = (snap.get("aetheer_per_tf", {}) or {}).get(str(tf), {})
            if not ind:
                ind = snap.get("aetheer_indicator", {})
            relation = ind.get(f"price_vs_ema{ema_n}") or ind.get(f"PRICE_VS_EMA{ema_n}")
            if relation == side:
                return f"ema_cross: {instrument} price_vs_ema{ema_n}={relation} on TF{tf}"
        except (ValueError, TypeError, KeyError) as e:
            logger.debug(f"ema_cross eval falló: {e}")
        return None

    if ttype == "atr_threshold":
        try:
            mult = float(trigger.get("multiplier", 1.5))
            baseline = float(trigger["baseline_atr"])
            ind = snap.get("aetheer_indicator", {})
            current_atr = ind.get("atr14") or ind.get("ATR14")
            if current_atr is None:
                return None
            current_atr = float(current_atr)
            if current_atr > baseline * mult:
                return f"atr_threshold: {instrument} atr={current_atr} > {baseline}*{mult}"
        except (ValueError, TypeError, KeyError) as e:
            logger.debug(f"atr_threshold eval falló: {e}")
        return None

    return None


def calc_expiry(timeframe: Optional[str] = None, days: int = 14) -> str:
    """Devuelve ISO8601 timestamp para expiración de la chain.

    Default 14 días (alineado con decay_factor 0.92/día → vida útil ~14d).
    Para chains de scalping (M15) reducir a 3 días; para D1+ extender a 30.
    """
    if timeframe in ("1", "5", "15"):
        days = 3
    elif timeframe in ("D", "W", "1D", "1W"):
        days = 30
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def adjust_confidence_post_validation(initial: float, validated: bool) -> float:
    """Ajusta confidence tras validación.

    Validated → +20% (cap 1.0). Invalidated → -50% (floor 0.05).
    Esto alimenta el calibration loop: chains que se cumplen ganan peso,
    chains que fallan pierden peso para futuras síntesis.
    """
    if validated:
        return round(min(initial * 1.2, 1.0), 3)
    return round(max(initial * 0.5, 0.05), 3)
