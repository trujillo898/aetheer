"""
tv_availability.py — Disponibilidad y acceso a TradingView Desktop.

Reemplaza la implementación anterior basada en subprocess Node.js.
Ahora usa TVBridge (CDP Python nativo) directamente.

API pública:
  is_tv_available()       → bool  (sync, HTTP check, cacheado 30s)
  get_tv_commands()       → async, singleton de TVCommands
  get_tv_quote()          → async
  get_tv_ohlcv()          → async
  get_tv_study_values()   → async
  get_tv_screenshot()     → async (no implementado en CDP puro, retorna None)
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

from .tv_bridge import TVBridge
from .tv_commands import TVCommands

logger = logging.getLogger("aetheer.tv_availability")

CDP_PORT = 9222
CDP_HOST = "localhost"

# ── DISPONIBILIDAD SYNC (HTTP check) ──────────────────────────────────────────

_tv_status: dict = {"available": False, "last_check": 0.0, "ttl": 30.0}


def is_tv_available(force_check: bool = False) -> bool:
    """Verificar si TradingView Desktop está corriendo con CDP habilitado.

    Usa HTTP GET a localhost:9222/json/list — no requiere WebSocket.
    Cachea el resultado 30 segundos para minimizar overhead.

    Returns:
        True si TradingView Desktop responde en puerto 9222.
    """
    now = time.time()
    if not force_check and (now - _tv_status["last_check"]) < _tv_status["ttl"]:
        return _tv_status["available"]

    available = False
    try:
        resp = httpx.get(
            f"http://{CDP_HOST}:{CDP_PORT}/json/list",
            timeout=2.0,
        )
        targets = resp.json()
        available = any(
            t.get("type") == "page"
            and "tradingview" in t.get("url", "").lower()
            for t in targets
        )
    except Exception:
        available = False

    _tv_status["available"] = available
    _tv_status["last_check"] = now
    return available


# ── SINGLETON TVCommands ───────────────────────────────────────────────────────

_tv_bridge: Optional[TVBridge] = None
_tv_commands: Optional[TVCommands] = None
_tv_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    """Lazy-init del lock (debe llamarse desde contexto async)."""
    global _tv_lock
    if _tv_lock is None:
        _tv_lock = asyncio.Lock()
    return _tv_lock


async def get_tv_commands() -> Optional[TVCommands]:
    """Obtener (o crear) la instancia singleton de TVCommands.

    Reconecta automáticamente si la conexión se perdió.
    Retorna None si TradingView no está disponible.
    """
    global _tv_bridge, _tv_commands

    lock = _get_lock()
    async with lock:
        # Verificar disponibilidad HTTP primero (barato)
        if not is_tv_available():
            return None

        # Si ya tenemos una conexión válida, retornarla
        if _tv_commands is not None and _tv_bridge is not None and _tv_bridge.connected:
            return _tv_commands

        # Crear o reconectar
        _tv_bridge = TVBridge()
        if await _tv_bridge.connect():
            _tv_commands = TVCommands(_tv_bridge)
            return _tv_commands

        logger.warning("TVBridge: no se pudo conectar a TradingView Desktop")
        return None


# ── FUNCIONES DE ACCESO ASYNC ─────────────────────────────────────────────────

async def get_tv_quote() -> Optional[dict]:
    """Quote del chart activo de TradingView.

    Returns dict con {success, symbol, close, last, time, volume, ...} o None si falla.
    """
    try:
        tv = await get_tv_commands()
        if tv is None:
            return None
        return await tv.get_quote()
    except Exception as e:
        logger.debug(f"get_tv_quote falló: {e}")
        return None


async def get_tv_ohlcv(summary: bool = True) -> Optional[dict]:
    """OHLCV del chart activo de TradingView.

    Args:
        summary: True → stats compactos. False → todas las barras (~100).

    Returns dict con OHLCV data o None si falla.

    NOTA: No acepta parámetro symbol — lee siempre el chart activo.
    Para multi-símbolo usar TVCommands.deep_read() vía read_market_data_tool.
    """
    try:
        tv = await get_tv_commands()
        if tv is None:
            return None
        return await tv.get_ohlcv(summary=summary)
    except Exception as e:
        logger.debug(f"get_tv_ohlcv falló: {e}")
        return None


async def get_tv_study_values() -> Optional[dict]:
    """Valores de indicadores nativos del chart activo.

    Returns dict con {success, study_count, studies} o None si falla.
    """
    try:
        tv = await get_tv_commands()
        if tv is None:
            return None
        return await tv.get_study_values()
    except Exception as e:
        logger.debug(f"get_tv_study_values falló: {e}")
        return None


async def get_tv_screenshot() -> Optional[str]:
    """Screenshot del chart activo.

    No implementado en CDP puro (requería CLI Node.js).
    Retorna None silenciosamente para mantener compatibilidad.
    """
    logger.debug("get_tv_screenshot: no implementado en CDP nativo")
    return None


async def get_tv_chart_state() -> Optional[dict]:
    """Estado del chart activo: símbolo, timeframe, indicadores."""
    try:
        tv = await get_tv_commands()
        if tv is None:
            return None
        return await tv.get_chart_state()
    except Exception as e:
        logger.debug(f"get_tv_chart_state falló: {e}")
        return None
