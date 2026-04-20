"""Pydantic schemas for TradingView economic calendar.

Discovered endpoint:
  GET https://economic-calendar.tradingview.com/events?from=...&to=...&countries=...
  Requiere headers: Origin + Referer de es.tradingview.com
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import CacheMeta


class TVCalendarEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    title: str
    country: str  # ISO2: US, EU, GB, DE, JP, ... (se mantiene abierto)
    indicator: Optional[str] = None
    ticker: Optional[str] = None  # Ej: "ECONOMICS:EUCOUT"
    comment: Optional[str] = None
    category: Optional[str] = None
    period: Optional[str] = None
    reference_date: Optional[str] = Field(default=None, alias="referenceDate")
    source: Optional[str] = None
    source_url: Optional[str] = None
    actual: Optional[float] = None
    previous: Optional[float] = None
    forecast: Optional[float] = None
    actual_raw: Optional[float] = Field(default=None, alias="actualRaw")
    previous_raw: Optional[float] = Field(default=None, alias="previousRaw")
    forecast_raw: Optional[float] = Field(default=None, alias="forecastRaw")
    currency: Optional[str] = None
    unit: Optional[str] = None
    importance: int = -1  # -1=holiday, 0=low, 1=medium, 2=high
    date: str  # ISO 8601

    @property
    def date_dt(self) -> datetime:
        return datetime.fromisoformat(self.date.replace("Z", "+00:00"))

    @property
    def is_high_impact(self) -> bool:
        return self.importance >= 2

    @property
    def surprise_direction(self) -> Optional[str]:
        """Dir de sorpresa respecto a forecast. None si no hay actual o forecast."""
        if self.actual is None or self.forecast is None:
            return None
        if self.actual > self.forecast:
            return "above_forecast"
        if self.actual < self.forecast:
            return "below_forecast"
        return "inline"


class CalendarResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = "ok"
    result: list[TVCalendarEvent]
    fetched_at: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp()))
    query: dict = Field(default_factory=dict)
    meta: CacheMeta = Field(default_factory=CacheMeta)
