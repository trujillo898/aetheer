"""
tv_market_reader.py — Lectura de datos de mercado desde TradingView Desktop.

Dos modos:
  - quick_read(): quote del chart activo. No toca el chart. ~2-3s.
  - deep_read():  tab switch + TF switch. Lee OHLCV + Pine tables. ~24-30s.
    INTERFIERE con el chart del trader (restaura siempre al terminar).

Requiere:
  - TV Desktop corriendo con --remote-debugging-port=9222
  - Para deep_read: 3 tabs → Tab 0=TVC:DXY, Tab 1=OANDA:EURUSD, Tab 2=OANDA:GBPUSD
  - Indicador Aetheer (D009) cargado en cada tab para deep_read

Reemplaza la implementación anterior basada en subprocess Node.js.
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

# Configuración de tabs — DEBE coincidir con el setup del trader en TradingView
TAB_CONFIG = {
    "DXY":    {"index": 0, "symbol": "TVC:DXY"},
    "EURUSD": {"index": 1, "symbol": "OANDA:EURUSD"},
    "GBPUSD": {"index": 2, "symbol": "OANDA:GBPUSD"},
}

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

    tabs_config = {
        name: TAB_CONFIG[name]["index"]
        for name in tabs_to_read
        if name in TAB_CONFIG
    }

    if not tabs_config:
        return {"error": f"Ningún tab reconocido en: {tabs_to_read}"}

    # Delegar al TVCommands.deep_read (maneja restore internamente)
    raw = await tv.deep_read(tabs_config=tabs_config, timeframes=tf_values)

    # Post-procesar: parsear indicador Aetheer y validar
    results = {}
    for symbol_name, tf_data in raw.items():
        results[symbol_name] = {}
        tab_cfg = TAB_CONFIG.get(symbol_name, {})

        for tf_val, data in tf_data.items():
            if not isinstance(data, dict) or "error" in data:
                results[symbol_name][tf_val] = data
                continue

            # Parsear Pine tables del indicador Aetheer
            aetheer_raw = data.get("aetheer")
            if aetheer_raw:
                parsed = parse_aetheer_table(aetheer_raw)
                expected_sym = tab_cfg.get("symbol", "").split(":")[-1]
                # TF inverso (tf_val "D" → "D1", "240" → "H4", etc.)
                tf_inv = {v: k for k, v in TF_MAP.items()}
                expected_tf_name = tf_inv.get(tf_val, tf_val)

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
            _save_deep_snapshot(symbol_name, tf_val, data)

    return results


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
