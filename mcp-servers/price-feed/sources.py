"""Price source cascade logic for Aetheer price-feed MCP server.

Cascada de prioridad:
  0: TradingView MCP (todos los instrumentos) — mismo feed que el gráfico del trader
     Solo disponible cuando TV Desktop corre con --remote-debugging-port=9222.
  1: Alpha Vantage API (EURUSD, GBPUSD) — respaldo API
  2: TradingEconomics scrape (DXY, EURUSD, GBPUSD)
  3: Investing.com scrape
  4: XE.com scrape (EURUSD, GBPUSD)
  5: Yahoo Finance API pública

Para DXY: TV → TradingEconomics → Investing → Yahoo.
"""

import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("aetheer.price-feed.sources")

# Mapeo de instrumentos Aetheer → símbolos TradingView (formato exchange:symbol)
TV_SYMBOLS: dict[str, str] = {
    "DXY":    "TVC:DXY",
    "EURUSD": "OANDA:EURUSD",
    "GBPUSD": "OANDA:GBPUSD",
    "XAUUSD": "OANDA:XAUUSD",
    "VIX":    "TVC:VIX",
    "SPX":    "SP:SPX",
    "US10Y":  "TVC:US10Y",
    "US02Y":  "TVC:US02Y",
    "USOIL":  "TVC:USOIL",
    "DE10Y":  "TVC:DE10Y",
    "GB10Y":  "TVC:GB10Y",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

_ua_index = 0


def _get_ua() -> str:
    global _ua_index
    ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
    _ua_index += 1
    return ua


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Mappings for scraping sources ---

YAHOO_SYMBOLS = {
    "DXY": "DX-Y.NYB",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
}

TE_PATHS = {
    "DXY": "/united-states/currency",
    "EURUSD": "/eurusd:cur",
    "GBPUSD": "/gbpusd:cur",
}

XE_PATHS = {
    "EURUSD": "/currencyconverter/convert/?From=EUR&To=USD&Amount=1",
    "GBPUSD": "/currencyconverter/convert/?From=GBP&To=USD&Amount=1",
}


# --- Alpha Vantage client singleton ---

_alpha_vantage_client = None


def _get_alpha_vantage_client():
    """Lazy-init del cliente Alpha Vantage."""
    global _alpha_vantage_client
    if _alpha_vantage_client is None:
        key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
        if key:
            from alpha_vantage import AlphaVantageClient
            _alpha_vantage_client = AlphaVantageClient(key)
    return _alpha_vantage_client


# --- Individual fetcher functions (independientes e importables) ---


async def fetch_from_tradingview(instrument: str) -> dict | None:
    """Fetch price from TradingView MCP (prioridad 0).

    Solo funciona cuando TV Desktop está corriendo con --remote-debugging-port=9222.
    Fallback silencioso si TV no está disponible — no bloquea la cascada.

    NOTA: Cambia el símbolo activo del gráfico del trader (limitación MVP).
    """
    try:
        from shared.tv_availability import is_tv_available, get_tv_quote
        if not is_tv_available():
            return None
        tv_symbol = TV_SYMBOLS.get(instrument)
        if not tv_symbol:
            return None
        quote = get_tv_quote(tv_symbol)
        if quote:
            price = quote.get("close") or quote.get("last")
            if price:
                return {
                    "instrument": instrument,
                    "price": round(float(price), 5),
                    "bid": quote.get("bid"),
                    "ask": quote.get("ask"),
                    "source": "tradingview",
                    "timestamp_utc": _now_utc(),
                    "quality_score": 0.98,
                }
    except Exception as e:
        logger.warning(f"TradingView fetch failed for {instrument}: {e}")
    return None


async def fetch_from_alpha_vantage(instrument: str) -> dict | None:
    """Fetch price from Alpha Vantage API (prioridad 1)."""
    client = _get_alpha_vantage_client()
    if client is None:
        return None
    return await client.get_price(instrument)


async def fetch_from_tradingeconomics(instrument: str) -> dict | None:
    """Fetch price from TradingEconomics public page (prioridad 2)."""
    path = TE_PATHS.get(instrument)
    if not path:
        return None
    url = f"https://tradingeconomics.com{path}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"User-Agent": _get_ua()}, timeout=10.0)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        price_el = soup.select_one("#tinline-0, .te-price-value, [id^='p']")
        if price_el:
            price_text = price_el.get_text(strip=True).replace(",", "")
            price = float(re.search(r"[\d.]+", price_text).group())
            return {
                "instrument": instrument,
                "price": round(price, 5),
                "source": "tradingeconomics",
                "timestamp_utc": _now_utc(),
            }
    except Exception as e:
        logger.warning(f"TradingEconomics failed for {instrument}: {e}")
    return None


async def fetch_from_investing(instrument: str) -> dict | None:
    """Fetch price from Investing.com (prioridad 3)."""
    slugs = {
        "DXY": "/indices/usdollar",
        "EURUSD": "/currencies/eur-usd",
        "GBPUSD": "/currencies/gbp-usd",
    }
    slug = slugs.get(instrument)
    if not slug:
        return None
    url = f"https://www.investing.com{slug}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": _get_ua(),
                    "Accept": "text/html",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=10.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        price_el = soup.select_one("[data-test='instrument-price-last']")
        if price_el:
            price_text = price_el.get_text(strip=True).replace(",", "")
            price = float(re.search(r"[\d.]+", price_text).group())
            return {
                "instrument": instrument,
                "price": round(price, 5),
                "source": "investing.com",
                "timestamp_utc": _now_utc(),
            }
    except Exception as e:
        logger.warning(f"Investing.com failed for {instrument}: {e}")
    return None


async def fetch_from_xe(instrument: str) -> dict | None:
    """Fetch price from XE.com (prioridad 4, solo pares Forex)."""
    path = XE_PATHS.get(instrument)
    if not path:
        return None
    url = f"https://www.xe.com{path}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"User-Agent": _get_ua(), "Accept": "text/html"},
                timeout=10.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        result_el = soup.select_one("[class*='result__BigRate'], [class*='unit-rates'] p")
        if result_el:
            text = result_el.get_text(strip=True).replace(",", "")
            match = re.search(r"[\d.]+", text)
            if match:
                price = float(match.group())
                return {
                    "instrument": instrument,
                    "price": round(price, 5),
                    "source": "xe.com",
                    "timestamp_utc": _now_utc(),
                }
    except Exception as e:
        logger.warning(f"XE.com failed for {instrument}: {e}")
    return None


async def fetch_from_yahoo(instrument: str) -> dict | None:
    """Fetch price from Yahoo Finance public API (prioridad 5)."""
    symbol = YAHOO_SYMBOLS.get(instrument)
    if not symbol:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"User-Agent": _get_ua()}, timeout=10.0)
            resp.raise_for_status()
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice", meta.get("previousClose"))
        if price:
            return {
                "instrument": instrument,
                "price": round(float(price), 5),
                "source": "yahoo_finance",
                "timestamp_utc": _now_utc(),
            }
    except Exception as e:
        logger.warning(f"Yahoo Finance failed for {instrument}: {e}")
    return None


# Símbolos Yahoo Finance para activos correlacionados
_YAHOO_CORRELATIONS: dict[str, str] = {
    "XAUUSD": "GC=F",   # Gold futures
    "VIX":    "^VIX",   # CBOE VIX Index
    "SPX":    "^GSPC",  # S&P 500
    "US10Y":  "^TNX",   # 10Y Treasury yield (x10 = yield %)
    "US02Y":  "^IRX",   # 13-week bill rate (proxy para 2Y)
    "USOIL":  "CL=F",   # WTI crude futures
    # DE10Y y GB10Y no disponibles de forma fiable en Yahoo Finance
}


async def _fetch_correlation_yahoo(instrument: str) -> dict | None:
    """Fetch de activo correlacionado desde Yahoo Finance."""
    symbol = _YAHOO_CORRELATIONS.get(instrument)
    if not symbol:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"User-Agent": _get_ua()}, timeout=10.0)
            resp.raise_for_status()
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice", meta.get("previousClose"))
        change_pct = None
        try:
            prev = float(meta.get("previousClose", 0))
            curr = float(price)
            if prev > 0:
                change_pct = round((curr - prev) / prev * 100, 3)
        except Exception:
            pass
        if price:
            return {
                "price": round(float(price), 5),
                "change_pct": change_pct,
                "source": "yahoo_finance",
                "timestamp": _now_utc(),
            }
    except Exception as e:
        logger.warning(f"Yahoo correlation fetch failed for {instrument}: {e}")
    return None


async def get_correlations_prices(instruments: list[str]) -> dict[str, dict]:
    """Obtiene precios de activos correlacionados.

    Prioridad 0: TradingView (si disponible).
    Fallback: Yahoo Finance para los instrumentos disponibles.

    Args:
        instruments: Lista de instrumentos (e.g. ["XAUUSD", "VIX", "SPX", "US10Y"])

    Returns:
        Dict: {instrument: {price, change_pct, source, timestamp}}
    """
    results: dict[str, dict] = {}

    # Intentar TradingView primero
    try:
        from shared.tv_availability import is_tv_available, get_tv_quote
        if is_tv_available():
            for instr in instruments:
                tv_symbol = TV_SYMBOLS.get(instr)
                if not tv_symbol:
                    continue
                quote = get_tv_quote(tv_symbol)
                if quote and (quote.get("close") or quote.get("last")):
                    price = quote.get("close") or quote.get("last")
                    results[instr] = {
                        "price": round(float(price), 5),
                        "change_pct": None,
                        "source": "tradingview",
                        "timestamp": _now_utc(),
                    }
    except Exception as e:
        logger.warning(f"TV correlations fetch failed: {e}")

    # Yahoo Finance fallback para lo que no salió de TV
    missing = [i for i in instruments if i not in results]
    for instr in missing:
        data = await _fetch_correlation_yahoo(instr)
        if data:
            results[instr] = data

    return results


# --- Default fetcher lists ---

# Para EURUSD/GBPUSD: TradingView primero, luego cascada existente
DEFAULT_FETCHERS_FOREX: list[Callable] = [
    fetch_from_tradingview,
    fetch_from_alpha_vantage,
    fetch_from_tradingeconomics,
    fetch_from_investing,
    fetch_from_xe,
    fetch_from_yahoo,
]

# Para DXY: TradingView primero (TV soporta DXY via TVC:DXY), luego scraping
DEFAULT_FETCHERS_DXY: list[Callable] = [
    fetch_from_tradingview,
    fetch_from_tradingeconomics,
    fetch_from_investing,
    fetch_from_yahoo,
]


def _get_default_fetchers(instrument: str) -> list[Callable]:
    """Retorna la lista de fetchers apropiada según el instrumento."""
    if instrument.upper() == "DXY":
        return DEFAULT_FETCHERS_DXY
    return DEFAULT_FETCHERS_FOREX


async def get_price_from_sources(
    instrument: str,
    fetchers: list[Callable] | None = None,
) -> dict:
    """Intenta todas las fuentes en cascada. Retorna primer resultado exitoso.

    Args:
        instrument: Instrumento en formato Aetheer (DXY, EURUSD, GBPUSD)
        fetchers: Lista opcional de funciones fetcher para inyección en tests.
                  Si es None, usa la cascada por defecto según el instrumento.
    """
    instrument = instrument.upper()

    if fetchers is None:
        fetchers = _get_default_fetchers(instrument)

    results = []
    for fetcher in fetchers:
        result = await fetcher(instrument)
        if result:
            results.append(result)
            if len(results) == 1:
                # Got at least one, try one more for divergence check
                continue
            break

    if not results:
        return {"error": "KILL_SWITCH", "message": "No price feed available"}

    primary = results[0]
    if "age_seconds" not in primary:
        primary["age_seconds"] = 0

    # Check divergence if we have multiple results
    if len(results) > 1:
        p1, p2 = results[0]["price"], results[1]["price"]
        if p1 > 0:
            divergence_pct = abs(p1 - p2) / p1 * 100
            if divergence_pct > 0.15:
                primary["divergence_warning"] = True
                primary["divergence_pct"] = round(divergence_pct, 4)
                primary["alt_source"] = results[1]["source"]
                primary["alt_price"] = results[1]["price"]

    return primary


# Backward-compatible alias
async def get_price_cascade(instrument: str) -> dict:
    """Alias de compatibilidad para get_price_from_sources."""
    return await get_price_from_sources(instrument)
