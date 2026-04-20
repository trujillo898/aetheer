"""
Cliente Alpha Vantage como fuente SECUNDARIA de precios Forex.

Alpha Vantage es el respaldo cuando TradingView no está disponible.

Endpoint:
- GET https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE
      &from_currency=EUR&to_currency=USD&apikey=KEY

Limitaciones del tier gratuito:
- 25 requests/día
- 5 requests/minuto

No soporta DXY. Solo pares Forex (EURUSD, GBPUSD).
"""

import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("aetheer.price-feed.alpha_vantage")

BASE_URL = "https://www.alphavantage.co/query"

# Mapeo de instrumentos Aetheer a parámetros Alpha Vantage
PAIR_MAP = {
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "AUDUSD": ("AUD", "USD"),
    "USDCAD": ("USD", "CAD"),
    "USDCHF": ("USD", "CHF"),
    "NZDUSD": ("NZD", "USD"),
}


class AlphaVantageClient:
    """Cliente async para Alpha Vantage Forex con rate limiting interno."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        # Rate limiting
        self._minute_calls: list[float] = []
        self._day_calls: list[float] = []
        self._day_reset: float = self._next_day_reset()

    def _next_day_reset(self) -> float:
        """Calcula el próximo reset diario a las 00:00 UTC."""
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if tomorrow <= now:
            from datetime import timedelta
            tomorrow += timedelta(days=1)
        return tomorrow.timestamp()

    def _check_rate_limit(self) -> bool:
        """Verifica si podemos hacer otra llamada. Retorna False si excedido."""
        now = time.time()

        # Reset diario
        if now >= self._day_reset:
            self._day_calls.clear()
            self._day_reset = self._next_day_reset()

        # Limpiar calls del último minuto
        self._minute_calls = [t for t in self._minute_calls if now - t < 60]

        # Verificar límites
        if len(self._minute_calls) >= 5:
            logger.info("[ALPHA_VANTAGE] Rate limit por minuto alcanzado (5/min)")
            return False
        if len(self._day_calls) >= 25:
            logger.info("[ALPHA_VANTAGE] Rate limit diario alcanzado (25/día)")
            return False

        return True

    def _record_call(self) -> None:
        """Registra una llamada para tracking de rate limits."""
        now = time.time()
        self._minute_calls.append(now)
        self._day_calls.append(now)

    @property
    def calls_remaining_minute(self) -> int:
        now = time.time()
        recent = [t for t in self._minute_calls if now - t < 60]
        return max(0, 5 - len(recent))

    @property
    def calls_remaining_day(self) -> int:
        now = time.time()
        if now >= self._day_reset:
            return 25
        return max(0, 25 - len(self._day_calls))

    async def get_price(self, instrument: str) -> dict | None:
        """Obtiene precio de un par Forex.

        Args:
            instrument: Formato Aetheer (EURUSD, GBPUSD)

        Returns:
            Dict con precio estandarizado o None si falla/sin quota.
        """
        pair = PAIR_MAP.get(instrument.upper())
        if not pair:
            return None

        if not self._check_rate_limit():
            return None

        from_currency, to_currency = pair
        params = {
            "function": "CURRENCY_EXCHANGE_RATE",
            "from_currency": from_currency,
            "to_currency": to_currency,
            "apikey": self.api_key,
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(BASE_URL, params=params, timeout=10.0)
                resp.raise_for_status()

            self._record_call()
            data = resp.json()

            rate_data = data.get("Realtime Currency Exchange Rate")
            if not rate_data:
                # Puede ser error de API key o rate limit de Alpha Vantage
                note = data.get("Note", data.get("Information", ""))
                if note:
                    logger.warning(f"[ALPHA_VANTAGE] API response: {note[:100]}")
                return None

            price = float(rate_data["5. Exchange Rate"])
            last_refreshed = rate_data.get("6. Last Refreshed", "")
            timezone_str = rate_data.get("7. Time Zone", "UTC")

            # Parsear timestamp
            try:
                dt = datetime.strptime(last_refreshed, "%Y-%m-%d %H:%M:%S")
                timestamp_utc = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            now = datetime.now(timezone.utc)
            try:
                ts = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
                age_seconds = int((now - ts).total_seconds())
            except Exception:
                age_seconds = 0

            logger.info(
                f"[ALPHA_VANTAGE] {instrument} -> {price} | {timestamp_utc} "
                f"(remaining: {self.calls_remaining_minute}/min, {self.calls_remaining_day}/day)"
            )

            return {
                "instrument": instrument.upper(),
                "price": round(price, 5),
                "source": "alpha_vantage",
                "timestamp_utc": timestamp_utc,
                "age_seconds": age_seconds,
            }

        except httpx.TimeoutException:
            logger.warning(f"[ALPHA_VANTAGE] Timeout para {instrument}")
            return None
        except Exception as e:
            logger.warning(f"[ALPHA_VANTAGE] Error para {instrument}: {e}")
            return None
