"""RSS feed parser for Aetheer news-feed MCP server."""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import feedparser
from dateutil import parser as dateparser

logger = logging.getLogger("aetheer.news-feed.parser")

# Feeds actualizados abril 2026
# Múltiples fuentes para redundancia. Si un feed muere, los demás cubren.
MACRO_FEEDS = {
    "cnbc_economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "wsj_economy": "https://feeds.a.wsj.com/xml/rss/3_7014.xml",
    "ft_markets": "https://www.ft.com/rss/markets",
    "marketwatch_topstories": "https://feeds.marketwatch.com/marketwatch/topstories",
    "investing_news": "https://www.investing.com/rss/news.rss",
    "forexlive": "https://www.forexlive.com/feed",
    "fed_press": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ecb_press": "https://www.ecb.europa.eu/rss/press.html",
}

GEOPOLITICS_FEEDS = {
    "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "aljazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "reuters_world": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
}

def _get_db_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "db" / "aetheer.db"


def _update_feed_status(feed_name: str, feed_url: str, success: bool,
                         articles_count: int = 0, error: str | None = None):
    """Actualiza estado del feed en feed_status."""
    try:
        db = _get_db_path()
        if not db.exists():
            return
        conn = sqlite3.connect(str(db))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        existing = conn.execute(
            "SELECT id, consecutive_failures FROM feed_status WHERE feed_name = ?",
            (feed_name,)
        ).fetchone()

        if existing:
            if success:
                conn.execute(
                    """UPDATE feed_status SET
                       status = 'active', last_success_utc = ?, consecutive_failures = 0,
                       articles_last_24h = ?, updated_at = ?
                       WHERE feed_name = ?""",
                    (now, articles_count, now, feed_name),
                )
            else:
                failures = existing[1] + 1
                status = "dead" if failures >= 5 else "degraded"
                conn.execute(
                    """UPDATE feed_status SET
                       status = ?, last_failure_utc = ?, last_error = ?,
                       consecutive_failures = ?, updated_at = ?
                       WHERE feed_name = ?""",
                    (status, now, error, failures, now, feed_name),
                )
        else:
            status = "active" if success else "unknown"
            conn.execute(
                """INSERT INTO feed_status
                   (feed_name, feed_url, status, last_success_utc, last_failure_utc,
                    last_error, consecutive_failures, articles_last_24h)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (feed_name, feed_url, status,
                 now if success else None,
                 None if success else now,
                 error, 0 if success else 1, articles_count),
            )

        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to update feed status for {feed_name}: {e}")


def get_feed_health_report() -> list[dict]:
    """Retorna estado de salud de todos los feeds monitoreados."""
    try:
        db = _get_db_path()
        if not db.exists():
            return []
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM feed_status ORDER BY status, feed_name"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


GEOPOLITICS_KEYWORDS = {
    "sanctions", "war", "conflict", "energy crisis", "supply chain",
    "g7", "g20", "nato", "missile", "nuclear", "embargo", "invasion",
    "blockade", "tariff", "trade war", "opec", "coup", "martial law",
}

HIGH_IMPACT_KEYWORDS = {
    "fed", "federal reserve", "ecb", "bank of england", "interest rate",
    "inflation", "cpi", "gdp", "recession", "employment", "jobs",
    "yield", "treasury", "bond", "dollar", "forex", "currency",
    "central bank", "monetary policy", "rate cut", "rate hike",
    "quantitative", "tightening", "easing", "stimulus",
}


def _parse_date(entry) -> str:
    """Extract and normalize date from feed entry."""
    for field in ("published", "updated", "created"):
        val = entry.get(field)
        if val:
            try:
                dt = dateparser.parse(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_summary(entry, max_chars: int = 200) -> str:
    """Extract summary text, truncated."""
    summary = entry.get("summary", entry.get("description", ""))
    # Strip HTML tags
    import re
    summary = re.sub(r"<[^>]+>", "", summary).strip()
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "..."
    return summary


def _hours_old(date_str: str, max_hours: int) -> bool:
    """Check if a date string is within max_hours."""
    try:
        dt = dateparser.parse(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return age <= max_hours
    except Exception:
        return True  # Include if we can't parse


def fetch_macro_news(hours: int = 24, filter_mode: str = "all") -> list[dict]:
    """Fetch macro/financial news from RSS feeds with health monitoring.

    Args:
        hours: Only include news from last N hours.
        filter_mode: "all" or "high_impact_only"
    """
    articles = []

    for source_name, url in MACRO_FEEDS.items():
        try:
            feed = feedparser.parse(url)

            # Detectar feed muerto (sin entries o bozo flag sin entries)
            if feed.bozo and not feed.entries:
                _update_feed_status(source_name, url, success=False,
                                     error=str(feed.bozo_exception))
                continue

            source_articles = 0
            for entry in feed.entries:
                date_str = _parse_date(entry)
                if not _hours_old(date_str, hours):
                    continue

                title = entry.get("title", "").strip()
                summary = _get_summary(entry)
                combined_text = f"{title} {summary}".lower()

                if filter_mode == "high_impact_only":
                    if not any(kw in combined_text for kw in HIGH_IMPACT_KEYWORDS):
                        continue

                articles.append({
                    "title": title,
                    "source": source_name,
                    "datetime_utc": date_str,
                    "summary": summary,
                    "link": entry.get("link", ""),
                })
                source_articles += 1

            _update_feed_status(source_name, url, success=True,
                                 articles_count=source_articles)

        except Exception as e:
            logger.warning(f"Failed to parse feed {source_name}: {e}")
            _update_feed_status(source_name, url, success=False, error=str(e))

    articles.sort(key=lambda x: x["datetime_utc"], reverse=True)
    return articles


def fetch_geopolitics_news() -> list[dict]:
    """Fetch high-impact geopolitics news with strict filtering and health monitoring.

    Only returns events matching geopolitical keywords related to
    G7/G20 conflicts, sanctions, energy crises, supply chain disruptions.
    """
    articles = []

    for source_name, url in GEOPOLITICS_FEEDS.items():
        try:
            feed = feedparser.parse(url)

            if feed.bozo and not feed.entries:
                _update_feed_status(source_name, url, success=False,
                                     error=str(feed.bozo_exception))
                continue

            source_articles = 0
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                summary = _get_summary(entry)
                combined_text = f"{title} {summary}".lower()

                # Strict keyword filter
                matching_keywords = [kw for kw in GEOPOLITICS_KEYWORDS if kw in combined_text]
                if not matching_keywords:
                    continue

                # Classify risk sentiment
                risk_off_words = {"war", "conflict", "sanctions", "crisis", "missile", "nuclear", "invasion"}
                risk_on_words = {"ceasefire", "peace", "agreement", "deal", "resolution"}

                sentiment = "mixed"
                if any(w in combined_text for w in risk_off_words):
                    sentiment = "risk_off"
                if any(w in combined_text for w in risk_on_words):
                    sentiment = "risk_on" if sentiment == "mixed" else "mixed"

                articles.append({
                    "title": title,
                    "source": source_name,
                    "datetime_utc": _parse_date(entry),
                    "summary": summary,
                    "matched_keywords": matching_keywords,
                    "risk_sentiment": sentiment,
                    "link": entry.get("link", ""),
                })
                source_articles += 1

            _update_feed_status(source_name, url, success=True,
                                 articles_count=source_articles)

        except Exception as e:
            logger.warning(f"Failed to parse geopolitics feed {source_name}: {e}")
            _update_feed_status(source_name, url, success=False, error=str(e))

    articles.sort(key=lambda x: x["datetime_utc"], reverse=True)
    return articles
