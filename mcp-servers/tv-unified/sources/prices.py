"""Price source: wraps shared.tv_market_reader behind cache-first policy.

Cache flow:
  1. Fresh cache (age <= TTL) → return directly.
  2. Miss → call TV CDP, store result.
  3. TV failure → serve stale cache up to STALE_MAX_SECONDS (30 min).
  4. No stale available → raise RuntimeError.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make `shared.*` imports resolve
_PKG_ROOT = Path(__file__).resolve().parent.parent.parent  # .../mcp-servers
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from shared.tv_market_reader import (  # noqa: E402
    TAB_CONFIG,
    TF_MAP,
    TF_PROFILES,
    deep_read,
    quick_read,
    read_market_data,
)
from shared.tv_availability import (  # noqa: E402
    get_tv_quote_for_symbol,
    is_tv_available,
)

from ..cache_store.snapshot import DEFAULT_TTLS, STALE_MAX_SECONDS, SnapshotCache

logger = logging.getLogger("aetheer.tv_unified.sources.prices")

# Map short instrument names → TV symbols
SYMBOL_MAP = {
    "DXY": "TVC:DXY",
    "EURUSD": "OANDA:EURUSD",
    "GBPUSD": "OANDA:GBPUSD",
    "XAUUSD": "OANDA:XAUUSD",
    "VIX": "TVC:VIX",
    "SPX": "SP:SPX",
    "US10Y": "TVC:US10Y",
    "US02Y": "TVC:US02Y",
    "USOIL": "TVC:USOIL",
    "DE10Y": "TVC:DE10Y",
    "GB10Y": "TVC:GB10Y",
}

CORRELATION_BASKET = ["DXY", "EURUSD", "GBPUSD", "XAUUSD", "VIX", "SPX", "US10Y", "US02Y"]


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _normalize_instrument(instrument: str) -> str:
    instrument = (instrument or "").upper().strip()
    # Allow callers to pass either "DXY" or "TVC:DXY"
    if ":" in instrument:
        instrument = instrument.split(":", 1)[1]
    return instrument


async def get_price(cache: SnapshotCache, instrument: str) -> dict:
    """Get current price for a single instrument.

    Returns dict ready for PriceData validation + CacheMeta wrapping.
    Raises RuntimeError if TV is down AND no stale cache is available.
    """
    instrument = _normalize_instrument(instrument)
    cache_key = instrument
    ttl = DEFAULT_TTLS["price"]

    # 1. Fresh cache
    hit = cache.get("price", cache_key, max_age_seconds=ttl)
    if hit is not None:
        data, age = hit
        data = dict(data)
        data["_cache_meta"] = {
            "from_cache": True,
            "cache_age_seconds": age,
            "stale": False,
            "source": "tradingview_cdp",
            "quality_score": 0.98,
        }
        return data

    # 2. Live read (only if TV is up)
    if is_tv_available():
        try:
            tv_symbol = SYMBOL_MAP.get(instrument, instrument)
            symbol_to_tab = {
                SYMBOL_MAP[name]: cfg["index"]
                for name, cfg in TAB_CONFIG.items()
                if name in SYMBOL_MAP
            }
            quote = await get_tv_quote_for_symbol(tv_symbol, symbol_to_tab)
            price = None
            if quote and quote.get("success"):
                price = quote.get("close") or quote.get("last")
            if price is not None:
                payload = {
                    "symbol": instrument,
                    "price": float(price),
                    "bid": quote.get("bid"),
                    "ask": quote.get("ask"),
                    "timestamp": _now_epoch(),
                }
                cache.set("price", cache_key, payload, ttl_seconds=ttl)
                payload["_cache_meta"] = {
                    "from_cache": False,
                    "cache_age_seconds": 0,
                    "stale": False,
                    "source": "tradingview_cdp",
                    "quality_score": 0.98,
                }
                return payload
            logger.warning(f"get_tv_quote_for_symbol did not return price for {instrument}")
        except Exception as e:
            logger.warning(f"get_tv_quote_for_symbol failed for {instrument}: {e}")

    # 3. Stale fallback
    stale = cache.get("price", cache_key, max_age_seconds=STALE_MAX_SECONDS)
    if stale is not None:
        data, age = stale
        data = dict(data)
        data["_cache_meta"] = {
            "from_cache": True,
            "cache_age_seconds": age,
            "stale": True,
            "source": "tradingview_cdp_stale",
            "quality_score": max(0.4, 0.98 - (age / STALE_MAX_SECONDS) * 0.5),
        }
        return data

    # 4. No data
    raise RuntimeError(
        f"No price available for {instrument}: TV offline and no stale cache within {STALE_MAX_SECONDS}s."
    )


async def get_correlations(cache: SnapshotCache) -> dict:
    """Fetch the correlation basket as a bundle. Uses its own cache namespace."""
    ttl = DEFAULT_TTLS["correlations"]
    hit = cache.get("correlations", "basket", max_age_seconds=ttl)
    if hit is not None:
        data, age = hit
        data = dict(data)
        data["_cache_meta"] = {
            "from_cache": True,
            "cache_age_seconds": age,
            "stale": False,
            "source": "tradingview_cdp",
            "quality_score": 0.98,
        }
        return data

    prices: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for inst in CORRELATION_BASKET:
        try:
            prices[inst] = await get_price(cache, inst)
        except Exception as e:
            errors[inst] = str(e)

    if not prices:
        raise RuntimeError(f"Correlation basket empty: {errors}")

    payload = {"prices": prices, "errors": errors, "timestamp": _now_epoch()}
    cache.set("correlations", "basket", payload, ttl_seconds=ttl)
    payload["_cache_meta"] = {
        "from_cache": False,
        "cache_age_seconds": 0,
        "stale": False,
        "source": "tradingview_cdp",
        "quality_score": 0.98,
    }
    return payload


async def get_ohlcv(
    cache: SnapshotCache,
    instrument: str,
    timeframe: str,
    intention: str = "full_analysis",
) -> dict:
    """Read OHLCV + Aetheer indicator data via deep_read.

    NOTE: deep_read interferes with the trader's chart (~24-30s). Prefer calling
    this only for full_analysis / validate_setup intents. For heartbeat / data_point
    use get_price instead.
    """
    instrument = _normalize_instrument(instrument)
    if instrument not in TAB_CONFIG:
        raise ValueError(f"Instrument {instrument} not in TAB_CONFIG (need DXY/EURUSD/GBPUSD tab setup).")
    if timeframe not in TF_MAP:
        raise ValueError(f"Unknown timeframe {timeframe}. Use one of {list(TF_MAP)}.")

    cache_key = f"{instrument}:{timeframe}"
    ttl = DEFAULT_TTLS["ohlcv"]

    hit = cache.get("ohlcv", cache_key, max_age_seconds=ttl)
    if hit is not None:
        data, age = hit
        data = dict(data)
        data["_cache_meta"] = {
            "from_cache": True,
            "cache_age_seconds": age,
            "stale": False,
            "source": "tradingview_cdp",
            "quality_score": 0.98,
        }
        return data

    if not is_tv_available():
        stale = cache.get("ohlcv", cache_key, max_age_seconds=STALE_MAX_SECONDS)
        if stale is not None:
            data, age = stale
            data = dict(data)
            data["_cache_meta"] = {
                "from_cache": True,
                "cache_age_seconds": age,
                "stale": True,
                "source": "tradingview_cdp_stale",
                "quality_score": 0.5,
            }
            return data
        raise RuntimeError(f"TV offline and no OHLCV cache for {instrument} {timeframe}.")

    # Live deep read for the requested instrument only
    tf_value = TF_MAP[timeframe]
    raw = await deep_read([instrument], intention)
    if "error" in raw:
        raise RuntimeError(f"deep_read error: {raw['error']}")

    tf_block = raw.get(instrument, {}).get(tf_value, {})
    if not tf_block or "error" in tf_block:
        raise RuntimeError(f"deep_read did not return {instrument} {timeframe}: {tf_block}")

    payload = {
        "symbol": instrument,
        "timeframe": timeframe,
        "ohlcv": tf_block.get("ohlcv"),
        "aetheer_indicator": tf_block.get("aetheer_indicator"),
        "aetheer_valid": tf_block.get("aetheer_valid"),
        "aetheer_errors": tf_block.get("aetheer_errors"),
        "aetheer_warnings": tf_block.get("aetheer_warnings"),
        "timestamp": _now_epoch(),
    }
    cache.set("ohlcv", cache_key, payload, ttl_seconds=ttl)
    payload["_cache_meta"] = {
        "from_cache": False,
        "cache_age_seconds": 0,
        "stale": False,
        "source": "tradingview_cdp",
        "quality_score": 0.98,
    }
    return payload


async def get_chart_indicators(
    cache: SnapshotCache,
    instrument: str,
    timeframe: str = "H1",
) -> dict:
    """Return just the Aetheer indicator block (cached separately)."""
    ttl = DEFAULT_TTLS["indicators"]
    cache_key = f"{_normalize_instrument(instrument)}:{timeframe}"
    hit = cache.get("indicators", cache_key, max_age_seconds=ttl)
    if hit is not None:
        data, age = hit
        data = dict(data)
        data["_cache_meta"] = {
            "from_cache": True,
            "cache_age_seconds": age,
            "stale": False,
            "source": "tradingview_cdp",
            "quality_score": 0.98,
        }
        return data

    # Reuse get_ohlcv to avoid double deep_read
    ohlcv = await get_ohlcv(cache, instrument, timeframe, intention="validate_setup")
    payload = {
        "symbol": ohlcv["symbol"],
        "timeframe": ohlcv["timeframe"],
        "aetheer_indicator": ohlcv.get("aetheer_indicator"),
        "aetheer_valid": ohlcv.get("aetheer_valid"),
        "timestamp": _now_epoch(),
    }
    cache.set("indicators", cache_key, payload, ttl_seconds=ttl)
    payload["_cache_meta"] = {
        "from_cache": False,
        "cache_age_seconds": 0,
        "stale": False,
        "source": "tradingview_cdp",
        "quality_score": 0.98,
    }
    return payload
