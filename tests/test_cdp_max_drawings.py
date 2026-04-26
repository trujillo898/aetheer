"""Cap + flag + consent gate tests for cdp_drawing.

Covers:
  - Exceeding max_drawings_per_analysis raises MaxDrawingsExceeded
  - Custom max_drawings_per_analysis is honored
  - Feature flag off → all draw_* return DrawingResult(skipped=True) without
    touching the bridge OR demanding consent
  - First draw_* without consent raises ConsentRequiredError; after grant it
    succeeds; revoke + retry raises again
"""
from __future__ import annotations

import pytest

from tv_unified_pkg.cdp_drawing import (  # type: ignore
    ConsentRequiredError,
    MaxDrawingsExceeded,
)
from tv_unified_pkg.drawing_schemas import HorizontalLine, PriceZone  # type: ignore


def _zone(top: float, bot: float) -> PriceZone:
    return PriceZone(
        symbol="EURUSD", timeframe="H1",
        price_top=top, price_bottom=bot,
        label="z", confidence=0.5,
    )


def _line(price: float) -> HorizontalLine:
    return HorizontalLine(
        symbol="EURUSD", timeframe="H1",
        price=price, label="l", confidence=0.5,
    )


@pytest.mark.asyncio
async def test_eleven_drawings_rejected_with_default_cap(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    too_many = [_line(1.10 + 0.001 * i) for i in range(11)]
    with pytest.raises(MaxDrawingsExceeded, match="max_drawings_per_analysis=10"):
        await drawer.draw_hlines(too_many)

    # And nothing was sent to the bridge.
    assert drawer.bridge.calls == []


@pytest.mark.asyncio
async def test_exactly_at_cap_is_allowed(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    at_cap = [_line(1.10 + 0.001 * i) for i in range(10)]
    res = await drawer.draw_hlines(at_cap)
    assert len(res.drawing_ids) == 10


@pytest.mark.asyncio
async def test_lower_cap_is_honored(drawer_factory):
    drawer = drawer_factory(flags={
        "enabled": True,
        "require_user_consent": True,
        "max_drawings_per_analysis": 3,
    })
    drawer.grant_drawing_consent()

    with pytest.raises(MaxDrawingsExceeded, match="max_drawings_per_analysis=3"):
        await drawer.draw_hlines([_line(1.1), _line(1.11), _line(1.12), _line(1.13)])


@pytest.mark.asyncio
async def test_feature_flag_off_makes_all_drawers_noop(drawer_factory):
    drawer = drawer_factory(flags={
        "enabled": False,
        "require_user_consent": True,
        "max_drawings_per_analysis": 10,
    })
    # Crucial: NO consent granted. Flag-off must short-circuit BEFORE the
    # consent gate, so this should not raise.
    res_z = await drawer.draw_zones([_zone(1.20, 1.10)])
    res_l = await drawer.draw_hlines([_line(1.10)])
    for res in (res_z, res_l):
        assert res.skipped is True
        assert res.skip_reason == "feature_flag_disabled"
        assert res.drawing_ids == []
        assert res.rollback_token == ""

    # Bridge was never touched.
    assert drawer.bridge.calls == []


@pytest.mark.asyncio
async def test_consent_required_then_granted_then_revoked(drawer_factory):
    drawer = drawer_factory()

    # 1. First call without consent raises.
    with pytest.raises(ConsentRequiredError):
        await drawer.draw_hlines([_line(1.10)])
    assert drawer.bridge.calls == []  # nothing reached the bridge

    # 2. Grant + retry succeeds.
    drawer.grant_drawing_consent(note="approved via CLI")
    assert drawer.has_consent() is True
    res = await drawer.draw_hlines([_line(1.10)])
    assert len(res.drawing_ids) == 1
    assert len(drawer.bridge.calls) == 1

    # 3. Revoke + retry raises again.
    drawer.revoke_drawing_consent()
    assert drawer.has_consent() is False
    with pytest.raises(ConsentRequiredError):
        await drawer.draw_hlines([_line(1.11)])


@pytest.mark.asyncio
async def test_consent_persists_across_drawer_instances(drawer_factory, tmp_path):
    """Consent lives in SQLite, not in-memory state, so a new drawer pointed
    at the same DB file inherits it."""
    consent_db = tmp_path / "shared_consents.db"
    rollback_db = tmp_path / "shared_rb.db"
    flags = {"enabled": True, "require_user_consent": True,
             "max_drawings_per_analysis": 10}

    d1 = drawer_factory(flags=flags, consent_db=consent_db, rollback_db=rollback_db)
    d1.grant_drawing_consent()

    d2 = drawer_factory(flags=flags, consent_db=consent_db, rollback_db=rollback_db)
    assert d2.has_consent() is True
    res = await d2.draw_hlines([_line(1.10)])
    assert len(res.drawing_ids) == 1


@pytest.mark.asyncio
async def test_consent_off_in_flag_skips_consent_check(drawer_factory):
    """If require_user_consent=False the gate is bypassed (escape hatch
    for headless test rigs). No grant call needed."""
    drawer = drawer_factory(flags={
        "enabled": True,
        "require_user_consent": False,
        "max_drawings_per_analysis": 10,
    })
    res = await drawer.draw_hlines([_line(1.10)])
    assert len(res.drawing_ids) == 1
