"""Shared pydantic bits: CacheMeta and ResponseMeta."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CacheMeta(BaseModel):
    """Metadata about cache state for a response. Always safe to ignore if fresh."""
    model_config = ConfigDict(extra="ignore")

    from_cache: bool = False
    cache_age_seconds: Optional[int] = Field(default=None, ge=0)
    stale: bool = False  # True si se sirvió cache pasado su TTL normal (fallback)
    source: str = "tradingview_cdp"
    quality_score: float = Field(default=0.98, ge=0.0, le=1.0)
