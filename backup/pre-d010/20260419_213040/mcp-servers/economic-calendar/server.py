"""Aetheer Economic Calendar MCP Server.

Provides upcoming economic events, last event impact analysis,
and historical event search for USD, EUR, and GBP currencies.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from scraper import get_calendar_events
from shared.cache import TTLCache

logging.basicConfig(level=logging.INFO, format="[economic-calendar] %(levelname)s %(message)s")
logger = logging.getLogger("aetheer.economic-calendar")

cache = TTLCache()

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent.parent.parent / "db" / "aetheer.db"))

mcp = FastMCP("economic-calendar")


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _preload_is_fresh(db: sqlite3.Connection, max_age_hours: int = 6) -> bool:
    """Verifica si la última pre-carga tiene menos de max_age_hours."""
    try:
        row = db.execute(
            """SELECT preloaded_at FROM events
               WHERE preloaded_at IS NOT NULL
               ORDER BY preloaded_at DESC LIMIT 1"""
        ).fetchone()
        if row and row["preloaded_at"]:
            preloaded = datetime.fromisoformat(row["preloaded_at"].replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - preloaded).total_seconds() / 3600
            return age_hours < max_age_hours
    except Exception:
        pass
    return False


@mcp.tool()
async def get_upcoming_events(hours_ahead: int = 72) -> str:
    """Get upcoming economic events for the next N hours.

    Checks preloaded data in SQLite first. Falls back to live scraping
    if preloaded data is older than 6 hours.

    Args:
        hours_ahead: Hours to look ahead (default: 72)
    """
    cache_key = f"upcoming:{hours_ahead}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Try preloaded data from DB first
    source = "preloaded"
    events = []
    try:
        db = _get_db()
        if _preload_is_fresh(db):
            rows = db.execute(
                """SELECT event_name, currency, importance, expected, actual, previous,
                          event_datetime_utc, result_status
                   FROM events
                   WHERE event_datetime_utc >= datetime('now')
                     AND event_datetime_utc <= datetime('now', '+' || ? || ' hours')
                   ORDER BY event_datetime_utc ASC""",
                (hours_ahead,),
            ).fetchall()
            for r in rows:
                events.append({
                    "event": r["event_name"],
                    "currency": r["currency"],
                    "importance": r["importance"],
                    "consensus": r["expected"],
                    "previous": r["previous"],
                    "actual": r["actual"],
                    "datetime_utc": r["event_datetime_utc"],
                })
            logger.info(f"[PRELOAD] {len(events)} events from DB")
        db.close()
    except Exception as e:
        logger.warning(f"Failed to read preloaded events: {e}")

    # Fallback to live scraping if DB is empty or stale
    if not events:
        source = "live_scrape"
        events = await get_calendar_events(hours_ahead)

        # Store new events in DB
        try:
            db = _get_db()
            for evt in events:
                if evt.get("actual") is not None:
                    db.execute(
                        """INSERT OR IGNORE INTO events
                           (event_name, currency, importance, expected, actual, previous, event_datetime_utc)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            evt["event"],
                            evt["currency"],
                            evt["importance"],
                            evt.get("consensus"),
                            evt.get("actual"),
                            evt.get("previous"),
                            evt["datetime_utc"],
                        ),
                    )
            db.commit()
            db.close()
        except Exception as e:
            logger.warning(f"Failed to store events: {e}")

    # Filtrar eventos futuros: sin actual publicado Y datetime en el futuro
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    upcoming = [
        e for e in events
        if e.get("actual") is None and (e.get("datetime_utc") or "") >= now_iso
    ]
    result = json.dumps({
        "upcoming_events": upcoming[:30],
        "total_found": len(events),
        "upcoming_count": len(upcoming),
        "source": source,
        "from_cache": False,
        "timestamp": now_iso,
    })
    cache.set(cache_key, result, ttl_seconds=300)  # 5 min — eventos publicados se reflejan rápido
    return result


@mcp.tool()
async def get_last_event_impact(event_name: str = "", currency: str = "") -> str:
    """Get the last published economic event with its market impact.

    Args:
        event_name: Optional filter by event name (partial match)
        currency: Optional filter by currency (USD, EUR, GBP)
    """
    cache_key = f"impact:{event_name}:{currency}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        db = _get_db()
        query = "SELECT * FROM events WHERE actual IS NOT NULL"
        params = []

        if event_name:
            query += " AND event_name LIKE ?"
            params.append(f"%{event_name}%")
        if currency:
            query += " AND currency = ?"
            params.append(currency.upper())

        query += " ORDER BY event_datetime_utc DESC LIMIT 1"
        row = db.execute(query, params).fetchone()
        db.close()

        if row:
            data = dict(row)
            surprise = "neutral"
            if data.get("actual") is not None and data.get("expected") is not None:
                diff = data["actual"] - data["expected"]
                if diff > 0:
                    surprise = "hawkish" if data["currency"] == "USD" else "dovish"
                elif diff < 0:
                    surprise = "dovish" if data["currency"] == "USD" else "hawkish"
            data["surprise_direction"] = surprise
            data["from_cache"] = False
            result = json.dumps(data)
            cache.set(cache_key, result, ttl_seconds=300)
            return result

        # Fallback: scrape fresh data
        events = await get_calendar_events(24)
        past = [e for e in events if e.get("actual") is not None]
        if currency:
            past = [e for e in past if e["currency"] == currency.upper()]
        if event_name:
            past = [e for e in past if event_name.lower() in e["event"].lower()]
        if past:
            return json.dumps(past[0])

        return json.dumps({"message": "No matching events found"})

    except Exception as e:
        logger.error(f"Failed to get last event impact: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def search_event_history(event_name: str, months: int = 3) -> str:
    """Search historical impact of a specific event type.

    Args:
        event_name: Event name to search (partial match)
        months: Months to look back (default: 3)
    """
    cache_key = f"history:{event_name}:{months}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        db = _get_db()
        rows = db.execute(
            """SELECT event_name, currency, importance, expected, actual, previous,
                      surprise_direction, price_reaction_dxy_pct, reaction_duration_min,
                      priced_in, event_datetime_utc
               FROM events
               WHERE event_name LIKE ?
                 AND event_datetime_utc >= datetime('now', '-' || ? || ' months')
               ORDER BY event_datetime_utc DESC
               LIMIT 20""",
            (f"%{event_name}%", months),
        ).fetchall()
        db.close()

        result = json.dumps({
            "event_search": event_name,
            "months_back": months,
            "results": [dict(r) for r in rows],
            "count": len(rows),
            "from_cache": False,
        })
        cache.set(cache_key, result, ttl_seconds=3600)
        return result
    except Exception as e:
        logger.error(f"Failed to search event history: {e}")
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
