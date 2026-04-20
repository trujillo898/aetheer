"""Aetheer News Feed MCP Server.

Provides macro-economic and geopolitical news via RSS feeds
from Reuters, Bloomberg, and other financial news sources.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from feed_parser import fetch_geopolitics_news, fetch_macro_news, get_feed_health_report
from shared.cache import TTLCache

logging.basicConfig(level=logging.INFO, format="[news-feed] %(levelname)s %(message)s")
logger = logging.getLogger("aetheer.news-feed")

cache = TTLCache()

mcp = FastMCP("news-feed")


@mcp.tool()
async def get_macro_news(hours: int = 24, filter: str = "all") -> str:
    """Get macro-economic and financial news from RSS feeds.

    Sources: Reuters Business, Bloomberg Markets, Financial Times.

    Args:
        hours: Hours to look back (default: 24)
        filter: "all" for everything, "high_impact_only" for major events
    """
    cache_key = f"macro_news:{hours}:{filter}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    articles = fetch_macro_news(hours=hours, filter_mode=filter)

    result = json.dumps({
        "articles": articles[:30],
        "total_found": len(articles),
        "filter": filter,
        "hours_back": hours,
        "from_cache": False,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    cache.set(cache_key, result, ttl_seconds=600)
    return result


@mcp.tool()
async def get_geopolitics() -> str:
    """Get high-impact geopolitical news with strict filtering.

    Only returns events matching: sanctions, war, conflict, energy crisis,
    supply chain disruptions, G7/G20, NATO events.

    Sources: Reuters World, BBC News, Al Jazeera.
    Each event is classified as risk_on, risk_off, or mixed.
    """
    cached = cache.get("geopolitics")
    if cached is not None:
        return cached

    articles = fetch_geopolitics_news()

    weight = "none"
    if len(articles) > 5:
        weight = "critical"
    elif len(articles) > 0:
        weight = "elevated"

    overall_sentiment = "mixed"
    risk_off_count = sum(1 for a in articles if a["risk_sentiment"] == "risk_off")
    risk_on_count = sum(1 for a in articles if a["risk_sentiment"] == "risk_on")
    if risk_off_count > risk_on_count:
        overall_sentiment = "risk_off"
    elif risk_on_count > risk_off_count:
        overall_sentiment = "risk_on"

    result = json.dumps({
        "events": articles[:15],
        "total_found": len(articles),
        "weight": weight,
        "overall_risk_sentiment": overall_sentiment,
        "from_cache": False,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    cache.set("geopolitics", result, ttl_seconds=600)
    return result


@mcp.tool()
async def get_feed_health() -> str:
    """Get health status of all RSS feeds.

    Shows which feeds are active, degraded, or dead.
    Useful for system diagnostics.
    """
    report = get_feed_health_report()
    active = sum(1 for r in report if r.get("status") == "active")
    dead = sum(1 for r in report if r.get("status") == "dead")

    return json.dumps({
        "feeds": report,
        "summary": {"active": active, "dead": dead, "total": len(report)},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


if __name__ == "__main__":
    mcp.run()
