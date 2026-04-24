"""
tv_market_reader.py — Lectura de datos de mercado desde TradingView Desktop.

Dos modos:
  - quick_read(): quote del chart activo. No toca el chart. ~2-3s.
  - deep_read():  tab switch + TF switch. Lee OHLCV + Pine tables. ~24-30s.
    INTERFIERE con el chart del trader (restaura siempre al terminar).

Requiere:
  - TV Desktop corriendo con --remote-debugging-port=9222
  - Un chart activo (el que sea) con el indicador Aetheer (D009) aplicado.
    El indicador se mantiene al cambiar de símbolo (comportamiento nativo TV).
  - Para deep_read: set_symbol secuencial sobre ese chart único + guard.

Reemplaza la implementación anterior basada en subprocess Node.js y la
arquitectura multi-tab (eliminada 2026-04-21 por cross-contamination recurrente).
"""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .tv_availability import get_tv_commands, is_tv_available
from .pine_parser import parse_aetheer_table, validate_aetheer_data

logger = logging.getLogger("aetheer.tv_market_reader")

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "db" / "aetheer.db"

# Mapeo instrumento → símbolo TV. deep_read los recorre con set_symbol
# secuencial sobre el chart activo (ya no hay multi-tab).
SYMBOL_CONFIG: dict[str, str] = {
    "DXY":    "TVC:DXY",
    "EURUSD": "OANDA:EURUSD",
    "GBPUSD": "OANDA:GBPUSD",
}

# Pares candidatos para restore post-deep_read (operables intradía).
RESTORE_CANDIDATES = ["EURUSD", "GBPUSD"]

# Valores de timeframe para la API de TradingView
TF_MAP = {
    "D1":  "D",
    "H4":  "240",
    "H1":  "60",
    "M15": "15",
}

# Timeframes por intención de consulta
TF_PROFILES = {
    "full_analysis":  ["D1", "H4", "H1", "M15"],
    "validate_setup": ["H1", "M15"],
    "macro_question": ["D1", "H4"],
    "sudden_move":    ["M15", "H1"],
}

# Cache TTL por timeframe (segundos)
CACHE_TTL = {
    "quote_get": 60,
    "deep_D1":   900,
    "deep_H4":   600,
    "deep_H1":   300,
    "deep_M15":  120,
}


# ── UTILIDADES ────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── LECTURA RÁPIDA ────────────────────────────────────────────────────────────

async def quick_read() -> dict:
    """Lectura rápida: quote del chart activo. No interfiere con el trader.

    Returns:
        Dict con precio del chart activo + metadata.
        Vacío si TV no está disponible.
    """
    tv = await get_tv_commands()
    if tv is None:
        return {}

    results = {}
    try:
        quote = await tv.get_quote()
        if quote and quote.get("success"):
            symbol = quote.get("symbol", "UNKNOWN")
            price = quote.get("close") or quote.get("last")
            if price:
                results[symbol] = {
                    "price":     round(float(price), 5),
                    "bid":       quote.get("bid"),
                    "ask":       quote.get("ask"),
                    "volume":    quote.get("volume"),
                    "timestamp": _now_utc(),
                    "source":    "tv_cdp",
                }
    except Exception as e:
        logger.warning(f"quick_read quote falló: {e}")

    _save_quick_snapshot(results)
    return results


def _save_quick_snapshot(results: dict) -> None:
    """Persiste snapshot de lectura rápida en price_snapshots."""
    if not results or not _DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        now = _now_utc()
        for symbol, data in results.items():
            instrument = symbol.split(":")[-1] if ":" in symbol else symbol
            conn.execute(
                "INSERT INTO price_snapshots (instrument, price, source, timestamp_utc) VALUES (?, ?, ?, ?)",
                (instrument, data["price"], "tv_cdp", now),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"_save_quick_snapshot falló: {e}")


# ── LECTURA PROFUNDA ──────────────────────────────────────────────────────────

async def deep_read(tabs_to_read: list[str], intention: str) -> dict:
    """Lectura profunda: cambia tabs y TFs para leer OHLCV + indicador Aetheer.

    INTERFIERE con el chart del trader. Al terminar restaura tab y TF original.

    Args:
        tabs_to_read: ["DXY", "EURUSD", "GBPUSD"] o subset
        intention: clave de TF_PROFILES

    Returns:
        Dict con datos por símbolo y timeframe.
    """
    tv = await get_tv_commands()
    if tv is None:
        return {"error": "TradingView no disponible"}

    timeframes = TF_PROFILES.get(intention, ["H1", "M15"])
    tf_values = [TF_MAP[tf] for tf in timeframes if tf in TF_MAP]

    symbols_config = {
        name: SYMBOL_CONFIG[name]
        for name in tabs_to_read
        if name in SYMBOL_CONFIG
    }

    if not symbols_config:
        return {"error": f"Ningún símbolo reconocido en: {tabs_to_read}"}

    # Fase 1: deep_read sin restore (lo haremos nosotros con la elección limpia)
    raw = await tv.deep_read(
        symbols_config=symbols_config,
        timeframes=tf_values,
        restore_to_symbol=None,  # default: restaurar al original; se sobreescribe abajo
    )

    # Fase 2: post-procesar, parsear Aetheer, validar
    results: dict = {}
    for symbol_name, tf_data in raw.items():
        results[symbol_name] = {}
        tv_symbol_full = SYMBOL_CONFIG.get(symbol_name, "")
        expected_sym = tv_symbol_full.split(":")[-1]

        for tf_val, data in tf_data.items():
            if not isinstance(data, dict) or "error" in data:
                results[symbol_name][tf_val] = data
                continue

            aetheer_raw = data.get("aetheer")
            if aetheer_raw:
                parsed = parse_aetheer_table(aetheer_raw)
                validation = validate_aetheer_data(
                    parsed,
                    expected_symbol=expected_sym,
                    expected_tf=tf_val,
                )
                data["aetheer_indicator"] = parsed
                data["aetheer_valid"]     = validation["valid"]
                if validation.get("errors"):
                    data["aetheer_errors"] = validation["errors"]
                if validation.get("warnings"):
                    data["aetheer_warnings"] = validation["warnings"]

            data["timestamp"]  = _now_utc()
            data["from_cache"] = False

            results[symbol_name][tf_val] = data
            # Solo persistir lecturas verificadas: evita envenenar cache con
            # OHLCV/Aetheer cross-contaminado cuando el chart hizo drift de
            # símbolo (ver guards en tv_commands.deep_read).
            if data.get("symbol_verified", True) and "error" not in data:
                _save_deep_snapshot(symbol_name, tf_val, data)

    # Fase 3: elegir pair más limpio y dejar el chart ahí (si aplica)
    try:
        winner = _choose_cleaner_pair(results)
        winner_tv_symbol = SYMBOL_CONFIG.get(winner)
        if winner_tv_symbol:
            current_state = await tv.get_chart_state()
            current_symbol = (current_state or {}).get("symbol") or ""
            if current_symbol.upper() != winner_tv_symbol.upper():
                await tv.set_symbol(winner_tv_symbol)
                logger.info(f"deep_read restore: chart dejado en {winner_tv_symbol} (pair más limpio)")
    except Exception as e:
        logger.warning(f"deep_read restore heuristic falló: {e}")

    return results


def _choose_cleaner_pair(results: dict) -> str:
    """Heurística: elige el par (EURUSD|GBPUSD) con estructura más limpia
    para que el trader opere la sesión.

    Scoring (menor = más limpio), evaluado sobre el indicador Aetheer en H1:
      - ema_align == "mixed"              → +3 (estructura rota)
      - price_phase == "expansion"        → +2 (ruptura ya hecha, más ruido)
      - price_phase == "transition"       → +1
      - price_phase == "compression"      → 0  (ideal: acumulación pre-ruptura)
      - rsi_div in {bull_div, bear_div}   → +1 (divergencia añade ambigüedad)

    Fallbacks:
      - Si Aetheer está ausente en H1, usa M15.
      - Si ambos pairs carecen de Aetheer, default EURUSD (mayor liquidez
        durante overlap London-NY).
      - Si solo un candidato tiene datos utilizables, ese gana.
    """
    scores: dict[str, float] = {}
    for pair in RESTORE_CANDIDATES:
        if pair not in results:
            continue
        tf_data = results[pair]
        aetheer = None
        for tf_pref in ("60", "15"):  # H1, luego M15
            block = tf_data.get(tf_pref)
            if isinstance(block, dict) and block.get("aetheer_valid"):
                aetheer = block.get("aetheer_indicator")
                if aetheer:
                    break
        if not aetheer:
            continue
        s = 0.0
        if aetheer.get("ema_align") == "mixed":
            s += 3
        phase = aetheer.get("price_phase")
        if phase == "expansion":
            s += 2
        elif phase == "transition":
            s += 1
        if aetheer.get("rsi_div") in ("bull_div", "bear_div"):
            s += 1
        scores[pair] = s

    if not scores:
        return "EURUSD"
    return min(scores, key=scores.get)


# ── CACHE SQLITE ──────────────────────────────────────────────────────────────

def _should_reread(symbol: str, timeframe: str) -> bool:
    """Determinar si necesitamos releer basado en cache TTL."""
    ttl_key = f"deep_{timeframe}"
    ttl = CACHE_TTL.get(ttl_key, 300)
    try:
        if not _DB_PATH.exists():
            return True
        conn = sqlite3.connect(str(_DB_PATH))
        row = conn.execute(
            "SELECT timestamp_utc FROM deep_snapshots WHERE symbol = ? AND timeframe = ? ORDER BY timestamp_utc DESC LIMIT 1",
            (symbol, timeframe),
        ).fetchone()
        conn.close()
        if not row:
            return True
        ts  = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > ttl
    except Exception:
        return True


def _get_cached_deep_snapshot(symbol: str, timeframe: str) -> Optional[dict]:
    """Recuperar último snapshot profundo del cache SQLite."""
    try:
        if not _DB_PATH.exists():
            return None
        conn = sqlite3.connect(str(_DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM deep_snapshots WHERE symbol = ? AND timeframe = ? ORDER BY timestamp_utc DESC LIMIT 1",
            (symbol, timeframe),
        ).fetchone()
        conn.close()
        if not row:
            return None
        result: dict = {"timeframe": row["timeframe"], "timestamp": row["timestamp_utc"]}
        if row["ohlcv_json"]:
            result["ohlcv"] = json.loads(row["ohlcv_json"])
        if row["aetheer_data_json"]:
            result["aetheer_indicator"] = json.loads(row["aetheer_data_json"])
        if row["native_indicators_json"]:
            result["native_indicators"] = json.loads(row["native_indicators_json"])
        return result
    except Exception as e:
        logger.warning(f"_get_cached_deep_snapshot falló: {e}")
        return None


def _save_deep_snapshot(symbol: str, timeframe: str, data: dict) -> None:
    """Persistir snapshot profundo en SQLite."""
    try:
        if not _DB_PATH.exists():
            return
        conn = sqlite3.connect(str(_DB_PATH))
        conn.execute(
            """INSERT INTO deep_snapshots
               (symbol, timeframe, ohlcv_json, aetheer_data_json,
                native_indicators_json, timestamp_utc)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                symbol,
                timeframe,
                json.dumps(data.get("ohlcv")) if data.get("ohlcv") else None,
                json.dumps(data.get("aetheer_indicator")) if data.get("aetheer_indicator") else None,
                json.dumps(data.get("native_indicators") or data.get("studies")) if (data.get("native_indicators") or data.get("studies")) else None,
                data.get("timestamp", _now_utc()),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"_save_deep_snapshot falló: {e}")


# ── ORQUESTADOR PRINCIPAL ─────────────────────────────────────────────────────

async def read_market_data(
    intention: str, specific_pair: Optional[str] = None
) -> dict:
    """Función principal de lectura de mercado según intención.

    Args:
        intention: "full_analysis", "validate_setup", "macro_question",
                   "sudden_move", "data_point", "heartbeat"
        specific_pair: Para validate_setup/sudden_move (e.g., "EURUSD")

    Returns:
        Estructura completa con precios + datos profundos.
    """
    result: dict = {
        "intention":      intention,
        "timestamp":      _now_utc(),
        "operating_mode": "ONLINE",
        "prices":         {},
        "deep_data":      {},
    }

    if not is_tv_available():
        result["operating_mode"] = "OFFLINE"
        result["warning"] = "TradingView no disponible. Solo datos de cache/fallback."
        return result

    # Lectura rápida siempre (no interfiere con el trader)
    result["prices"] = await quick_read()

    # Lectura profunda solo si la intención lo requiere
    if intention in TF_PROFILES:
        if intention == "full_analysis":
            tabs = ["DXY", "EURUSD", "GBPUSD"]
        elif intention == "validate_setup":
            pair = specific_pair or "EURUSD"
            tabs = ["DXY", pair]
        elif intention == "macro_question":
            tabs = ["DXY"]
        elif intention == "sudden_move":
            pair = specific_pair or "EURUSD"
            tabs = [pair]
        else:
            tabs = []

        if tabs:
            result["deep_data"] = await deep_read(tabs, intention)

    return result
