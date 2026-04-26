"""Aetheer Macro Data MCP Server.

Provides macro-economic data: FedWatch probabilities, bond yields,
macro indicators (CPI, NFP, GDP), and correlation assets (Gold, VIX, S&P500, WTI).
"""

import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from fred_client import get_correlations, get_macro_indicator, get_yields
from shared.cache import TTLCache

logging.basicConfig(level=logging.INFO, format="[macro-data] %(levelname)s %(message)s")
logger = logging.getLogger("aetheer.macro-data")

cache = TTLCache()

mcp = FastMCP("macro-data")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _get_db_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "db" / "aetheer.db"


def _save_fedwatch_to_history(data: dict):
    """Persiste lectura de FedWatch en fedwatch_history."""
    try:
        db = _get_db_path()
        if not db.exists():
            return
        conn = sqlite3.connect(str(db))
        probs = data.get("probabilities", {})
        conn.execute(
            """INSERT INTO fedwatch_history
               (meeting_date, prob_hold, prob_cut_25, prob_hike_25,
                source, raw_data, timestamp_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("next_meeting"),
                probs.get("hold"),
                probs.get("cut_25bp"),
                probs.get("hike_25bp"),
                data.get("source", "unknown"),
                json.dumps(data),
                data.get("timestamp_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to save FedWatch to history: {e}")


def _get_last_known_fedwatch(max_age_hours: int = 168) -> dict | None:
    """Recupera última lectura conocida de FedWatch (máx 7 días)."""
    try:
        db = _get_db_path()
        if not db.exists():
            return None
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM fedwatch_history
               ORDER BY timestamp_utc DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        if row:
            created = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            if age_hours <= max_age_hours:
                return {
                    "probabilities": {
                        "hold": row["prob_hold"],
                        "cut_25bp": row["prob_cut_25"],
                        "hike_25bp": row["prob_hike_25"],
                    },
                    "meeting_date": row["meeting_date"],
                    "source": f"{row['source']}_cached",
                    "timestamp_utc": row["timestamp_utc"],
                    "age_hours": round(age_hours, 1),
                    "is_fallback": True,
                    "warning": f"Dato de hace {round(age_hours, 1)}h. Puede no reflejar cambios recientes.",
                }
    except Exception as e:
        logger.warning(f"Failed to get FedWatch from history: {e}")
    return None


@mcp.tool(name="macro_get_fed_watch")
async def get_fed_watch() -> str:
    """Get CME FedWatch Tool probabilities for the next Fed meeting.

    Cascada:
      1. CME API JSON endpoint (no requiere JS)
      2. CME scraping clásico (puede fallar con 403)
      3. fedwatch_history (último dato conocido, marcado con antigüedad)
      4. Error explícito si no hay dato

    Si se obtiene dato fresco, se persiste en fedwatch_history.
    """
    cached = cache.get("fed_watch")
    if cached is not None:
        return cached

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = None

    # --- Intento 1: CME API JSON ---
    try:
        api_url = "https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/305/G"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                api_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Referer": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
                },
                timeout=15.0,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "quotes" in data:
                    result = {
                        "source": "cme_api",
                        "raw_quotes": data["quotes"][:5] if data["quotes"] else [],
                        "timestamp_utc": now_utc,
                        "note": "Raw CME futures data. Probabilities require calculation from implied rates.",
                    }
            else:
                logger.warning(f"CME API returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"CME API failed: {e}")

    # --- Intento 2: scraping clásico (puede fallar con 403) ---
    if result is None:
        try:
            url = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                    timeout=15.0,
                    follow_redirects=True,
                )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                probs = {}
                prob_els = soup.select("[class*='probability'], [class*='prob'], td[class*='value']")
                for el in prob_els:
                    text = el.get_text(strip=True)
                    match = re.search(r"([\d.]+)%", text)
                    if match:
                        pct = float(match.group(1))
                        parent_text = el.parent.get_text(strip=True).lower() if el.parent else ""
                        if "cut" in parent_text or "decrease" in parent_text:
                            probs["cut_25bp"] = pct
                        elif "hike" in parent_text or "increase" in parent_text:
                            probs["hike_25bp"] = pct
                        elif "hold" in parent_text or "no change" in parent_text or "unchanged" in parent_text:
                            probs["hold"] = pct

                if probs:
                    result = {
                        "probabilities": probs,
                        "source": "cme_scrape",
                        "timestamp_utc": now_utc,
                    }
        except Exception as e:
            logger.warning(f"CME scrape failed: {e}")

    # --- Persistir si obtuvimos dato fresco ---
    if result and result.get("source") in ("cme_api", "cme_scrape"):
        _save_fedwatch_to_history(result)

    # --- Intento 3: fallback a fedwatch_history ---
    if result is None:
        result = _get_last_known_fedwatch()

    # --- Sin dato ---
    if result is None:
        result = {
            "error": "no_fedwatch_data",
            "message": "CME FedWatch no disponible (403) y no hay historial. "
                       "Puedes ingresar datos manualmente con update_fedwatch_manual.",
            "timestamp_utc": now_utc,
        }

    result["from_cache"] = False
    resp_str = json.dumps(result)
    if "error" not in result:
        cache.set("fed_watch", resp_str, ttl_seconds=900)
    return resp_str


@mcp.tool(name="macro_update_fedwatch_manual")
async def update_fedwatch_manual(
    hold: float | None = None,
    cut_25bp: float | None = None,
    hike_25bp: float | None = None,
    meeting_date: str | None = None,
    current_rate: str | None = None,
) -> str:
    """Manually update FedWatch probabilities when CME is blocked.

    The trader can input values seen on CME website or other sources.
    These get stored in fedwatch_history and used as fallback.

    Args:
        hold: Probability of hold (%)
        cut_25bp: Probability of 25bp cut (%)
        hike_25bp: Probability of 25bp hike (%)
        meeting_date: Next FOMC meeting date
        current_rate: Current fed funds rate (e.g. "5.25-5.50")
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if hold is None and cut_25bp is None and hike_25bp is None:
        return json.dumps({
            "error": "No probabilities provided. Include at least one: hold, cut_25bp, hike_25bp."
        })

    data = {
        "probabilities": {
            "hold": hold,
            "cut_25bp": cut_25bp,
            "hike_25bp": hike_25bp,
        },
        "next_meeting": meeting_date,
        "current_rate": current_rate,
        "source": "manual",
        "timestamp_utc": now_utc,
    }

    _save_fedwatch_to_history(data)

    # Invalidar cache para que la próxima llamada use el dato nuevo
    cache.set("fed_watch", None, ttl_seconds=0)

    return json.dumps({
        "status": "saved",
        "data": data,
        "message": "FedWatch actualizado manualmente. Se usará como referencia hasta obtener dato de CME.",
    })


@mcp.tool(name="macro_get_yields")
async def get_yields_data() -> str:
    """Get 10-year bond yields for US Treasury, German Bund, and UK Gilt.

    Returns yield levels and calculated spreads (US vs EU, US vs UK).
    """
    cached = cache.get("yields")
    if cached is not None:
        return cached
    data = await get_yields()
    data["from_cache"] = False
    result = json.dumps(data)
    cache.set("yields", result, ttl_seconds=900)
    return result


@mcp.tool(name="macro_get_indicator")
async def get_macro_indicators(country: str, indicator: str) -> str:
    """Get a macro-economic indicator value.

    Args:
        country: Country code - "US", "EU", or "UK"
        indicator: Indicator name - "CPI", "NFP", "GDP", "unemployment"
    """
    cache_key = f"macro:{country}:{indicator}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    data = await get_macro_indicator(country, indicator)
    data["from_cache"] = False
    result = json.dumps(data)
    if data.get("value") is not None:
        cache.set(cache_key, result, ttl_seconds=3600)
    return result


@mcp.tool(name="macro_get_correlations")
async def get_correlations_data() -> str:
    """Get correlation assets: Gold (XAU/USD), VIX, S&P 500, WTI Crude.

    Returns current levels, 24h trends, and regime classification.
    """
    cached = cache.get("correlations")
    if cached is not None:
        return cached
    data = await get_correlations()
    data["from_cache"] = False
    result = json.dumps(data)
    cache.set("correlations", result, ttl_seconds=300)
    return result


if __name__ == "__main__":
    mcp.run()
