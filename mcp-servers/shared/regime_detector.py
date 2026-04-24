"""regime_detector.py — Clasificación heurística del régimen de mercado.

Combina:
  1. Indicador Aetheer en múltiples TFs (ema_align, price_phase, atr_expanding)
  2. Histórico reciente de trades del usuario (win_rate, fail patterns)
  3. Calendario (priors de mes — abril favorece transición)

Output: {regime, confidence, symptoms, recommendation, calendar_bias}.

Diseñado como función pura para que pueda ser llamada por:
- Subagente price-behavior (al leer Aetheer)
- MCP tool detect_regime (para inspección directa o uso en synthesis)
- Validación post-trade (¿el régimen cambió desde el entry?)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("aetheer.regime_detector")

# Calendario de regímenes priors (referencial, ver feedback_april_regime_framework.md)
MONTH_PRIORS: dict[int, dict[str, Any]] = {
    1:  {"prior": "trending",   "note": "Inicio de año, posicionamiento institucional"},
    2:  {"prior": "trending",   "note": "Continuación, alta claridad direccional"},
    3:  {"prior": "trending",   "note": "Continuación, breakouts funcionan"},
    4:  {"prior": "transition", "note": "Manipulación liquidez, falsos breakouts frecuentes"},
    5:  {"prior": "transition", "note": "Mejora estructural progresiva"},
    6:  {"prior": "trending",   "note": "Estructura más limpia"},
    7:  {"prior": "ranging",    "note": "Baja volatilidad, lento, errático"},
    8:  {"prior": "ranging",    "note": "Continuación de baja vol"},
    9:  {"prior": "trending",   "note": "Alta actividad institucional, óptimo"},
    10: {"prior": "trending",   "note": "Continuación de actividad"},
    11: {"prior": "transition", "note": "Cierre de año, posicionamiento"},
    12: {"prior": "ranging",    "note": "Liquidez decreciente, holiday season"},
}


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k, default if k == keys[-1] else {})
        else:
            return default
    return cur if cur != {} else default


def _count_aetheer_signal(
    aetheer_by_tf: dict,
    field: str,
    target_value: Any,
) -> int:
    """Cuenta cuántos timeframes muestran ese valor en el campo de Aetheer."""
    count = 0
    for tf, data in (aetheer_by_tf or {}).items():
        if not isinstance(data, dict):
            continue
        # Aetheer puede venir bajo "aetheer_indicator" o directo
        ind = data.get("aetheer_indicator", data)
        if not isinstance(ind, dict):
            continue
        val = ind.get(field) or ind.get(field.upper())
        if val == target_value:
            count += 1
    return count


def detect_regime(
    aetheer_per_pair: dict | None = None,
    recent_trades_stats: dict | None = None,
    current_utc: datetime | None = None,
) -> dict:
    """Clasifica el régimen actual de mercado.

    Args:
        aetheer_per_pair: dict {pair: {tf: aetheer_data}}. Ejemplo:
            {"EURUSD": {"60": {...aetheer fields...}, "240": {...}}, "GBPUSD": ...}
            Cada aetheer_data debe tener al menos: ema_align, price_phase, atr_expanding.
        recent_trades_stats: output de get_recent_trades stats. Útiles:
            win_rate, avg_r_multiple, losses (count).
        current_utc: timestamp para inferir mes/sesión. None = ahora.

    Returns:
        {
          "regime": "trending|transition|ranging",
          "confidence": float [0,1],
          "symptoms": [str, ...],
          "recommendation": str,
          "calendar_bias": {"month": int, "prior": str, "note": str},
          "score_breakdown": {...}
        }
    """
    now = current_utc or datetime.now(timezone.utc)
    month = now.month
    cal_bias = MONTH_PRIORS.get(month, {"prior": "transition", "note": "fallback"})

    symptoms: list[str] = []
    score = {"trending": 0.0, "transition": 0.0, "ranging": 0.0}

    # 1) Calendar prior (peso 1.0)
    score[cal_bias["prior"]] += 1.0

    # 2) Aetheer multi-TF analysis (peso fuerte)
    if aetheer_per_pair:
        # Agregar todos los TF de todos los pares
        all_tfs: dict = {}
        for pair, tfs in aetheer_per_pair.items():
            for tf, data in (tfs or {}).items():
                all_tfs[f"{pair}_{tf}"] = data

        total_tfs = max(len(all_tfs), 1)

        # Mixed EMAs en 50%+ de TFs → transition
        mixed_count = _count_aetheer_signal(all_tfs, "ema_align", "mixed")
        if mixed_count / total_tfs >= 0.5:
            symptoms.append(f"mixed_ema_majority_{mixed_count}/{total_tfs}")
            score["transition"] += 1.5

        # Bullish o bearish ALIGNED en 60%+ → trending
        bull_count = _count_aetheer_signal(all_tfs, "ema_align", "bullish")
        bear_count = _count_aetheer_signal(all_tfs, "ema_align", "bearish")
        directional = max(bull_count, bear_count)
        if directional / total_tfs >= 0.6:
            direction = "bullish" if bull_count > bear_count else "bearish"
            symptoms.append(f"ema_aligned_{direction}_{directional}/{total_tfs}")
            score["trending"] += 1.5

        # Phase=compression dominante → ranging
        compr_count = _count_aetheer_signal(all_tfs, "price_phase", "compression")
        if compr_count / total_tfs >= 0.5:
            symptoms.append(f"price_phase_compression_{compr_count}/{total_tfs}")
            score["ranging"] += 1.0
            # Compression + mixed EMAs juntos → transition reforzada (pre-breakout)
            if mixed_count / total_tfs >= 0.4:
                score["transition"] += 0.5

        # Phase=expansion + atr_expanding → trending
        exp_count = _count_aetheer_signal(all_tfs, "price_phase", "expansion")
        atr_exp_count = _count_aetheer_signal(all_tfs, "atr_expanding", True)
        if exp_count / total_tfs >= 0.4 and atr_exp_count / total_tfs >= 0.4:
            symptoms.append(f"expansion+atr_expanding_{exp_count}/{total_tfs}")
            score["trending"] += 1.0

        # ATR expanding sin alineación direccional → transition (vol sin dirección)
        if atr_exp_count / total_tfs >= 0.5 and directional / total_tfs < 0.5:
            symptoms.append("atr_expanding_no_direction")
            score["transition"] += 1.0

        # rsi_div detectado en HTFs → riesgo de reversión, transition
        div_count = (
            _count_aetheer_signal(all_tfs, "rsi_div", "bull_div") +
            _count_aetheer_signal(all_tfs, "rsi_div", "bear_div")
        )
        if div_count >= 1:
            symptoms.append(f"rsi_divergence_present_{div_count}_tfs")
            score["transition"] += 0.5

    # 3) Trade journal: win_rate bajo en últimos 30d → señal de régimen adverso
    if recent_trades_stats:
        wr = recent_trades_stats.get("win_rate")
        closed = recent_trades_stats.get("closed", 0)
        losses = recent_trades_stats.get("losses", 0)
        if wr is not None and closed >= 3:
            if wr < 0.35:
                symptoms.append(f"recent_win_rate_low_{wr}")
                score["transition"] += 1.0
            elif wr > 0.65:
                symptoms.append(f"recent_win_rate_high_{wr}")
                score["trending"] += 0.5
        if losses >= 3:
            symptoms.append(f"recent_losses_streak_{losses}")
            score["transition"] += 0.5

    # Decisión final
    regime = max(score, key=score.get)
    total_weight = sum(score.values()) or 1.0
    confidence = round(score[regime] / total_weight, 2)

    recommendation = {
        "trending": (
            "Régimen tendencial: priorizar continuación; breakouts y pullbacks a EMA "
            "tienen mayor edge. Stops más amplios aceptables."
        ),
        "transition": (
            "Régimen de transición: PRIORIZAR REVERSIONES, esperar confirmaciones "
            "adicionales antes de entradas. Falsos breakouts frecuentes; cuestionar "
            "correlaciones. Recomendado: max 1-2 ops/día, stop day on first loss."
        ),
        "ranging": (
            "Régimen de rango: operar reversiones desde extremos (sess_high/low, prev_day). "
            "Evitar breakouts hasta confirmar expansión clara."
        ),
    }[regime]

    return {
        "regime": regime,
        "confidence": confidence,
        "symptoms": symptoms,
        "recommendation": recommendation,
        "calendar_bias": {
            "month": month,
            "prior": cal_bias["prior"],
            "note": cal_bias["note"],
        },
        "score_breakdown": {k: round(v, 2) for k, v in score.items()},
    }


if __name__ == "__main__":
    # Smoke test
    import json

    test_aetheer = {
        "EURUSD": {
            "60": {"ema_align": "mixed", "price_phase": "transition", "atr_expanding": True, "rsi_div": "bear_div"},
            "240": {"ema_align": "mixed", "price_phase": "compression", "atr_expanding": False, "rsi_div": "none"},
        },
        "GBPUSD": {
            "60": {"ema_align": "mixed", "price_phase": "transition", "atr_expanding": True, "rsi_div": "none"},
        },
    }
    test_stats = {"win_rate": 0.30, "closed": 5, "losses": 3, "avg_r_multiple": -0.5}

    result = detect_regime(test_aetheer, test_stats)
    print(json.dumps(result, indent=2))
