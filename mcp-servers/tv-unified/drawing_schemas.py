"""Pydantic schemas for tv-unified CDP drawing primitives (Fase 3).

Strict price-range and label-length checks live here so the drawer can trust
its inputs. NaN / inf are rejected at construction time — drawer never sees them.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Plausible Forex/index price band. Tighter than float-range to catch typos
# (e.g., "10482" instead of "104.82") before they hit TradingView.
PRICE_MIN = 0.0001
PRICE_MAX = 100_000.0

LABEL_MAX = 80


def _check_finite_price(v: float, *, field: str) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"{field} must be a number, got {type(v).__name__}")
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"{field} must be finite (got {v!r})")
    if not (PRICE_MIN < f < PRICE_MAX):
        raise ValueError(
            f"{field}={f} outside plausible band ({PRICE_MIN}, {PRICE_MAX})"
        )
    return f


class _DrawingBase(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    timeframe: str = Field(min_length=1, max_length=8)
    label: str = Field(default="", max_length=LABEL_MAX)
    confidence: float = Field(ge=0.0, le=1.0)


class PriceZone(_DrawingBase):
    price_top: float
    price_bottom: float

    @field_validator("price_top", "price_bottom", mode="before")
    @classmethod
    def _finite(cls, v, info):
        return _check_finite_price(v, field=info.field_name)

    @field_validator("price_bottom")
    @classmethod
    def _ordering(cls, v, info):
        top = info.data.get("price_top")
        if top is not None and v >= top:
            raise ValueError(
                f"price_bottom ({v}) must be strictly less than price_top ({top})"
            )
        return v


class HorizontalLine(_DrawingBase):
    price: float

    @field_validator("price", mode="before")
    @classmethod
    def _finite(cls, v, info):
        return _check_finite_price(v, field=info.field_name)


class TextAnnotation(_DrawingBase):
    price: float
    text: str = Field(min_length=1, max_length=LABEL_MAX)

    @field_validator("price", mode="before")
    @classmethod
    def _finite(cls, v, info):
        return _check_finite_price(v, field=info.field_name)


class DrawingResult(BaseModel):
    """Returned by every draw_* call.

    `skipped=True` means the feature flag is off (or any other no-op condition);
    in that case `drawing_ids` is empty and `rollback_token` is "".
    """
    drawing_ids: list[str] = Field(default_factory=list)
    rollback_token: str = ""
    chart_symbol: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    skipped: bool = False
    skip_reason: Optional[str] = None


class RollbackResult(BaseModel):
    rollback_token: str
    removed: list[str] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    chart_symbol: str = ""


DrawingKind = Literal["zone", "hline", "text"]
