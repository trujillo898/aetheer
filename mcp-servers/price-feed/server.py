"""Aetheer Price Feed MCP Server.

Provides real-time price data for DXY, EURUSD, and GBPUSD via a cascade
of sources: TradingView → Alpha Vantage → TradingEconomics → Investing.com → XE.com → Yahoo Finance.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Add parent paths for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from sources import get_correlations_prices, get_price_cascade, TV_SYMBOLS
from shared.cache import TTLCache, TV_CACHE_TTLS

logging.basicConfig(level=logging.INFO, format="[price-feed] %(levelname)s %(message)s")
logger = logging.getLogger("aetheer.price-feed")

cache = TTLCache()

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent.parent.parent / "db" / "aetheer.db"))

mcp = FastMCP("price-feed")


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _store_snapshot(instrument: str, price: float, source: str, timestamp_utc: str) -> None:
    try:
        db = _get_db()
        db.execute(
            "INSERT INTO price_snapshots (instrument, price, source, timestamp_utc) VALUES (?, ?, ?, ?)",
            (instrument, price, source, timestamp_utc),
        )
        db.commit()
        db.close()
    except Exception as e:
        logger.warning(f"Failed to store snapshot: {e}")


@mcp.tool()
async def get_price(instrument: str) -> str:
    """Get current price for an instrument (DXY, EURUSD, GBPUSD).

    Tries sources in cascade: TradingView → Alpha Vantage → TradingEconomics → Investing.com → XE.com → Yahoo Finance.
    Returns JSON with price, source, and timestamp.
    If no source responds, returns KILL_SWITCH error.

    Args:
        instrument: One of "DXY", "EURUSD", "GBPUSD"
    """
    instrument = instrument.upper().strip()
    if instrument not in ("DXY", "EURUSD", "GBPUSD"):
        return json.dumps({"error": "Invalid instrument. Use DXY, EURUSD, or GBPUSD."})

    cache_key = f"price:{instrument}"
    cached = cache.get(cache_key)
    if cached is not None:
        return json.dumps({**cached, "from_cache": True})

    result = await get_price_cascade(instrument)

    if "error" not in result:
        _store_snapshot(result["instrument"], result["price"], result["source"], result["timestamp_utc"])
        cache.set(cache_key, result, ttl_seconds=60)

    result["from_cache"] = False
    return json.dumps(result)


@mcp.tool()
async def get_all_prices() -> str:
    """Get current prices for all tracked instruments (DXY, EURUSD, GBPUSD).

    Returns JSON array with price data for each instrument.
    """
    cache_key = "all_prices"
    cached = cache.get(cache_key)
    if cached is not None:
        return json.dumps([{**r, "from_cache": True} for r in cached])

    results = []
    for inst in ("DXY", "EURUSD", "GBPUSD"):
        result = await get_price_cascade(inst)
        if "error" not in result:
            _store_snapshot(result["instrument"], result["price"], result["source"], result["timestamp_utc"])
        result["from_cache"] = False
        results.append(result)

    cache.set(cache_key, results, ttl_seconds=60)
    return json.dumps(results)


@mcp.tool()
async def get_price_history(instrument: str, hours: int = 24) -> str:
    """Get price history from stored snapshots.

    Args:
        instrument: One of "DXY", "EURUSD", "GBPUSD"
        hours: Number of hours to look back (default: 24)
    """
    instrument = instrument.upper().strip()
    cache_key = f"history:{instrument}:{hours}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # already JSON string

    try:
        db = _get_db()
        cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = db.execute(
            """SELECT instrument, price, source, timestamp_utc
               FROM price_snapshots
               WHERE instrument = ?
                 AND timestamp_utc >= datetime(?, '-' || ? || ' hours')
               ORDER BY timestamp_utc DESC
               LIMIT 500""",
            (instrument, cutoff, hours),
        ).fetchall()
        db.close()
        result = json.dumps([dict(r) for r in rows])
        cache.set(cache_key, result, ttl_seconds=300)
        return result
    except Exception as e:
        logger.error(f"Failed to query history: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_candles(instrument: str, granularity: str = "H1", count: int = 100) -> str:
    """Get OHLCV candle data for an instrument.

    Uses TradingView as primary source when available.
    Falls back to SQLite price history snapshots.

    Args:
        instrument: One of "DXY", "EURUSD", "GBPUSD"
        granularity: Candle period hint (H1, H4, D, etc.) — informational when using TV
        count: Number of candles to return (default: 100)
    """
    instrument = instrument.upper().strip()
    cache_key = f"candles:{instrument}:{granularity}:{count}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Prioridad 0: TradingView
    try:
        from shared.tv_availability import is_tv_available, get_tv_ohlcv
        if is_tv_available():
            tv_symbol = TV_SYMBOLS.get(instrument)
            if tv_symbol:
                data = get_tv_ohlcv(tv_symbol, summary=False)
                if data:
                    result = json.dumps({
                        "instrument": instrument,
                        "granularity": granularity,
                        "candles": data,
                        "source": "tradingview",
                        "from_cache": False,
                    }, indent=2)
                    cache.set(cache_key, result, ttl_seconds=TV_CACHE_TTLS["tv_ohlcv"])
                    return result
    except Exception as e:
        logger.warning(f"TV candles failed for {instrument}: {e}")

    # Fallback: price history from SQLite
    try:
        db = _get_db()
        rows = db.execute(
            """SELECT price, source, timestamp_utc
               FROM price_snapshots
               WHERE instrument = ?
               ORDER BY timestamp_utc DESC
               LIMIT ?""",
            (instrument, count),
        ).fetchall()
        db.close()
        fallback = [{"time": r["timestamp_utc"], "close": r["price"]} for r in rows]
        result = json.dumps({
            "instrument": instrument,
            "granularity": granularity,
            "candles": fallback,
            "count": len(fallback),
            "source": "sqlite_history",
            "note": "TradingView unavailable. Limited data from price snapshots.",
            "from_cache": False,
        })
        cache.set(cache_key, result, ttl_seconds=300)
        return result
    except Exception as e:
        logger.error(f"Failed to get candles: {e}")
        return json.dumps({"error": str(e)})


CORRELATION_INSTRUMENTS = ["XAUUSD", "VIX", "SPX", "US10Y", "US02Y", "USOIL", "DE10Y", "GB10Y"]


@mcp.tool()
async def get_correlations() -> str:
    """Get prices for correlated assets (Gold, VIX, S&P500, US yields, Oil, Bund, Gilt).

    Uses TradingView as primary source when available, Yahoo Finance as fallback.
    DE10Y and GB10Y are available only via TradingView (not on Yahoo Finance).

    Returns JSON dict: {instrument: {price, change_pct, source, timestamp}}
    """
    cache_key = "correlations"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    results = await get_correlations_prices(CORRELATION_INSTRUMENTS)
    output = json.dumps(results, indent=2)
    cache.set(cache_key, output, ttl_seconds=TV_CACHE_TTLS["tv_quote"])
    return output


@mcp.tool()
async def get_ohlcv_for_analysis(instrument: str, summary: bool = False) -> str:
    """Get OHLCV data optimized for liquidity and price-structure analysis.

    Uses TradingView as primary source (reads trader's exact chart data).
    Falls back to SQLite price history when TV unavailable.

    Args:
        instrument: One of "DXY", "EURUSD", "GBPUSD"
        summary: True → compact stats (high/low/open/close/range/change_pct).
                 False → full bar data (~100 bars). Use False for structure analysis.
    """
    instrument = instrument.upper().strip()
    cache_key = f"ohlcv_analysis:{instrument}:{'summary' if summary else 'full'}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    ttl = TV_CACHE_TTLS["tv_ohlcv"] if summary else TV_CACHE_TTLS["tv_ohlcv_full"]

    # Prioridad 0: TradingView
    try:
        from shared.tv_availability import is_tv_available, get_tv_ohlcv
        if is_tv_available():
            tv_symbol = TV_SYMBOLS.get(instrument)
            if tv_symbol:
                data = get_tv_ohlcv(tv_symbol, summary=summary)
                if data:
                    result = json.dumps({
                        "instrument": instrument,
                        "source": "tradingview",
                        "data": data,
                    }, indent=2)
                    cache.set(cache_key, result, ttl_seconds=ttl)
                    return result
    except Exception as e:
        logger.warning(f"TV OHLCV failed for {instrument}: {e}")

    # Fallback: SQLite history snapshots
    count = 20 if summary else 100
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT price, timestamp_utc FROM price_snapshots WHERE instrument = ? ORDER BY timestamp_utc DESC LIMIT ?",
            (instrument, count),
        ).fetchall()
        db.close()
        fallback = [{"time": r["timestamp_utc"], "close": r["price"]} for r in rows]
        result = json.dumps({
            "instrument": instrument,
            "source": "sqlite_history",
            "note": "TradingView unavailable — limited data from price snapshots.",
            "data": {"bars": fallback, "bar_count": len(fallback)},
        }, indent=2)
        cache.set(cache_key, result, ttl_seconds=300)
        return result
    except Exception as e:
        logger.error(f"Failed to get OHLCV for analysis: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_chart_indicators() -> str:
    """Read current indicator values directly from the trader's TradingView chart.

    Returns ATR, RSI, MACD, and any other indicator with a plot() call.
    Only works when TradingView is connected.

    This gives exact consistency between Aetheer's analysis and what the trader sees.
    """
    try:
        from shared.tv_availability import is_tv_available, get_tv_study_values
        if not is_tv_available():
            return json.dumps({
                "available": False,
                "error": "TradingView not connected. Start TV with --remote-debugging-port=9222.",
            })
        values = get_tv_study_values()
        if values:
            return json.dumps({
                "available": True,
                "source": "tradingview",
                "note": "Values read directly from trader's active chart",
                "study_count": values.get("study_count", 0),
                "studies": values.get("studies", []),
            }, indent=2)
        return json.dumps({"available": True, "error": "Could not read indicator values from chart."})
    except Exception as e:
        logger.error(f"Failed to get chart indicators: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def capture_chart_screenshot() -> str:
    """Capture a screenshot of the trader's current TradingView chart.

    Useful for visual price structure analysis.
    Only works when TradingView is connected.

    Returns the screenshot file path or base64 data.
    """
    try:
        from shared.tv_availability import is_tv_available, get_tv_screenshot
        if not is_tv_available():
            return json.dumps({
                "available": False,
                "error": "TradingView not connected.",
            })
        screenshot = get_tv_screenshot()
        if screenshot:
            return json.dumps({
                "available": True,
                "source": "tradingview",
                "result": screenshot,
            })
        return json.dumps({"available": True, "error": "Screenshot capture failed."})
    except Exception as e:
        logger.error(f"Failed to capture screenshot: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def read_market_data_tool(
    intention: str = "full_analysis",
    specific_pair: str | None = None,
) -> dict:
    """Read market data at appropriate depth based on user's intention.

    Two modes:
    - Quick read (quote_get): prices of all symbols without touching the chart. ~2-3s.
    - Deep read (tab+TF switch): OHLCV + Aetheer indicator per TF. ~24-30s.
      INTERFERES with trader's chart temporarily (restores afterward).

    Requires TradingView Desktop running with --remote-debugging-port=9222.
    For deep read, requires 3 tabs: Tab 0=DXY, Tab 1=EURUSD, Tab 2=GBPUSD
    with Aetheer indicator loaded on each.

    Args:
        intention: What the user wants to do.
            "full_analysis" = all symbols, 4 TFs (D1, H4, H1, M15). ~24-30s.
            "validate_setup" = DXY + specific pair, 2 TFs (H1, M15). ~8-10s.
            "macro_question" = DXY only, 2 TFs (D1, H4). ~4-6s.
            "sudden_move" = specific pair, 2 TFs (M15, H1). ~4-6s.
            "data_point" = prices only, no deep read. ~2s.
            "heartbeat" = prices only, no deep read. ~2s.
        specific_pair: For validate_setup/sudden_move, which pair (e.g. "EURUSD").
    """
    from shared.tv_market_reader import read_market_data
    return read_market_data(intention, specific_pair)


if __name__ == "__main__":
    mcp.run()
