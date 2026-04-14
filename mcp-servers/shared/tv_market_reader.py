"""Multi-timeframe market reader via TradingView MCP.

Dos modos de lectura:
  - quick_read(): quote_get de múltiples símbolos. No toca el chart. ~2-3s.
  - deep_read(): tab switch + TF switch. Lee OHLCV + Pine tables. ~24-30s.

Requiere:
  - TV Desktop corriendo con --remote-debugging-port=9222
  - 3 tabs abiertos: Tab 0 = TVC:DXY, Tab 1 = OANDA:EURUSD, Tab 2 = OANDA:GBPUSD
  - Indicador Aetheer (D009) cargado en cada tab
"""

import asyncio
import json
import logging
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("aetheer.shared.tv_market_reader")

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

_TV_CLI = Path.home() / "tradingview-mcp" / "src" / "cli" / "index.js"
_DB_PATH = Path(__file__).resolve().parent.parent.parent / "db" / "aetheer.db"

# Símbolos para lectura rápida (quote_get — no toca el chart)
SYMBOLS_PRICE = ["TVC:DXY", "OANDA:EURUSD", "OANDA:GBPUSD"]
SYMBOLS_CORRELATIONS = [
    "TVC:US10Y", "TVC:VIX", "OANDA:XAUUSD", "SP:SPX", "TVC:USOIL",
]

# Configuración de tabs — DEBE coincidir con el setup en TradingView
TAB_CONFIG = {
    "DXY":    {"index": 0, "symbol": "TVC:DXY"},
    "EURUSD": {"index": 1, "symbol": "OANDA:EURUSD"},
    "GBPUSD": {"index": 2, "symbol": "OANDA:GBPUSD"},
}

# Mapeo de timeframes a valores del MCP
TF_MAP = {
    "D1":  "D",
    "H4":  "240",
    "H1":  "60",
    "M15": "15",
}

# Timeframes por intención
TF_PROFILES = {
    "full_analysis":   ["D1", "H4", "H1", "M15"],
    "validate_setup":  ["H1", "M15"],
    "macro_question":  ["D1", "H4"],
    "sudden_move":     ["M15", "H1"],
}

# Cache TTL por timeframe (segundos)
CACHE_TTL = {
    "quote_get": 60,
    "deep_D1":   900,    # 15 min
    "deep_H4":   600,    # 10 min
    "deep_H1":   300,    # 5 min
    "deep_M15":  120,    # 2 min
}

# Delay después de cambiar tab/TF. Empezar con 1.5s, optimizar con pruebas.
CHART_LOAD_DELAY_S = 1.5


# ═══════════════════════════════════════════════════════════════
# UTILIDADES TV MCP CLI
# ═══════════════════════════════════════════════════════════════

def _tv_cli(command: str, args: list[str] | None = None, timeout: int = 10) -> dict | None:
    """Ejecuta un comando del TV MCP CLI y retorna JSON parseado."""
    cmd = ["node", str(_TV_CLI), command]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, Exception) as e:
        logger.warning(f"TV CLI '{command}' failed: {e}")
    return None


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ═══════════════════════════════════════════════════════════════
# LECTURA RÁPIDA (quote_get — sin tocar chart)
# ═══════════════════════════════════════════════════════════════

def quick_read() -> dict:
    """Lectura rápida de precios vía quote_get. No interfiere con el trader.

    Returns:
        Dict con precio de cada símbolo + metadata.
    """
    results = {}
    all_symbols = SYMBOLS_PRICE + SYMBOLS_CORRELATIONS

    for symbol in all_symbols:
        data = _tv_cli("quote", [symbol])
        if data and data.get("success"):
            price = data.get("close") or data.get("last")
            if price:
                results[symbol] = {
                    "price": round(float(price), 5),
                    "bid": data.get("bid"),
                    "ask": data.get("ask"),
                    "change": data.get("change"),
                    "change_pct": data.get("change_percent"),
                    "volume": data.get("volume"),
                    "timestamp": _now_utc(),
                }

    _save_quick_snapshot(results)
    return results


def _save_quick_snapshot(results: dict):
    """Persiste snapshot de lectura rápida en price_snapshots."""
    try:
        if not _DB_PATH.exists():
            return
        conn = sqlite3.connect(str(_DB_PATH))
        now = _now_utc()
        for symbol, data in results.items():
            instrument = symbol.split(":")[-1] if ":" in symbol else symbol
            conn.execute(
                """INSERT INTO price_snapshots (instrument, price, source, timestamp_utc)
                   VALUES (?, ?, ?, ?)""",
                (instrument, data["price"], "tv_quote_get", now),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to save quick snapshot: {e}")


# ═══════════════════════════════════════════════════════════════
# LECTURA PROFUNDA (tab switch + TF switch)
# ═══════════════════════════════════════════════════════════════

def deep_read(tabs_to_read: list[str], intention: str) -> dict:
    """Lectura profunda: cambia tabs y TFs para leer OHLCV + indicador Aetheer.

    INTERFIERE con el chart del trader (cambia tabs y timeframes).
    Al terminar restaura tab y TF original.

    Args:
        tabs_to_read: ["DXY", "EURUSD", "GBPUSD"] o subset
        intention: clave de TF_PROFILES

    Returns:
        Dict con datos por símbolo y timeframe.
    """
    from shared.pine_parser import parse_aetheer_table, validate_aetheer_data

    timeframes = TF_PROFILES.get(intention, ["H1", "M15"])
    results = {}

    # 1. Guardar estado original del chart
    original_state = _tv_cli("state")
    original_tab_index = _get_active_tab_index()
    original_tf = original_state.get("timeframe", "60") if original_state else "60"

    try:
        for tab_name in tabs_to_read:
            tab_cfg = TAB_CONFIG.get(tab_name)
            if not tab_cfg:
                logger.warning(f"Tab '{tab_name}' no está en TAB_CONFIG")
                continue

            results[tab_name] = {}

            # 2. Cambiar al tab del símbolo
            switch_result = _tv_cli("tab-switch", [str(tab_cfg["index"])])
            if not switch_result or not switch_result.get("success"):
                results[tab_name]["error"] = f"No se pudo cambiar al tab {tab_cfg['index']}"
                continue

            time.sleep(CHART_LOAD_DELAY_S)

            # 3. Verificar símbolo correcto
            state = _tv_cli("state")
            if state and state.get("symbol") != tab_cfg["symbol"]:
                results[tab_name]["error"] = (
                    f"Tab {tab_cfg['index']} tiene {state.get('symbol')}, "
                    f"esperaba {tab_cfg['symbol']}"
                )
                continue

            # 4. Para cada timeframe
            for tf_name in timeframes:
                tf_value = TF_MAP.get(tf_name)
                if not tf_value:
                    continue

                # Verificar cache antes de releer
                if not _should_reread(tab_name, tf_name):
                    cached = _get_cached_deep_snapshot(tab_name, tf_name)
                    if cached:
                        cached["from_cache"] = True
                        results[tab_name][tf_name] = cached
                        continue

                # Cambiar timeframe
                tf_result = _tv_cli("set-timeframe", [tf_value])
                if not tf_result or not tf_result.get("success"):
                    results[tab_name][tf_name] = {"error": f"No se pudo cambiar a TF {tf_name}"}
                    continue

                time.sleep(CHART_LOAD_DELAY_S)

                tf_data = {}

                # Leer OHLCV summary
                ohlcv = _tv_cli("ohlcv", ["--summary"])
                tf_data["ohlcv"] = ohlcv if ohlcv and ohlcv.get("success") else None

                # Leer Pine tables del indicador Aetheer
                pine_raw = _tv_cli("pine-tables", ["--filter", "Aetheer"])
                if pine_raw:
                    parsed = parse_aetheer_table(pine_raw)
                    validation = validate_aetheer_data(
                        parsed,
                        expected_symbol=tab_cfg["symbol"].split(":")[-1],
                        expected_tf=tf_value,
                    )
                    tf_data["aetheer_indicator"] = parsed
                    tf_data["aetheer_valid"] = validation["valid"]
                    if validation["errors"]:
                        tf_data["aetheer_errors"] = validation["errors"]
                    if validation["warnings"]:
                        tf_data["aetheer_warnings"] = validation["warnings"]
                else:
                    tf_data["aetheer_indicator"] = None
                    tf_data["aetheer_valid"] = False
                    tf_data["aetheer_errors"] = ["data_get_pine_tables no retornó datos"]

                # Leer indicadores nativos como backup
                studies = _tv_cli("values")
                tf_data["native_indicators"] = studies if studies and studies.get("success") else None

                tf_data["timestamp"] = _now_utc()
                tf_data["timeframe"] = tf_name
                tf_data["from_cache"] = False

                results[tab_name][tf_name] = tf_data

                _save_deep_snapshot(tab_name, tf_name, tf_data)

    finally:
        # 5. SIEMPRE restaurar estado original
        try:
            if original_tab_index is not None:
                _tv_cli("tab-switch", [str(original_tab_index)])
            if original_tf:
                _tv_cli("set-timeframe", [original_tf])
        except Exception as e:
            logger.warning(f"Failed to restore original chart state: {e}")

    return results


def _get_active_tab_index() -> int | None:
    """Obtener índice del tab activo actual."""
    tabs = _tv_cli("tab-list")
    if tabs and isinstance(tabs, list):
        for tab in tabs:
            if isinstance(tab, dict) and tab.get("active"):
                return tab.get("index", 0)
    if tabs and isinstance(tabs, dict):
        tab_list = tabs.get("tabs", [])
        for tab in tab_list:
            if isinstance(tab, dict) and tab.get("active"):
                return tab.get("index", 0)
    return 0


# ═══════════════════════════════════════════════════════════════
# CACHE EN SQLITE
# ═══════════════════════════════════════════════════════════════

def _should_reread(symbol: str, timeframe: str) -> bool:
    """Determina si necesitamos releer basado en cache TTL."""
    ttl_key = f"deep_{timeframe}"
    ttl = CACHE_TTL.get(ttl_key, 300)

    try:
        if not _DB_PATH.exists():
            return True
        conn = sqlite3.connect(str(_DB_PATH))
        row = conn.execute(
            """SELECT timestamp_utc FROM deep_snapshots
               WHERE symbol = ? AND timeframe = ?
               ORDER BY timestamp_utc DESC LIMIT 1""",
            (symbol, timeframe),
        ).fetchone()
        conn.close()

        if not row:
            return True

        ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > ttl

    except Exception:
        return True


def _get_cached_deep_snapshot(symbol: str, timeframe: str) -> dict | None:
    """Recupera último snapshot profundo del cache."""
    try:
        if not _DB_PATH.exists():
            return None
        conn = sqlite3.connect(str(_DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM deep_snapshots
               WHERE symbol = ? AND timeframe = ?
               ORDER BY timestamp_utc DESC LIMIT 1""",
            (symbol, timeframe),
        ).fetchone()
        conn.close()

        if row:
            result = {
                "timeframe": row["timeframe"],
                "timestamp": row["timestamp_utc"],
            }
            if row["ohlcv_json"]:
                result["ohlcv"] = json.loads(row["ohlcv_json"])
            if row["aetheer_data_json"]:
                result["aetheer_indicator"] = json.loads(row["aetheer_data_json"])
            if row["native_indicators_json"]:
                result["native_indicators"] = json.loads(row["native_indicators_json"])
            return result

    except Exception as e:
        logger.warning(f"Failed to get cached deep snapshot: {e}")
    return None


def _save_deep_snapshot(symbol: str, timeframe: str, data: dict):
    """Persiste snapshot profundo en SQLite."""
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
                json.dumps(data.get("native_indicators")) if data.get("native_indicators") else None,
                data.get("timestamp", _now_utc()),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to save deep snapshot: {e}")


# ═══════════════════════════════════════════════════════════════
# ORQUESTADOR PRINCIPAL
# ═══════════════════════════════════════════════════════════════

def read_market_data(intention: str, specific_pair: str | None = None) -> dict:
    """Función principal de lectura de mercado según intención.

    Args:
        intention: "full_analysis", "validate_setup", "macro_question",
                   "sudden_move", "data_point", "heartbeat"
        specific_pair: Para validate_setup/sudden_move, qué par (e.g. "EURUSD")

    Returns:
        Estructura completa con precios + datos profundos.
    """
    from shared.tv_availability import is_tv_available

    result = {
        "intention": intention,
        "timestamp": _now_utc(),
        "operating_mode": "FULL",
        "prices": {},
        "deep_data": {},
    }

    if not is_tv_available():
        result["operating_mode"] = "DEGRADED"
        result["warning"] = "TradingView no disponible. Solo datos de cache/fallback."
        return result

    # Lectura rápida siempre (no interfiere con trader)
    result["prices"] = quick_read()

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
            result["deep_data"] = deep_read(tabs, intention)

    return result
