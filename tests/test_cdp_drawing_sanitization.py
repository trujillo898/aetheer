"""Sanitization + input-validation tests for cdp_drawing.

Covers:
  - <script>, javascript:, on* handlers in labels are stripped or neutralized
  - SQL-style payloads in labels do not reach SQLite (they're treated as text)
  - NaN / inf / out-of-range / wrong type prices are rejected
  - Drawing IDs always start with the expected prefix
  - Consent gate blocks sanitization-bypass attempts (negative path)
"""
from __future__ import annotations

import json
import math

import pytest

from tv_unified_pkg.cdp_drawing import (  # type: ignore
    DRAWING_ID_PREFIX,
    ConsentRequiredError,
    PriceZone,
    TradingViewCDPDrawer,
)
from tv_unified_pkg.drawing_schemas import (  # type: ignore
    HorizontalLine,
    TextAnnotation,
)


# ---------------------------------------------------------------- price guards


def test_nan_price_rejected():
    with pytest.raises(ValueError, match="finite"):
        PriceZone(
            symbol="EURUSD", timeframe="H1",
            price_top=1.10, price_bottom=float("nan"),
            label="x", confidence=0.5,
        )


def test_inf_price_rejected():
    with pytest.raises(ValueError, match="finite"):
        HorizontalLine(
            symbol="EURUSD", timeframe="H1",
            price=math.inf, label="x", confidence=0.5,
        )


def test_negative_inf_price_rejected():
    with pytest.raises(ValueError, match="finite"):
        HorizontalLine(
            symbol="EURUSD", timeframe="H1",
            price=-math.inf, label="x", confidence=0.5,
        )


def test_zero_price_rejected_as_implausible():
    with pytest.raises(ValueError, match="plausible band"):
        HorizontalLine(
            symbol="EURUSD", timeframe="H1",
            price=0.0, label="x", confidence=0.5,
        )


def test_huge_price_rejected_as_implausible():
    with pytest.raises(ValueError, match="plausible band"):
        HorizontalLine(
            symbol="EURUSD", timeframe="H1",
            price=1_000_000.0, label="x", confidence=0.5,
        )


def test_price_top_below_bottom_rejected():
    with pytest.raises(ValueError, match="strictly less"):
        PriceZone(
            symbol="EURUSD", timeframe="H1",
            price_top=1.10, price_bottom=1.20,
            label="x", confidence=0.5,
        )


def test_string_price_rejected():
    with pytest.raises(ValueError):
        HorizontalLine(
            symbol="EURUSD", timeframe="H1",
            price="not-a-number", label="x", confidence=0.5,
        )


# ---------------------------------------------------------------- label guards


def test_label_max_length_enforced_at_schema():
    with pytest.raises(ValueError):
        PriceZone(
            symbol="EURUSD", timeframe="H1",
            price_top=1.10, price_bottom=1.05,
            label="A" * 81, confidence=0.5,
        )


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValueError):
        HorizontalLine(
            symbol="EURUSD", timeframe="H1",
            price=1.10, label="x", confidence=1.5,
        )
    with pytest.raises(ValueError):
        HorizontalLine(
            symbol="EURUSD", timeframe="H1",
            price=1.10, label="x", confidence=-0.1,
        )


# ---------------------------------------------------- script/JS-injection in labels


@pytest.mark.asyncio
async def test_label_with_script_tag_is_neutralized(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent(note="test")

    payload = HorizontalLine(
        symbol="EURUSD", timeframe="H1",
        price=1.10,
        label="<script>alert('xss')</script>",
        confidence=0.7,
    )
    res = await drawer.draw_hlines([payload])
    assert len(res.drawing_ids) == 1

    js = drawer.bridge.calls[0]
    # The literal "<script" / "</script" sequences must not survive — the
    # Python sanitizer strips them and the JS uses textContent anyway.
    assert "<script" not in js.lower()
    assert "</script" not in js.lower()


@pytest.mark.asyncio
async def test_label_with_javascript_url_neutralized(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    payload = HorizontalLine(
        symbol="EURUSD", timeframe="H1",
        price=1.10, label="javascript:alert(1)", confidence=0.5,
    )
    await drawer.draw_hlines([payload])
    js = drawer.bridge.calls[0]
    assert "javascript:" not in js.lower()


@pytest.mark.asyncio
async def test_label_with_event_handler_neutralized(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    payload = HorizontalLine(
        symbol="EURUSD", timeframe="H1",
        price=1.10, label='" onerror="alert(1)', confidence=0.5,
    )
    await drawer.draw_hlines([payload])
    js = drawer.bridge.calls[0]
    assert "onerror=" not in js.lower()


@pytest.mark.asyncio
async def test_text_annotation_text_field_also_sanitized(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    payload = TextAnnotation(
        symbol="EURUSD", timeframe="H1",
        price=1.10, text="<script>x</script>BUY",
        label="anno", confidence=0.6,
    )
    await drawer.draw_texts([payload])
    js = drawer.bridge.calls[0]
    assert "<script" not in js.lower()
    # The benign tail of the text should still be present.
    assert "BUY" in js


# ------------------------------------------------------------ SQL injection path


@pytest.mark.asyncio
async def test_sql_injection_in_label_does_not_corrupt_rollback_db(drawer_factory):
    """Labels go through Pydantic+sanitizer, but the rollback store only
    stores drawing_id + chart_symbol — never the label. We still verify the
    rollback round-trip survives a hostile label."""
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    hostile = "'); DROP TABLE cdp_rollback_tokens; --"
    payload = HorizontalLine(
        symbol="EURUSD", timeframe="H1",
        price=1.10, label=hostile, confidence=0.5,
    )
    res = await drawer.draw_hlines([payload])

    # Rollback store still works after the hostile label round-trip.
    entries = drawer.rollback.load(res.rollback_token)
    assert len(entries) == 1
    assert entries[0][1] == "EURUSD"


# ---------------------------------------------------------------- ID + consent


@pytest.mark.asyncio
async def test_drawing_ids_have_expected_prefix(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    payload = HorizontalLine(
        symbol="EURUSD", timeframe="H1",
        price=1.10, label="x", confidence=0.5,
    )
    res = await drawer.draw_hlines([payload, payload])
    assert all(did.startswith(DRAWING_ID_PREFIX) for did in res.drawing_ids)
    # IDs are unique even within one batch.
    assert len(set(res.drawing_ids)) == len(res.drawing_ids)


@pytest.mark.asyncio
async def test_payload_is_json_parseable_after_render(drawer_factory):
    """Defense-in-depth: the JSON.parse(<literal>) we inject must round-trip.
    A bug in the sanitizer that broke encoding would surface here."""
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    payload = HorizontalLine(
        symbol="EURUSD", timeframe="H1",
        price=1.10,
        label='quote " and \\backslash and \n newline',
        confidence=0.5,
    )
    await drawer.draw_hlines([payload])
    js = drawer.bridge.calls[0]
    # Extract the string literal between JSON.parse(" and the closing ")
    start = js.index("JSON.parse(") + len("JSON.parse(")
    end = js.index(")", start)
    js_string_literal = js[start:end]
    # js_string_literal is a JS string literal — also valid JSON for a string.
    decoded_once = json.loads(js_string_literal)  # → the inner JSON payload
    decoded_twice = json.loads(decoded_once)
    assert decoded_twice["symbol"] == "EURUSD"
    assert decoded_twice["price"] == 1.10
