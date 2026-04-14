"""FRED API client and macro data scrapers for Aetheer."""

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("aetheer.macro-data.fred")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

FRED_SERIES = {
    "US": {
        "CPI": "CPIAUCSL",
        "unemployment": "UNRATE",
        "GDP": "GDP",
        "NFP": "PAYEMS",
        "fed_funds": "FEDFUNDS",
    },
}

YAHOO_SYMBOLS = {
    "gold": "GC=F",
    "xauusd": "GC=F",
    "vix": "^VIX",
    "sp500": "^GSPC",
    "wti": "CL=F",
    "brent": "BZ=F",
    "us_10y": "^TNX",
}


async def fetch_fred_series(series_id: str, limit: int = 5) -> dict | None:
    """Fetch latest observation from FRED API."""
    if not FRED_API_KEY:
        return None

    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(FRED_BASE, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            observations = data.get("observations", [])
            if observations:
                latest = observations[0]
                return {
                    "series_id": series_id,
                    "value": float(latest["value"]) if latest["value"] != "." else None,
                    "date": latest["date"],
                    "source": "FRED",
                }
    except Exception as e:
        logger.warning(f"FRED API failed for {series_id}: {e}")
    return None


async def fetch_yahoo_quote(symbol: str) -> dict | None:
    """Fetch a quote from Yahoo Finance."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", meta.get("previousClose"))
            prev = meta.get("chartPreviousClose", meta.get("previousClose"))
            if price is not None:
                change_pct = ((price - prev) / prev * 100) if prev else 0
                return {
                    "symbol": symbol,
                    "price": round(float(price), 4),
                    "previous_close": round(float(prev), 4) if prev else None,
                    "change_pct_24h": round(change_pct, 2),
                    "source": "yahoo_finance",
                    "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
    except Exception as e:
        logger.warning(f"Yahoo Finance failed for {symbol}: {e}")
    return None


async def scrape_tradingeconomics_yield(bond: str) -> dict | None:
    """Scrape bond yield from TradingEconomics."""
    paths = {
        "us_10y": "/united-states/government-bond-yield",
        "bund_10y": "/germany/government-bond-yield",
        "gilt_10y": "/united-kingdom/government-bond-yield",
    }
    path = paths.get(bond)
    if not path:
        return None

    url = f"https://tradingeconomics.com{path}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=10.0)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        price_el = soup.select_one("#tinline-0, .te-price-value")
        if price_el:
            text = price_el.get_text(strip=True).replace(",", "")
            match = re.search(r"[\d.]+", text)
            if match:
                return {
                    "bond": bond,
                    "yield_pct": round(float(match.group()), 3),
                    "source": "tradingeconomics",
                    "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
    except Exception as e:
        logger.warning(f"TradingEconomics yield scrape failed for {bond}: {e}")
    return None


def _get_db_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "db" / "aetheer.db"


def _save_yield_to_history(bond: str, yield_pct: float, source: str):
    """Persiste yield en yields_history para fallback futuro."""
    try:
        db = _get_db_path()
        if not db.exists():
            return
        conn = sqlite3.connect(str(db))
        conn.execute(
            """INSERT INTO yields_history (bond, yield_pct, source, timestamp_utc)
               VALUES (?, ?, ?, ?)""",
            (bond, yield_pct, source, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to save yield to history: {e}")


def _get_last_known_yield(bond: str, max_age_hours: int = 72) -> dict | None:
    """Recupera último yield conocido de yields_history como fallback."""
    try:
        db = _get_db_path()
        if not db.exists():
            return None
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT yield_pct, source, timestamp_utc, created_at
               FROM yields_history
               WHERE bond = ?
               ORDER BY timestamp_utc DESC
               LIMIT 1""",
            (bond,),
        ).fetchone()
        conn.close()
        if row:
            created = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            if age_hours <= max_age_hours:
                return {
                    "bond": bond,
                    "yield_pct": row["yield_pct"],
                    "source": f"{row['source']}_cached",
                    "timestamp_utc": row["timestamp_utc"],
                    "age_hours": round(age_hours, 1),
                    "is_fallback": True,
                }
    except Exception as e:
        logger.warning(f"Failed to get yield from history: {e}")
    return None


async def get_yields() -> dict:
    """Get 10Y yields for US, Germany, UK with persistence and fallback.

    Cascada por bond:
      1. Yahoo Finance (US 10Y only)
      2. TradingEconomics (scraping)
      3. FRED API (US yields only, si key configurada)
      4. yields_history (último dato conocido, máx 72h)

    Cada lectura exitosa se persiste en yields_history.
    """
    results = {}
    sources_used = []

    # --- US 10Y ---
    tnx = await fetch_yahoo_quote("^TNX")
    if tnx:
        results["us_10y"] = tnx["price"]
        sources_used.append("yahoo")
        _save_yield_to_history("us_10y", tnx["price"], "yahoo")
    else:
        te = await scrape_tradingeconomics_yield("us_10y")
        if te:
            results["us_10y"] = te["yield_pct"]
            sources_used.append("tradingeconomics")
            _save_yield_to_history("us_10y", te["yield_pct"], "tradingeconomics")
        else:
            if FRED_API_KEY:
                fred_data = await fetch_fred_series("DGS10", limit=1)
                if fred_data and fred_data["value"] is not None:
                    results["us_10y"] = fred_data["value"]
                    sources_used.append("fred")
                    _save_yield_to_history("us_10y", fred_data["value"], "fred")

            if "us_10y" not in results:
                cached = _get_last_known_yield("us_10y")
                if cached:
                    results["us_10y"] = cached["yield_pct"]
                    results["us_10y_age_hours"] = cached["age_hours"]
                    sources_used.append(cached["source"])
                else:
                    results["us_10y"] = None

    # --- Bund 10Y (Germany) ---
    te = await scrape_tradingeconomics_yield("bund_10y")
    if te:
        results["bund_10y"] = te["yield_pct"]
        sources_used.append("tradingeconomics")
        _save_yield_to_history("de_10y", te["yield_pct"], "tradingeconomics")
    else:
        cached = _get_last_known_yield("de_10y")
        if cached:
            results["bund_10y"] = cached["yield_pct"]
            results["bund_10y_age_hours"] = cached["age_hours"]
            results["bund_10y_is_fallback"] = True
        else:
            results["bund_10y"] = None

    # --- Gilt 10Y (UK) ---
    te = await scrape_tradingeconomics_yield("gilt_10y")
    if te:
        results["gilt_10y"] = te["yield_pct"]
        sources_used.append("tradingeconomics")
        _save_yield_to_history("gb_10y", te["yield_pct"], "tradingeconomics")
    else:
        cached = _get_last_known_yield("gb_10y")
        if cached:
            results["gilt_10y"] = cached["yield_pct"]
            results["gilt_10y_age_hours"] = cached["age_hours"]
            results["gilt_10y_is_fallback"] = True
        else:
            results["gilt_10y"] = None

    # Calculate spreads (only if both sides available)
    if results.get("us_10y") and results.get("bund_10y"):
        results["spread_us_eu"] = round(results["us_10y"] - results["bund_10y"], 3)
    if results.get("us_10y") and results.get("gilt_10y"):
        results["spread_us_uk"] = round(results["us_10y"] - results["gilt_10y"], 3)

    results["sources_used"] = sources_used
    results["timestamp_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return results


async def get_correlations() -> dict:
    """Get correlation assets: Gold, VIX, S&P500, WTI."""
    assets = {}

    for name, symbol in [("gold_xauusd", "GC=F"), ("vix", "^VIX"), ("sp500", "^GSPC"), ("wti_crude", "CL=F")]:
        quote = await fetch_yahoo_quote(symbol)
        if quote:
            trend = "flat"
            if quote["change_pct_24h"] > 0.3:
                trend = "up"
            elif quote["change_pct_24h"] < -0.3:
                trend = "down"

            assets[name] = quote["price"]
            assets[f"{name}_trend"] = trend

            if name == "vix":
                if quote["price"] < 15:
                    assets["vix_regime"] = "risk_on"
                elif quote["price"] > 25:
                    assets["vix_regime"] = "risk_off"
                else:
                    assets["vix_regime"] = "neutral"

            if name == "wti_crude":
                if quote["price"] > 90:
                    assets["energy_inflation_pressure"] = "high"
                elif quote["price"] > 70:
                    assets["energy_inflation_pressure"] = "medium"
                else:
                    assets["energy_inflation_pressure"] = "low"

    assets["source"] = "yahoo_finance"
    assets["timestamp_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return assets


async def get_macro_indicator(country: str, indicator: str) -> dict:
    """Get a macro indicator from FRED or scrape."""
    country = country.upper()
    indicator = indicator.upper()

    series_map = FRED_SERIES.get(country, {})
    series_id = series_map.get(indicator)

    if series_id:
        result = await fetch_fred_series(series_id)
        if result:
            return {
                "country": country,
                "indicator": indicator,
                "value": result["value"],
                "date": result["date"],
                "source": "FRED",
                "series_id": series_id,
            }

    return {
        "country": country,
        "indicator": indicator,
        "value": None,
        "error": "No FRED API key configured or data unavailable. Set FRED_API_KEY env var.",
        "source": "none",
    }
