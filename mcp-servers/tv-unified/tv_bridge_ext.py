"""TVBridgeExtended — wraps shared/tv_bridge.TVBridge with HTTP helpers.

Adds JSON API fetchers for news + economic calendar discovered in the D013
consolidation (see Essence/06_DECISIONES.txt — D013 tv-unified).

These APIs do NOT require CDP — they are plain HTTPS — but we expose them via
the same bridge so all TV access lives behind one object with one health check.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp

# Import shared TVBridge — use absolute path insertion (mcp-servers/shared has hyphen-free name)
_SHARED = Path(__file__).resolve().parent.parent / "shared"
if str(_SHARED.parent) not in sys.path:
    sys.path.insert(0, str(_SHARED.parent))
from shared.tv_bridge import TVBridge  # noqa: E402

logger = logging.getLogger("aetheer.tv_unified.bridge")


NEWS_BASE = "https://news-headlines.tradingview.com"
CALENDAR_BASE = "https://economic-calendar.tradingview.com"

CALENDAR_HEADERS = {
    "Origin": "https://es.tradingview.com",
    "Referer": "https://es.tradingview.com/economic-calendar/",
    "User-Agent": "Mozilla/5.0 (Aetheer tv-unified)",
    "Accept": "application/json",
}


class TVBridgeExtended:
    """Facade over TVBridge + direct HTTP helpers."""

    def __init__(self, port: int = 9222):
        self._cdp = TVBridge(port=port)
        self._http: Optional[aiohttp.ClientSession] = None

    # -------- CDP passthrough --------

    @property
    def cdp(self) -> TVBridge:
        return self._cdp

    async def evaluate(self, js: str, await_promise: bool = False, timeout: float = 10.0):
        return await self._cdp.evaluate(js, await_promise=await_promise, timeout=timeout)

    async def cdp_connected(self) -> bool:
        try:
            return await self._cdp.health_check()
        except Exception as e:
            logger.debug(f"cdp health_check failed: {e}")
            return False

    # -------- HTTP session --------

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    # -------- News API --------

    async def fetch_news(
        self,
        symbol: Optional[str] = None,
        category: str = "forex",
        lang: str = "es",
        limit: int = 50,
        timeout: float = 10.0,
    ) -> list[dict]:
        """Fetch news from TradingView internal JSON API.

        If symbol given → /v2/view/headlines/symbol (more relevant).
        Else           → /v2/headlines?category=...
        Returns raw list of items (unvalidated).
        """
        session = await self._session()
        if symbol:
            url = f"{NEWS_BASE}/v2/view/headlines/symbol"
            params = {"symbol": symbol, "lang": lang, "client": "web"}
        else:
            url = f"{NEWS_BASE}/v2/headlines"
            params = {"category": category, "lang": lang, "client": "web"}

        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        items = data.get("items") if isinstance(data, dict) else data
        items = items or []
        return items[:limit]

    # -------- Calendar API --------

    async def fetch_calendar(
        self,
        countries: str = "US,EU,GB",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        timeout: float = 10.0,
    ) -> list[dict]:
        """Fetch economic calendar events. Requires Origin + Referer headers."""
        now = datetime.now(timezone.utc)
        if not from_date:
            from_date = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if not to_date:
            to_date = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        session = await self._session()
        params = {"from": from_date, "to": to_date, "countries": countries}
        async with session.get(
            f"{CALENDAR_BASE}/events",
            params=params,
            headers=CALENDAR_HEADERS,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if data.get("status") != "ok":
            raise RuntimeError(f"Calendar API non-ok response: {data}")
        return data.get("result") or []

    # -------- Health --------

    async def check_health(self) -> dict:
        """Check CDP + HTTP endpoints. Used by get_system_health and the monitor."""
        report: dict = {
            "cdp_connected": False,
            "news_api_ok": False,
            "calendar_api_ok": False,
            "errors": {},
        }

        try:
            report["cdp_connected"] = await self.cdp_connected()
        except Exception as e:
            report["errors"]["cdp"] = str(e)

        # Probar news API (público)
        try:
            items = await self.fetch_news(category="forex", limit=1, timeout=5)
            report["news_api_ok"] = bool(items) and "id" in items[0]
        except Exception as e:
            report["errors"]["news"] = str(e)

        # Probar calendar API (requiere headers)
        try:
            now = datetime.now(timezone.utc)
            t0 = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            t1 = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            await self.fetch_calendar(countries="US", from_date=t0, to_date=t1, timeout=5)
            report["calendar_api_ok"] = True
        except Exception as e:
            report["errors"]["calendar"] = str(e)

        return report

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        try:
            await self._cdp.disconnect()
        except Exception:
            pass
