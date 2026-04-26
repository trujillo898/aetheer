"""Shared fixtures for cdp_drawing tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


class StubBridge:
    """Mimics TVBridge.evaluate(): records every JS payload, returns the id."""

    def __init__(self, *, fail_on_id: str | None = None):
        self.calls: list[str] = []
        self.fail_on_id = fail_on_id

    async def evaluate(self, js: str, await_promise: bool = False, timeout: float = 10.0) -> Any:
        self.calls.append(js)
        if self.fail_on_id and self.fail_on_id in js:
            raise RuntimeError(f"simulated bridge failure on {self.fail_on_id}")
        # The remove snippet is the only one that returns a boolean. We
        # detect it via the snippet body.
        if "delete window.__aetheerDrawings" in js:
            return True
        # draw_* snippets return p.id — we extract it from the JSON.parse(...)
        # payload to give the drawer back exactly what JS would have returned.
        marker = '"id": "'
        idx = js.find(marker)
        if idx == -1:
            # Try escaped form (JSON.parse gets a stringified payload)
            marker = '\\"id\\": \\"'
            idx = js.find(marker)
            if idx == -1:
                return None
            start = idx + len(marker)
            end = js.find('\\"', start)
        else:
            start = idx + len(marker)
            end = js.find('"', start)
        if end == -1:
            return None
        return js[start:end]


@pytest.fixture
def flag_dict_enabled() -> dict:
    return {
        "enabled": True,
        "require_user_consent": True,
        "max_drawings_per_analysis": 10,
        "auto_rollback_on_error": True,
    }


@pytest.fixture
def flag_dict_disabled() -> dict:
    return {
        "enabled": False,
        "require_user_consent": True,
        "max_drawings_per_analysis": 10,
        "auto_rollback_on_error": True,
    }


@pytest.fixture
def drawer_factory(tmp_path: Path):
    """Returns a callable: drawer_factory(flags=..., bridge=...) → drawer."""
    from tv_unified_pkg.cdp_drawing import TradingViewCDPDrawer  # type: ignore

    def _make(
        flags: dict | None = None,
        bridge: Any | None = None,
        *,
        consent_db: Path | None = None,
        rollback_db: Path | None = None,
    ) -> TradingViewCDPDrawer:
        return TradingViewCDPDrawer(
            bridge=bridge or StubBridge(),
            flags_path=tmp_path / "flags-not-used.yaml",
            db_path=consent_db or (tmp_path / "consents.db"),
            rollback_db_path=rollback_db or (tmp_path / "rollbacks.db"),
            flags_override=flags or {
                "enabled": True,
                "require_user_consent": True,
                "max_drawings_per_analysis": 10,
                "auto_rollback_on_error": True,
            },
        )

    return _make
