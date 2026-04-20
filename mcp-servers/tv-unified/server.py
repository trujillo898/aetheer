"""Aetheer tv-unified MCP Server (D010).

Exposes TradingView CDP as the ONLY source of truth for:
  - price/OHLCV/correlations/Aetheer indicator  (CDP)
  - news                                         (news-headlines.tradingview.com)
  - economic calendar                            (economic-calendar.tradingview.com)
  - system health                                (CDP probe + JSON APIs probe)

Operating modes (D010-collapsed):
  ONLINE  — TV reachable (CDP or HTTP APIs OK) → serve fresh or stale-within-30min.
  OFFLINE — neither CDP nor HTTP reachable AND no cache within 30min → refuse analysis.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Make both `mcp-servers/` and `tv-unified/` importable; dash-in-dirname forces sys.path hack
_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent  # .../mcp-servers
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
# Also add tv-unified/ itself so relative-looking submodules work when server.py is launched by path
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Import using tv_unified namespace shim (since directory has a dash)
import importlib.util


def _load_package() -> tuple:
    """Dynamically load tv-unified subpackages to sidestep the dash in the dir name."""
    base = _HERE
    specs = {
        "tv_unified_cache": base / "cache_store" / "snapshot.py",
        "tv_unified_bridge": base / "tv_bridge_ext.py",
        "tv_unified_prices": base / "sources" / "prices.py",
        "tv_unified_news_src": base / "sources" / "news.py",
        "tv_unified_cal_src": base / "sources" / "calendar.py",
        "tv_unified_schemas_price": base / "schemas" / "price.py",
        "tv_unified_schemas_news": base / "schemas" / "news.py",
        "tv_unified_schemas_cal": base / "schemas" / "calendar.py",
        "tv_unified_schemas_health": base / "schemas" / "health.py",
    }
    mods: dict = {}
    for name, path in specs.items():
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        mods[name] = module
    return mods


# Dash in directory name ("tv-unified") blocks a direct `import tv-unified`.
# Load the package via its __init__.py so subimports inside it use relative names.
_TV_INIT = _HERE / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "tv_unified_pkg",
    _TV_INIT,
    submodule_search_locations=[str(_HERE)],
)
tv_unified_pkg = importlib.util.module_from_spec(_spec)
sys.modules["tv_unified_pkg"] = tv_unified_pkg
_spec.loader.exec_module(tv_unified_pkg)

# Now import subpackages by their canonical tv_unified_pkg.<sub> path
from tv_unified_pkg.cache_store.snapshot import SnapshotCache  # noqa: E402
from tv_unified_pkg.tv_bridge_ext import TVBridgeExtended  # noqa: E402
from tv_unified_pkg.sources.prices import (  # noqa: E402
    CORRELATION_BASKET,
    get_chart_indicators,
    get_correlations,
    get_ohlcv,
    get_price,
)
from tv_unified_pkg.sources.news import get_news  # noqa: E402
from tv_unified_pkg.sources.calendar import get_economic_calendar  # noqa: E402
from tv_unified_pkg.schemas.price import (  # noqa: E402
    CorrelationsResponse,
    OHLCVResponse,
    PriceData,
)
from tv_unified_pkg.schemas.news import NewsResponse  # noqa: E402
from tv_unified_pkg.schemas.calendar import CalendarResponse  # noqa: E402
from tv_unified_pkg.schemas.health import HealthReport  # noqa: E402


logging.basicConfig(level=logging.INFO, format="[tv-unified] %(levelname)s %(message)s")
logger = logging.getLogger("aetheer.tv-unified")

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "db" / "tv_cache.sqlite")
DB_PATH = os.environ.get("TV_CACHE_DB", DEFAULT_DB_PATH)
CDP_PORT = int(os.environ.get("TV_CDP_PORT", "9222"))

mcp = FastMCP("tv-unified")

_cache = SnapshotCache(DB_PATH)
_bridge = TVBridgeExtended(port=CDP_PORT)


def _pop_meta(payload: dict) -> dict:
    """Pull the _cache_meta injected by sources/* into schema-compatible `meta` dict."""
    meta = payload.pop("_cache_meta", {}) if isinstance(payload, dict) else {}
    payload["meta"] = meta
    return payload


def _err(msg: str, **extra) -> str:
    return json.dumps({"error": msg, **extra})


# ─────────────────────────── TOOLS ───────────────────────────


@mcp.tool()
async def get_price_tool(instrument: str) -> str:
    """Current price for DXY/EURUSD/GBPUSD/etc. via TradingView CDP.

    Args:
        instrument: Instrument code (e.g., "DXY", "EURUSD", "GBPUSD").
    """
    try:
        raw = await get_price(_cache, instrument)
        raw = _pop_meta(raw)
        return PriceData(**raw).model_dump_json()
    except Exception as e:
        logger.warning(f"get_price error: {e}")
        return _err(str(e), instrument=instrument)


@mcp.tool()
async def get_ohlcv_tool(
    instrument: str, timeframe: str = "H1", intention: str = "full_analysis"
) -> str:
    """OHLCV bars + Aetheer indicator data via deep read.

    WARNING: This switches the trader's chart for ~24-30s. Use sparingly.

    Args:
        instrument: One of "DXY", "EURUSD", "GBPUSD" (must have a configured tab).
        timeframe: "D1" | "H4" | "H1" | "M15".
        intention: "full_analysis" | "validate_setup" | "macro_question" | "sudden_move".
    """
    try:
        raw = await get_ohlcv(_cache, instrument, timeframe, intention=intention)
        raw = _pop_meta(raw)
        # Schema expects bars; we return structure-light JSON (raw)
        return json.dumps(raw, default=str)
    except Exception as e:
        logger.warning(f"get_ohlcv error: {e}")
        return _err(str(e), instrument=instrument, timeframe=timeframe)


@mcp.tool()
async def get_correlations_tool() -> str:
    """Correlation basket (DXY + EURUSD + GBPUSD + XAUUSD + VIX + SPX + US10Y + US02Y)."""
    try:
        raw = await get_correlations(_cache)
        raw = _pop_meta(raw)
        return json.dumps(raw, default=str)
    except Exception as e:
        logger.warning(f"get_correlations error: {e}")
        return _err(str(e))


@mcp.tool()
async def get_chart_indicators_tool(instrument: str, timeframe: str = "H1") -> str:
    """Read Aetheer indicator JSON from the TradingView label.

    Args:
        instrument: "DXY" | "EURUSD" | "GBPUSD".
        timeframe: "D1" | "H4" | "H1" | "M15".
    """
    try:
        raw = await get_chart_indicators(_cache, instrument, timeframe)
        raw = _pop_meta(raw)
        return json.dumps(raw, default=str)
    except Exception as e:
        logger.warning(f"get_chart_indicators error: {e}")
        return _err(str(e), instrument=instrument, timeframe=timeframe)


@mcp.tool()
async def get_news_tool(
    symbol: str = "",
    category: str = "forex",
    lang: str = "es",
    limit: int = 50,
) -> str:
    """Financial news headlines from TradingView's internal API.

    Args:
        symbol: If provided (e.g., "OANDA:EURUSD"), returns symbol-specific headlines.
                Otherwise returns general news for `category`.
        category: "forex" | "stock" | "crypto" | "economic" (TV-defined buckets).
        lang: "es" | "en" | "pt" | ... — defaults to Spanish.
        limit: Max items to return (default 50).
    """
    try:
        raw = await get_news(
            _cache, _bridge,
            symbol=symbol or None, category=category, lang=lang, limit=limit,
        )
        raw = _pop_meta(raw)
        return json.dumps(raw, default=str)
    except Exception as e:
        logger.warning(f"get_news error: {e}")
        return _err(str(e))


@mcp.tool()
async def get_economic_calendar_tool(
    countries: str = "US,EU,GB",
    from_date: str = "",
    to_date: str = "",
    window_hours: int = 24,
) -> str:
    """Economic calendar events from TradingView.

    Args:
        countries: Comma-separated ISO2 codes (e.g., "US,EU,GB").
        from_date: ISO 8601 UTC start (optional; defaults to now).
        to_date:   ISO 8601 UTC end   (optional; defaults to now + window_hours).
        window_hours: Used only if from_date/to_date are empty.
    """
    try:
        raw = await get_economic_calendar(
            _cache, _bridge,
            countries=countries,
            from_date=from_date or None,
            to_date=to_date or None,
            window_hours=window_hours,
        )
        raw = _pop_meta(raw)
        return json.dumps(raw, default=str)
    except Exception as e:
        logger.warning(f"get_economic_calendar error: {e}")
        return _err(str(e))


@mcp.tool()
async def get_system_health() -> str:
    """Unified health report: CDP + news API + calendar API + cache fallback.

    Returns operating_mode = "ONLINE" if any primary channel works OR cache has
    data fresher than 30 min; otherwise "OFFLINE".
    """
    report = await _bridge.check_health()
    any_live = report["cdp_connected"] or report["news_api_ok"] or report["calendar_api_ok"]

    # Check cache fallback: is there ANY recent price snapshot?
    cache_fallback = False
    try:
        for inst in ("DXY", "EURUSD", "GBPUSD"):
            hit = _cache.get("price", inst, max_age_seconds=1800)
            if hit is not None:
                cache_fallback = True
                break
    except Exception as e:
        report["errors"]["cache_probe"] = str(e)

    if any_live:
        operating_mode = "ONLINE"
        status = "online"
    elif cache_fallback:
        operating_mode = "ONLINE"  # Degradado pero servible
        status = "online"
    else:
        operating_mode = "OFFLINE"
        status = "offline"

    out = HealthReport(
        status=status,
        cdp_connected=report["cdp_connected"],
        news_api_ok=report["news_api_ok"],
        calendar_api_ok=report["calendar_api_ok"],
        operating_mode=operating_mode,
        errors=report.get("errors", {}),
        cache_fallback_available=cache_fallback,
        timestamp=int(datetime.now(timezone.utc).timestamp()),
        details=report,
    )

    # Log to health table for observability
    try:
        _cache.log_health(status=status, operating_mode=operating_mode, details=report)
    except Exception as e:
        logger.warning(f"log_health failed: {e}")

    return out.model_dump_json()


# ─────────────────────────── MAIN ───────────────────────────

if __name__ == "__main__":
    logger.info(f"tv-unified MCP starting | cache={DB_PATH} | cdp_port={CDP_PORT}")
    mcp.run()
