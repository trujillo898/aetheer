"""TradingViewCDPDrawer — synthesis-side helper to draw on TV Desktop via CDP.

Defaults to NO-OP. Three independent gates must all be open before any JS hits
the wire:

  1. Feature flag `cdp_drawing.enabled` (config/feature_flags.yaml).
  2. Explicit user consent persisted in db/aetheer.db (table `user_consents`).
  3. The current call hasn't exceeded `cdp_drawing.max_drawings_per_analysis`.

Sanitization, ID issuance, and rollback bookkeeping are owned here so the JS
snippets can stay dumb.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .drawing_schemas import (  # noqa: F401  (re-export friendly path)
    DrawingResult,
    HorizontalLine,
    PriceZone,
    RollbackResult,
    TextAnnotation,
)
from .rollback_store import RollbackStore

logger = logging.getLogger("aetheer.tv_unified.cdp_drawing")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_FLAGS_PATH = _REPO_ROOT / "config" / "feature_flags.yaml"
_DEFAULT_DB_PATH = _REPO_ROOT / "db" / "aetheer.db"
_DEFAULT_ROLLBACK_DB = _REPO_ROOT / "db" / "tv_cache.sqlite"
_SNIPPETS_DIR = _REPO_ROOT / "scripts" / "tv_js_snippets"

DRAWING_ID_PREFIX = "aetheer_"

# Confidence bands → fill / line color. Hex (no rgba()) keeps CSS compact.
_COLOR_HIGH = "#1faa59"     # >= 0.66 — verde
_COLOR_MED = "#e6a417"      # 0.33–0.66 — amarillo
_COLOR_LOW = "#d54848"      # < 0.33 — rojo


class ConsentRequiredError(RuntimeError):
    """Raised by every draw_* before grant_drawing_consent() has been called."""


class MaxDrawingsExceeded(RuntimeError):
    """Raised when a single call would push us over max_drawings_per_analysis."""


# --------------------------------------------------------------------- helpers


def _confidence_color(conf: float) -> str:
    if conf >= 0.66:
        return _COLOR_HIGH
    if conf >= 0.33:
        return _COLOR_MED
    return _COLOR_LOW


def _new_drawing_id() -> str:
    return f"{DRAWING_ID_PREFIX}{uuid.uuid4().hex}"


def _safe_label(label: str) -> str:
    """Sanitize a free-form label for safe inclusion in a JS string literal.

    The JS snippets pass labels through .textContent (not innerHTML) so the
    browser will not parse HTML; this routine adds a Python-side belt-and-
    suspenders pass that strips control chars and the obvious script-tag
    pattern. Output is then JSON-encoded by the caller via `json.dumps`.
    """
    if label is None:
        return ""
    if not isinstance(label, str):
        label = str(label)
    # Drop NUL + other control bytes that have no business in a chart label.
    cleaned = "".join(ch for ch in label if ch == "\n" or ch == "\t" or ord(ch) >= 0x20)
    # Strip any literal </script ...> that survives — the JSON encoder also
    # escapes "/" but defense-in-depth is cheap.
    lowered = cleaned.lower()
    for needle in ("<script", "</script", "javascript:", "onerror=", "onload="):
        idx = lowered.find(needle)
        while idx != -1:
            cleaned = cleaned[:idx] + cleaned[idx + len(needle):]
            lowered = cleaned.lower()
            idx = lowered.find(needle)
    return cleaned[:80]


def _load_snippet(name: str) -> str:
    path = _SNIPPETS_DIR / name
    return path.read_text(encoding="utf-8")


def _render_snippet(snippet: str, params: dict[str, Any]) -> str:
    encoded = json.dumps(params, ensure_ascii=False, allow_nan=False)
    # JSON.parse on the JS side, NOT raw object literal, so a stray quote in
    # the encoded payload can't break out into JS.
    js_literal = json.dumps(encoded)  # python str -> JS string literal
    return snippet.replace("__PARAMS__", f"JSON.parse({js_literal})")


# --------------------------------------------------------------- consent store


class ConsentStore:
    """Tiny adapter on the user_consents table. Created lazily on first use."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS user_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature TEXT NOT NULL,
            granted INTEGER NOT NULL DEFAULT 0,
            granted_at TEXT,
            revoked_at TEXT,
            note TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_consents_feature
            ON user_consents(feature);
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, isolation_level=None)

    def is_granted(self, feature: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT granted FROM user_consents WHERE feature = ?",
                (feature,),
            ).fetchone()
        return bool(row and row[0] == 1)

    def grant(self, feature: str, note: str = "") -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO user_consents (feature, granted, granted_at, revoked_at, note, created_at, updated_at) "
                "VALUES (?, 1, ?, NULL, ?, ?, ?) "
                "ON CONFLICT(feature) DO UPDATE SET "
                "  granted=1, granted_at=excluded.granted_at, "
                "  revoked_at=NULL, note=excluded.note, updated_at=excluded.updated_at",
                (feature, ts, note, ts, ts),
            )

    def revoke(self, feature: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE user_consents SET granted=0, revoked_at=?, updated_at=? "
                "WHERE feature = ?",
                (ts, ts, feature),
            )


# ----------------------------------------------------------------- main class


class TradingViewCDPDrawer:
    """Synthesis's draw_* surface. Construct once per process; share across calls."""

    FEATURE = "cdp_drawing"

    def __init__(
        self,
        bridge: Any,
        *,
        flags_path: str | Path = _DEFAULT_FLAGS_PATH,
        db_path: str | Path = _DEFAULT_DB_PATH,
        rollback_db_path: str | Path = _DEFAULT_ROLLBACK_DB,
        flags_override: Optional[dict] = None,
    ):
        """
        Args:
            bridge: anything with `async evaluate(js, await_promise=False)`.
                    In production it's TVBridgeExtended; in tests it's a stub.
            flags_path: where to read feature_flags.yaml. Re-read on every
                        public call so a flip takes effect without restart.
            db_path: SQLite path holding user_consents.
            rollback_db_path: SQLite path for the rollback token store. Kept
                              separate so test fixtures can swap it.
            flags_override: if provided, used INSTEAD of reading flags_path.
                            For tests + programmatic overrides only.
        """
        self.bridge = bridge
        self.flags_path = Path(flags_path)
        self.flags_override = flags_override
        self.consent = ConsentStore(db_path)
        self.rollback = RollbackStore(rollback_db_path)
        self._snippets = {
            "zone": _load_snippet("draw_rect.js"),
            "hline": _load_snippet("draw_hline.js"),
            "text": _load_snippet("draw_text.js"),
            "remove": _load_snippet("remove_by_id.js"),
        }

    # ------------------------------------------------------------ flags

    def _flags(self) -> dict:
        if self.flags_override is not None:
            return self.flags_override
        if not self.flags_path.exists():
            return {}
        with self.flags_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("cdp_drawing", {}) or {}

    def _enabled(self) -> bool:
        return bool(self._flags().get("enabled", False))

    def _max_drawings(self) -> int:
        return int(self._flags().get("max_drawings_per_analysis", 10))

    def _require_consent(self) -> bool:
        return bool(self._flags().get("require_user_consent", True))

    # ------------------------------------------------------------ consent api

    def grant_drawing_consent(self, note: str = "") -> None:
        self.consent.grant(self.FEATURE, note=note)

    def revoke_drawing_consent(self) -> None:
        self.consent.revoke(self.FEATURE)

    def has_consent(self) -> bool:
        return self.consent.is_granted(self.FEATURE)

    # ------------------------------------------------------------ public draw

    async def draw_zones(self, zones: Iterable[PriceZone | dict]) -> DrawingResult:
        return await self._draw_many("zone", zones, PriceZone)

    async def draw_hlines(
        self, lines: Iterable[HorizontalLine | dict]
    ) -> DrawingResult:
        return await self._draw_many("hline", lines, HorizontalLine)

    async def draw_texts(
        self, texts: Iterable[TextAnnotation | dict]
    ) -> DrawingResult:
        return await self._draw_many("text", texts, TextAnnotation)

    async def rollback_drawings(self, rollback_token: str) -> RollbackResult:
        """Reverse only the drawings recorded under `rollback_token`."""
        if not self._enabled():
            return RollbackResult(rollback_token=rollback_token)

        entries = self.rollback.load(rollback_token)
        if not entries:
            return RollbackResult(rollback_token=rollback_token)

        chart_symbol = entries[0][1]
        removed: list[str] = []
        not_found: list[str] = []
        snippet = self._snippets["remove"]
        for drawing_id, _sym in entries:
            try:
                ok = await self.bridge.evaluate(
                    _render_snippet(snippet, {"id": drawing_id})
                )
            except Exception as e:
                logger.warning(f"rollback evaluate failed for {drawing_id}: {e}")
                ok = False
            if ok:
                removed.append(drawing_id)
            else:
                not_found.append(drawing_id)

        self.rollback.delete(rollback_token)
        return RollbackResult(
            rollback_token=rollback_token,
            removed=removed,
            not_found=not_found,
            chart_symbol=chart_symbol,
        )

    # -------------------------------------------------------------- internals

    async def _draw_many(
        self,
        kind: str,
        items: Iterable,
        model_cls: type,
    ) -> DrawingResult:
        if not self._enabled():
            return DrawingResult(skipped=True, skip_reason="feature_flag_disabled")

        if self._require_consent() and not self.has_consent():
            raise ConsentRequiredError(
                f"Consent for '{self.FEATURE}' not granted. "
                "Call grant_drawing_consent() before drawing."
            )

        # Coerce dicts → typed models. This is where NaN/inf/out-of-range
        # values get rejected with explicit ValueError messages.
        typed = [it if isinstance(it, model_cls) else model_cls(**it) for it in items]

        cap = self._max_drawings()
        if len(typed) > cap:
            raise MaxDrawingsExceeded(
                f"{len(typed)} drawings requested but max_drawings_per_analysis={cap}."
            )

        if not typed:
            return DrawingResult(skipped=True, skip_reason="empty_input")

        chart_symbol = typed[0].symbol
        token = self.rollback.new_token()
        snippet = self._snippets[kind]
        ids: list[str] = []
        registered: list[tuple[str, str]] = []

        try:
            for item in typed:
                payload = self._build_payload(kind, item)
                js = _render_snippet(snippet, payload)
                returned = await self.bridge.evaluate(js)
                drawing_id = payload["id"]
                if returned and returned != drawing_id:
                    logger.debug(
                        f"snippet returned {returned!r}, expected {drawing_id!r}"
                    )
                ids.append(drawing_id)
                registered.append((drawing_id, item.symbol))
        except Exception:
            # Best-effort rollback: persist what we did manage to draw, then
            # let the caller decide (auto_rollback_on_error is read by the
            # caller, not by the drawer, to keep responsibilities clean).
            if registered:
                self.rollback.save(token, registered)
            raise

        self.rollback.save(token, registered)
        return DrawingResult(
            drawing_ids=ids,
            rollback_token=token,
            chart_symbol=chart_symbol,
        )

    def _build_payload(self, kind: str, item: Any) -> dict[str, Any]:
        drawing_id = _new_drawing_id()
        common = {
            "id": drawing_id,
            "symbol": item.symbol,
            "timeframe": item.timeframe,
            "label": _safe_label(item.label),
            "confidence": item.confidence,
            "color": _confidence_color(item.confidence),
        }
        if kind == "zone":
            common["price_top"] = item.price_top
            common["price_bottom"] = item.price_bottom
        elif kind == "hline":
            common["price"] = item.price
        elif kind == "text":
            common["price"] = item.price
            common["text"] = _safe_label(item.text)
        else:
            raise ValueError(f"unknown drawing kind: {kind}")

        # Final paranoia: any non-finite slipped through? Refuse loudly.
        for k, v in common.items():
            if isinstance(v, float) and not math.isfinite(v):
                raise ValueError(f"non-finite {k}={v!r} in payload")
        return common
