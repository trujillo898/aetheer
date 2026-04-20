"""Economic calendar scraper for Aetheer.

Cascada de fuentes:
  1. Investing.com calendar API (JSON endpoint, no HTML)
  2. FXStreet calendar API (JSON)
  3. TradingEconomics HTML scrape (último recurso)

Filtro: solo eventos USD, EUR, GBP.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("aetheer.economic-calendar.scraper")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]
_ua_idx = 0


def _get_ua() -> str:
    global _ua_idx
    ua = USER_AGENTS[_ua_idx % len(USER_AGENTS)]
    _ua_idx += 1
    return ua


HIGH_IMPACT = {"cpi", "nfp", "non-farm", "interest rate", "rate decision", "fomc",
               "ecb rate", "boe rate", "gdp", "pce", "core pce", "payrolls"}
MEDIUM_IMPACT = {"pmi", "retail sales", "unemployment", "trade balance",
                 "industrial production", "consumer confidence", "housing starts",
                 "durable goods", "ism manufacturing", "ism services"}


def classify_importance(event_name: str, source_importance: str = "") -> str:
    name_lower = event_name.lower()
    if any(k in name_lower for k in HIGH_IMPACT):
        return "high"
    if any(k in name_lower for k in MEDIUM_IMPACT):
        return "medium"
    if source_importance.lower() in ("high", "3", "bull3"):
        return "high"
    if source_importance.lower() in ("medium", "2", "bull2"):
        return "medium"
    return "low"


def _parse_numeric(text: str) -> float | None:
    """Parsea texto numérico de calendario económico."""
    if not text or text.strip() in ("", "-", "—", "\xa0"):
        return None
    clean = text.strip().replace("%", "").replace(",", "")
    multiplier = 1
    if clean.upper().endswith("K"):
        clean = clean[:-1]
        multiplier = 1000
    elif clean.upper().endswith("M"):
        clean = clean[:-1]
        multiplier = 1_000_000
    elif clean.upper().endswith("B"):
        clean = clean[:-1]
        multiplier = 1_000_000_000
    try:
        return float(re.sub(r"[^\d.\-]", "", clean)) * multiplier
    except (ValueError, AttributeError):
        return None


# ═══════════════════════════════════════════════════════════════
# FUENTE 1: Investing.com Calendar JSON API
# ═══════════════════════════════════════════════════════════════

async def scrape_investing_calendar(hours_ahead: int = 72) -> list[dict]:
    """Scrape economic calendar from Investing.com.

    Intenta el endpoint JSON interno primero, luego HTML como fallback.
    """
    events = []

    try:
        events = await _investing_json_api(hours_ahead)
        if events:
            logger.info(f"[CALENDAR] Investing.com JSON API: {len(events)} events")
            return events
    except Exception as e:
        logger.warning(f"[CALENDAR] Investing.com JSON API failed: {e}")

    try:
        events = await _investing_html_scrape(hours_ahead)
        if events:
            logger.info(f"[CALENDAR] Investing.com HTML scrape: {len(events)} events")
            return events
    except Exception as e:
        logger.warning(f"[CALENDAR] Investing.com HTML scrape failed: {e}")

    return events


async def _investing_json_api(hours_ahead: int) -> list[dict]:
    """Investing.com tiene un endpoint JSON interno para el calendario."""
    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + timedelta(hours=hours_ahead)).strftime("%Y-%m-%d")

    url = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
    headers = {
        "User-Agent": _get_ua(),
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript",
        "Referer": "https://www.investing.com/economic-calendar/",
    }
    data = {
        "country[]": [5, 72, 4],  # 5=US, 72=EU, 4=UK
        "dateFrom": date_from,
        "dateTo": date_to,
        "timeZone": 55,  # UTC
        "timeFilter": "timeRemain",
        "currentTab": "nextSevenDays",
        "limit_from": 0,
    }

    events = []
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, data=data, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        result = resp.json()

        html_data = result.get("data", "")
        if html_data:
            soup = BeautifulSoup(html_data, "lxml")
            rows = soup.select("tr.js-event-item, tr[data-event-datetime]")

            for row in rows:
                try:
                    dt_attr = row.get("data-event-datetime", "")
                    currency_el = row.select_one("td.flagCur, td.left span")
                    currency = currency_el.get_text(strip=True)[:3].upper() if currency_el else ""

                    if currency not in ("USD", "EUR", "GBP"):
                        continue

                    event_el = row.select_one("td.event a, td.left.event a")
                    event_name = event_el.get_text(strip=True) if event_el else "Unknown"

                    importance_els = row.select("td i[class*='bull']")
                    source_importance = str(len(importance_els))

                    tds = row.select("td")
                    actual = consensus = previous = None
                    for td in tds:
                        td_id = td.get("id", "")
                        td_text = td.get_text(strip=True)
                        if "actual" in td_id:
                            actual = _parse_numeric(td_text)
                        elif "forecast" in td_id or "consensus" in td_id:
                            consensus = _parse_numeric(td_text)
                        elif "previous" in td_id:
                            previous = _parse_numeric(td_text)

                    # Parsear datetime del atributo — Investing envía "YYYY/MM/DD HH:MM:SS" en UTC
                    event_dt = None
                    if dt_attr:
                        try:
                            clean_dt = dt_attr.strip().replace("/", "-")
                            parsed = datetime.strptime(clean_dt, "%Y-%m-%d %H:%M:%S")
                            event_dt = parsed.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        except ValueError:
                            pass

                    if not event_dt:
                        # Sin datetime válido no podemos ordenar ni filtrar el evento
                        logger.debug(f"[CALENDAR] Skipping Investing event without parseable datetime: {event_name}")
                        continue

                    events.append({
                        "event": event_name,
                        "datetime_utc": event_dt,
                        "currency": currency,
                        "importance": classify_importance(event_name, source_importance),
                        "consensus": consensus,
                        "previous": previous,
                        "actual": actual,
                        "source": "investing_api",
                    })
                except Exception as e:
                    logger.debug(f"[CALENDAR] Failed to parse Investing row: {e}")

    return events


async def _investing_html_scrape(hours_ahead: int) -> list[dict]:
    """Fallback: scraping directo del HTML de Investing.com."""
    events = []
    url = "https://www.investing.com/economic-calendar/"
    headers = {
        "User-Agent": _get_ua(),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    rows = soup.select("tr.js-event-item, tr[data-event-datetime]")

    for row in rows:
        try:
            dt_attr = row.get("data-event-datetime", "")
            currency_el = row.select_one("td.flagCur, td.left span")
            currency = currency_el.get_text(strip=True)[:3].upper() if currency_el else ""

            if currency not in ("USD", "EUR", "GBP"):
                continue

            event_el = row.select_one("td.event a, td.left.event a")
            event_name = event_el.get_text(strip=True) if event_el else "Unknown"

            importance_els = row.select("td i[class*='bull']")
            source_importance = str(len(importance_els))

            tds = row.select("td")
            actual = consensus = previous = None
            for td in tds:
                td_id = td.get("id", "")
                td_text = td.get_text(strip=True)
                if "actual" in td_id:
                    actual = _parse_numeric(td_text)
                elif "forecast" in td_id:
                    consensus = _parse_numeric(td_text)
                elif "previous" in td_id:
                    previous = _parse_numeric(td_text)

            # Parsear datetime — mismo formato que JSON API ("YYYY/MM/DD HH:MM:SS" UTC)
            event_dt = None
            if dt_attr:
                try:
                    clean_dt = dt_attr.strip().replace("/", "-")
                    parsed = datetime.strptime(clean_dt, "%Y-%m-%d %H:%M:%S")
                    event_dt = parsed.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    pass

            if not event_dt:
                continue  # Descartar eventos sin datetime válido

            events.append({
                "event": event_name,
                "datetime_utc": event_dt,
                "currency": currency,
                "importance": classify_importance(event_name, source_importance),
                "consensus": consensus,
                "previous": previous,
                "actual": actual,
                "source": "investing_html",
            })
        except Exception:
            continue

    return events


# ═══════════════════════════════════════════════════════════════
# FUENTE 2: FXStreet Calendar API
# ═══════════════════════════════════════════════════════════════

async def scrape_fxstreet_calendar(hours_ahead: int = 72) -> list[dict]:
    """FXStreet tiene un endpoint JSON público para el calendario."""
    events = []
    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")  # desde ahora, no desde 00:00
    date_to = (now + timedelta(hours=hours_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = f"https://calendar-api.fxstreet.com/en/api/v1/eventDates/{date_from}/{date_to}"
    headers = {
        "User-Agent": _get_ua(),
        "Accept": "application/json",
        "Origin": "https://www.fxstreet.com",
        "Referer": "https://www.fxstreet.com/economic-calendar",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
            if resp.status_code != 200:
                logger.warning(f"[CALENDAR] FXStreet API returned {resp.status_code}")
                return events

            data = resp.json()

            for item in data:
                country = item.get("countryCode", "").upper()
                currency_map = {"US": "USD", "EU": "EUR", "GB": "GBP",
                                "EMU": "EUR", "UK": "GBP"}
                currency = currency_map.get(country, "")
                if not currency:
                    continue

                volatility = item.get("volatility", "").lower()
                importance = "high" if volatility == "high" else \
                             "medium" if volatility == "medium" else "low"

                event_name = item.get("name", "Unknown")
                importance = classify_importance(event_name, volatility) \
                    if importance == "low" else importance

                events.append({
                    "event": event_name,
                    "datetime_utc": item.get("dateUtc", ""),
                    "currency": currency,
                    "importance": importance,
                    "consensus": _parse_numeric(str(item.get("consensus", ""))),
                    "previous": _parse_numeric(str(item.get("previous", ""))),
                    "actual": _parse_numeric(str(item.get("actual", ""))),
                    "source": "fxstreet_api",
                })

        logger.info(f"[CALENDAR] FXStreet API: {len(events)} events")
    except Exception as e:
        logger.warning(f"[CALENDAR] FXStreet API failed: {e}")

    return events


# ═══════════════════════════════════════════════════════════════
# FUENTE 3: TradingEconomics Calendar
# ═══════════════════════════════════════════════════════════════

async def scrape_tradingeconomics_calendar(hours_ahead: int = 72) -> list[dict]:
    """Scrape TradingEconomics calendar page."""
    events = []
    url = "https://tradingeconomics.com/calendar"
    headers = {
        "User-Agent": _get_ua(),
        "Accept": "text/html",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15.0, follow_redirects=True)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        rows = soup.select("tr.calendar-item, table.table tbody tr")

        for row in rows:
            try:
                tds = row.select("td")
                if len(tds) < 5:
                    continue

                country_text = tds[1].get_text(strip=True) if len(tds) > 1 else ""
                currency_map = {
                    "United States": "USD", "Euro Area": "EUR", "United Kingdom": "GBP",
                    "US": "USD", "EU": "EUR", "UK": "GBP",
                }

                currency = None
                for key, val in currency_map.items():
                    if key.lower() in country_text.lower():
                        currency = val
                        break
                if not currency:
                    continue

                event_name = tds[2].get_text(strip=True) if len(tds) > 2 else "Unknown"

                # Intentar extraer datetime real de tds[0] o atributos del row
                event_dt = None
                dt_candidates = [
                    row.get("data-date", ""),
                    row.get("data-event-datetime", ""),
                    tds[0].get_text(strip=True) if tds else "",
                ]
                for candidate in dt_candidates:
                    if not candidate:
                        continue
                    try:
                        from dateutil import parser as dateparser
                        parsed = dateparser.parse(candidate)
                        if parsed:
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                            event_dt = parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
                            break
                    except Exception:
                        continue

                if not event_dt:
                    # Sin datetime real no podemos incluir el evento — fecha incorrecta
                    # es peor que no tener el evento
                    logger.debug(f"[CALENDAR] Skipping TradingEconomics event without parseable datetime: {event_name}")
                    continue

                events.append({
                    "event": event_name,
                    "datetime_utc": event_dt,
                    "currency": currency,
                    "importance": classify_importance(event_name),
                    "consensus": _parse_numeric(tds[5].get_text(strip=True)) if len(tds) > 5 else None,
                    "previous": _parse_numeric(tds[4].get_text(strip=True)) if len(tds) > 4 else None,
                    "actual": _parse_numeric(tds[3].get_text(strip=True)) if len(tds) > 3 else None,
                    "source": "tradingeconomics",
                })
            except Exception:
                continue

        logger.info(f"[CALENDAR] TradingEconomics: {len(events)} events")
    except Exception as e:
        logger.warning(f"[CALENDAR] TradingEconomics scrape failed: {e}")

    return events


# ═══════════════════════════════════════════════════════════════
# ORQUESTADOR (cascada de fuentes)
# ═══════════════════════════════════════════════════════════════

async def get_calendar_events(hours_ahead: int = 72) -> list[dict]:
    """Obtiene calendario económico de la primera fuente disponible.

    Cascada:
      1. Investing.com (JSON API + HTML fallback)
      2. FXStreet (JSON API)
      3. TradingEconomics (HTML scrape)

    Retorna lista de eventos filtrados a USD/EUR/GBP.
    """
    # Intento 1: Investing.com
    events = await scrape_investing_calendar(hours_ahead)
    if events:
        return events

    # Intento 2: FXStreet
    events = await scrape_fxstreet_calendar(hours_ahead)
    if events:
        return events

    # Intento 3: TradingEconomics
    events = await scrape_tradingeconomics_calendar(hours_ahead)
    if events:
        return events

    logger.error("[CALENDAR] Todas las fuentes de calendario fallaron")
    return []
