"""Pydantic schemas for TradingView news payloads.

Discovered endpoints:
  GET https://news-headlines.tradingview.com/v2/headlines?category=...&client=web&lang=es
  GET https://news-headlines.tradingview.com/v2/view/headlines/symbol?symbol=...&lang=es&client=web
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import CacheMeta


class RelatedSymbol(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    symbol: str
    logoid: Optional[str] = None
    currency_logoid: Optional[str] = Field(default=None, alias="currency-logoid")
    base_currency_logoid: Optional[str] = Field(default=None, alias="base-currency-logoid")


class TVNewsItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    title: str
    provider: str  # "reuters", "europapress", "beincrypto"… no cerramos con Literal
    source_logo_id: Optional[str] = Field(default=None, alias="sourceLogoId")
    published: int  # unix seconds
    source: str
    urgency: Optional[int] = None
    permission: Optional[str] = None
    link: Optional[str] = None
    story_path: Optional[str] = Field(default=None, alias="storyPath")
    related_symbols: Optional[list[RelatedSymbol]] = Field(default=None, alias="relatedSymbols")

    @property
    def published_dt(self) -> datetime:
        return datetime.fromtimestamp(self.published, tz=timezone.utc)


class NewsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[TVNewsItem]
    fetched_at: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp()))
    query: dict = Field(default_factory=dict)  # symbol/category/limit que produjo la lista
    meta: CacheMeta = Field(default_factory=CacheMeta)
