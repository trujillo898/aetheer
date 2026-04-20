"""Health report schema."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class HealthReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str  # "online" | "offline"
    cdp_connected: bool = False
    news_api_ok: bool = False
    calendar_api_ok: bool = False
    operating_mode: str = "OFFLINE"  # ONLINE | OFFLINE
    errors: dict[str, str] = Field(default_factory=dict)
    cache_fallback_available: bool = False
    timestamp: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp()))
    details: Optional[dict] = None
