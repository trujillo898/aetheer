"""News source: calls TVBridgeExtended.fetch_news behind cache-first policy."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..cache_store.snapshot import DEFAULT_TTLS, STALE_MAX_SECONDS, SnapshotCache
from ..tv_bridge_ext import TVBridgeExtended

logger = logging.getLogger("aetheer.tv_unified.sources.news")


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


async def get_news(
    cache: SnapshotCache,
    bridge: TVBridgeExtended,
    symbol: Optional[str] = None,
    category: str = "forex",
    lang: str = "es",
    limit: int = 50,
) -> dict:
    """Fetch news from TV. Cache key is (symbol|category|lang|limit)."""
    cache_key = f"{symbol or category}:{lang}:{limit}"
    ttl = DEFAULT_TTLS["news"]

    hit = cache.get("news", cache_key, max_age_seconds=ttl)
    if hit is not None:
        data, age = hit
        data = dict(data)
        data["_cache_meta"] = {
            "from_cache": True,
            "cache_age_seconds": age,
            "stale": False,
            "source": "tradingview_news_api",
            "quality_score": 0.95,
        }
        return data

    try:
        items = await bridge.fetch_news(
            symbol=symbol, category=category, lang=lang, limit=limit
        )
        payload = {
            "items": items,
            "query": {"symbol": symbol, "category": category, "lang": lang, "limit": limit},
            "fetched_at": _now_epoch(),
        }
        cache.set("news", cache_key, payload, ttl_seconds=ttl)
        payload["_cache_meta"] = {
            "from_cache": False,
            "cache_age_seconds": 0,
            "stale": False,
            "source": "tradingview_news_api",
            "quality_score": 0.95,
        }
        return payload
    except Exception as e:
        logger.warning(f"fetch_news failed: {e}")
        stale = cache.get("news", cache_key, max_age_seconds=STALE_MAX_SECONDS)
        if stale is not None:
            data, age = stale
            data = dict(data)
            data["_cache_meta"] = {
                "from_cache": True,
                "cache_age_seconds": age,
                "stale": True,
                "source": "tradingview_news_api_stale",
                "quality_score": 0.6,
            }
            return data
        raise RuntimeError(f"News API down and no stale cache: {e}")
