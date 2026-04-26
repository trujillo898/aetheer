"""End-to-end rollback tests for cdp_drawing.

Covers:
  - rollback removes exactly the drawings made under the returned token
  - rollback does NOT touch drawings created in a separate call
  - calling rollback twice is a no-op
  - feature flag off → rollback noops without hitting the bridge
  - 24h purge wipes stale tokens
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tv_unified_pkg.cdp_drawing import TradingViewCDPDrawer  # type: ignore
from tv_unified_pkg.drawing_schemas import HorizontalLine  # type: ignore
from tv_unified_pkg.rollback_store import RollbackStore  # type: ignore


def _line(price: float, label: str = "lvl") -> HorizontalLine:
    return HorizontalLine(
        symbol="EURUSD", timeframe="H1",
        price=price, label=label, confidence=0.7,
    )


@pytest.mark.asyncio
async def test_rollback_removes_only_call_drawings(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    res_a = await drawer.draw_hlines([_line(1.10), _line(1.11), _line(1.12)])
    res_b = await drawer.draw_hlines([_line(1.20), _line(1.21)])
    assert len(res_a.drawing_ids) == 3
    assert len(res_b.drawing_ids) == 2

    # Sanity: tokens are distinct.
    assert res_a.rollback_token != res_b.rollback_token

    rb = await drawer.rollback_drawings(res_a.rollback_token)
    assert sorted(rb.removed) == sorted(res_a.drawing_ids)
    assert rb.not_found == []

    # B's drawings are still tracked.
    surviving = drawer.rollback.load(res_b.rollback_token)
    surviving_ids = sorted(did for did, _ in surviving)
    assert surviving_ids == sorted(res_b.drawing_ids)

    # And A's token is gone from the store.
    assert drawer.rollback.load(res_a.rollback_token) == []


@pytest.mark.asyncio
async def test_rollback_token_is_idempotent_on_second_call(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    res = await drawer.draw_hlines([_line(1.10)])
    first = await drawer.rollback_drawings(res.rollback_token)
    assert len(first.removed) == 1

    second = await drawer.rollback_drawings(res.rollback_token)
    # Token is gone after first rollback → second call sees nothing to do.
    assert second.removed == []
    assert second.not_found == []


@pytest.mark.asyncio
async def test_rollback_with_unknown_token_is_safe(drawer_factory):
    drawer = drawer_factory()
    drawer.grant_drawing_consent()

    res = await drawer.rollback_drawings("does-not-exist")
    assert res.removed == []
    assert res.not_found == []


@pytest.mark.asyncio
async def test_feature_flag_off_rollback_is_noop(drawer_factory):
    """Even if a token exists, disabling the flag must short-circuit
    rollback so we never inject JS into a non-consenting environment."""
    drawer = drawer_factory()
    drawer.grant_drawing_consent()
    res = await drawer.draw_hlines([_line(1.10)])

    flipped = TradingViewCDPDrawer(
        bridge=drawer.bridge,
        flags_path=drawer.flags_path,
        db_path=drawer.consent.db_path,
        rollback_db_path=drawer.rollback.db_path,
        flags_override={"enabled": False, "require_user_consent": True,
                        "max_drawings_per_analysis": 10},
    )
    rb = await flipped.rollback_drawings(res.rollback_token)
    assert rb.removed == []
    # Token still in the store (we didn't actively touch the bridge).
    assert flipped.rollback.load(res.rollback_token)


def test_rollback_store_purges_after_ttl(tmp_path):
    """Tokens older than 24h should be auto-purged on next access."""
    store = RollbackStore(tmp_path / "rb.db", ttl=timedelta(hours=24))
    token = store.new_token()
    store.save(token, [("aetheer_one", "EURUSD"), ("aetheer_two", "EURUSD")])
    assert len(store.load(token)) == 2

    # Force the row to look 25h old.
    import sqlite3
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE cdp_rollback_tokens SET created_at = datetime('now', '-25 hours')"
        )
        conn.commit()

    # Next access triggers purge.
    assert store.load(token) == []
