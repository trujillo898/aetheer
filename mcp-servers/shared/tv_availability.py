"""
Detector de disponibilidad de TradingView MCP.
Usado por price-feed para decidir si TV es fuente primaria de datos.

CLI interface (tradingview-mcp):
  node cli status              → health check (JSON con success: true/false)
  node cli symbol <SYM>        → cambia símbolo activo del gráfico, espera a que cargue
  node cli quote               → quote del gráfico activo (close, last, bid?, ask?, time?)
  node cli ohlcv [--summary]   → barras OHLCV del gráfico activo
  node cli values              → valores de indicadores del gráfico activo
  node cli screenshot -r chart → screenshot del gráfico activo

Limitación MVP: get_tv_quote y get_tv_ohlcv cambian el símbolo activo del gráfico
del trader antes de leer datos. Alternativa ideal: multi-pane con símbolos fijos +
leer por pane sin cambiar el gráfico.
"""

import json
import subprocess
import time
from pathlib import Path

_TV_CLI = Path.home() / "tradingview-mcp" / "src" / "cli" / "index.js"

# Cache de estado — evita health check en cada consulta
_tv_status: dict = {"available": False, "last_check": 0.0, "ttl": 30}


def is_tv_available(force_check: bool = False) -> bool:
    """Verifica si TradingView MCP está disponible vía CDP.

    Cachea el resultado 30 segundos para minimizar overhead.
    Si tradingview-mcp no está instalado en ~/tradingview-mcp, retorna False.

    Returns:
        True si TV Desktop está corriendo con --remote-debugging-port=9222.
    """
    now = time.time()
    if not force_check and (now - _tv_status["last_check"]) < _tv_status["ttl"]:
        return _tv_status["available"]

    available = False
    try:
        if not _TV_CLI.exists():
            # tradingview-mcp no instalado — fallo silencioso, no es un error
            _tv_status["available"] = False
            _tv_status["last_check"] = now
            return False

        result = subprocess.run(
            ["node", str(_TV_CLI), "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            available = data.get("success") is True and data.get("cdp_connected") is True
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, Exception):
        available = False

    _tv_status["available"] = available
    _tv_status["last_check"] = now
    return available


def get_tv_quote(symbol: str) -> dict | None:
    """Obtiene quote del gráfico de TradingView.

    Cambia el símbolo activo del gráfico (setSymbol + waitForChartReady),
    luego lee el último bar y datos de precio.

    Args:
        symbol: Símbolo en formato TV con exchange (e.g. "TVC:DXY", "OANDA:EURUSD").

    Returns:
        Dict con { success, symbol, close, last, volume, bid?, ask?, time?, ... }
        o None si falla.
    """
    try:
        # setSymbol espera a que el gráfico termine de cargar (waitForChartReady)
        sym_result = subprocess.run(
            ["node", str(_TV_CLI), "symbol", symbol],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # Retorno de `symbol` es { success, symbol, resolution } — no es el quote
        if sym_result.returncode != 0:
            return None

        # Leer quote del gráfico activo (ya cargado con el símbolo correcto)
        q_result = subprocess.run(
            ["node", str(_TV_CLI), "quote"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if q_result.returncode == 0 and q_result.stdout.strip():
            data = json.loads(q_result.stdout)
            if data.get("success") and (data.get("close") or data.get("last")):
                return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, Exception):
        pass
    return None


def get_tv_ohlcv(symbol: str, summary: bool = True) -> dict | None:
    """Obtiene datos OHLCV del gráfico de TradingView.

    Cambia el símbolo activo, luego lee las barras históricas del gráfico.

    Args:
        symbol: Símbolo en formato TV con exchange (e.g. "TVC:DXY").
        summary: True → stats compactos (high/low/open/close/range/change_pct).
                 False → todas las barras (~100 barras).

    Returns:
        Dict con datos OHLCV o None si falla.
    """
    try:
        # Navegar al símbolo
        sym_result = subprocess.run(
            ["node", str(_TV_CLI), "symbol", symbol],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if sym_result.returncode != 0:
            return None

        # Leer OHLCV del gráfico activo
        args = ["node", str(_TV_CLI), "ohlcv"]
        if summary:
            args.append("--summary")
        ohlcv_result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if ohlcv_result.returncode == 0 and ohlcv_result.stdout.strip():
            data = json.loads(ohlcv_result.stdout)
            if data.get("success"):
                return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, Exception):
        pass
    return None


def get_tv_study_values() -> dict | None:
    """Lee valores actuales de indicadores del gráfico activo.

    No cambia el símbolo — lee los estudios visibles en el gráfico actual.
    Retorna ATR, RSI, MACD y cualquier otro indicador con plot().

    Returns:
        Dict con { success, study_count, studies: [{name, values}] } o None si falla.
    """
    try:
        result = subprocess.run(
            ["node", str(_TV_CLI), "values"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            if data.get("success"):
                return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, Exception):
        pass
    return None


def get_tv_screenshot() -> str | None:
    """Captura screenshot del gráfico activo.

    Returns:
        Ruta del archivo screenshot o base64 string, o None si falla.
    """
    try:
        result = subprocess.run(
            ["node", str(_TV_CLI), "screenshot", "--region", "chart"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    return None
