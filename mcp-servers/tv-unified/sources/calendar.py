"""Economic calendar source: TV calendar API behind cache-first policy."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..cache_store.snapshot import DEFAULT_TTLS, STALE_MAX_SECONDS, SnapshotCache
from ..tv_bridge_ext import TVBridgeExtended

logger = logging.getLogger("aetheer.tv_unified.sources.calendar")


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


async def get_economic_calendar(
    cache: SnapshotCache,
    bridge: TVBridgeExtended,
    countries: str = "US,EU,GB",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    window_hours: int = 24,
) -> dict:
    """Fetch economic calendar events.

    If from_date/to_date are not provided, uses [now, now+window_hours].
    Cache key is (countries|from|to).
    """
    now = datetime.now(timezone.utc)
    if not from_date:
        from_date = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if not to_date:
        to_date = (now + timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    cache_key = f"{countries}:{from_date}:{to_date}"
    ttl = DEFAULT_TTLS["calendar"]

    hit = cache.get("calendar", cache_key, max_age_seconds=ttl)
    if hit is not None:
        data, age = hit
        data = dict(data)
        data["_cache_meta"] = {
            "from_cache": True,
            "cache_age_seconds": age,
            "stale": False,
            "source": "tradingview_calendar_api",
            "quality_score": 0.95,
        }
        return data

    try:
        events = await bridge.fetch_calendar(
            countries=countries, from_date=from_date, to_date=to_date
        )
        payload = {
            "result": events,
            "query": {"countries": countries, "from": from_date, "to": to_date},
            "fetched_at": _now_epoch(),
        }
        cache.set("calendar", cache_key, payload, ttl_seconds=ttl)
        payload["_cache_meta"] = {
            "from_cache": False,
            "cache_age_seconds": 0,
            "stale": False,
            "source": "tradingview_calendar_api",
            "quality_score": 0.95,
        }
        return payload
    except Exception as e:
        logger.warning(f"fetch_calendar failed: {e}")
        stale = cache.get("calendar", cache_key, max_age_seconds=STALE_MAX_SECONDS)
        if stale is not None:
            data, age = stale
            data = dict(data)
            data["_cache_meta"] = {
                "from_cache": True,
                "cache_age_seconds": age,
                "stale": True,
                "source": "tradingview_calendar_api_stale",
                "quality_score": 0.6,
            }
            return data
        raise RuntimeError(f"Calendar API down and no stale cache: {e}")
