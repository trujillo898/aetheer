"""Pydantic schemas for price + OHLCV data coming from TradingView CDP."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .common import CacheMeta


class OHLCV(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None

    @field_validator("timestamp")
    @classmethod
    def _plausible(cls, v: int) -> int:
        if not (1_000_000_000 <= v <= 4_000_000_000):
            raise ValueError(f"timestamp {v} fuera de rango UNIX razonable")
        return v


class PriceData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    change: Optional[float] = None
    change_percent: Optional[float] = None
    timestamp: int
    timeframe: Optional[str] = None
    meta: CacheMeta = Field(default_factory=CacheMeta)

    @property
    def datetime_utc(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)


class OHLCVResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    timeframe: str
    bars: list[OHLCV]
    meta: CacheMeta = Field(default_factory=CacheMeta)


class CorrelationsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prices: dict[str, PriceData]
    meta: CacheMeta = Field(default_factory=CacheMeta)
