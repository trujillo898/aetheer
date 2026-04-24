"""Daily cost accounting + threshold alerts for OpenRouter calls.

Persists spend in a simple SQLite table (daily + per-agent). Emits alerts via
an injectable callback (Telegram sender in Fase 6; default: logger.warning).

The monitor is deliberately small:
    - `record(agent, cost_usd, tokens_in, tokens_out)` — non-blocking, sync OK
    - `spent_today_usd()` — current total
    - `should_downgrade()` — True if under soft threshold (`prefer_cheap` hint)
    - `should_block()` — True if over hard daily cap (router refuses premium)

Daily rollover is UTC to match TradingView sessions. No rolling averages,
no cost projection — keep it boring and auditable for now.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("aetheer.cost")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_utc TEXT NOT NULL,       -- 'YYYY-MM-DD'
    agent_name TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    model_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS cost_events_day_idx ON cost_events(day_utc);
"""

AlertSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    daily_cap_usd: float = 10.00         # hard cap — block premium above this
    soft_threshold_pct: float = 0.50     # prefer_cheap when spent > 50% of cap
    alert_threshold_pct: float = 0.80    # emit alert once when spent >= 80% of cap

    def soft_amount(self) -> float:
        return self.daily_cap_usd * self.soft_threshold_pct

    def alert_amount(self) -> float:
        return self.daily_cap_usd * self.alert_threshold_pct


class CostMonitor:
    """Thread-safe SQLite-backed daily cost tracker with 2-tier gating."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        config: BudgetConfig | None = None,
        alert_sink: AlertSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._config = config or BudgetConfig()
        self._sink = alert_sink or _default_alert_sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._alerted_for_day: str | None = None

        # Ensure parent dir + schema exist.
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _today(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def record(
        self,
        *,
        agent_name: str,
        cost_usd: float,
        prompt_tokens: int,
        completion_tokens: int,
        model_id: str | None = None,
    ) -> None:
        """Persist one call and maybe fire a threshold alert."""
        if cost_usd < 0:
            raise ValueError(f"cost_usd must be non-negative, got {cost_usd}")
        day = self._today()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO cost_events "
                "(day_utc, agent_name, cost_usd, prompt_tokens, completion_tokens, model_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day, agent_name, cost_usd, prompt_tokens, completion_tokens, model_id),
            )
            spent = _sum_for_day(conn, day)
        self._maybe_alert(day, spent)

    def spent_today_usd(self) -> float:
        with self._connect() as conn:
            return _sum_for_day(conn, self._today())

    def spent_by_agent_today(self) -> dict[str, float]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_name, SUM(cost_usd) AS total "
                "FROM cost_events WHERE day_utc = ? GROUP BY agent_name",
                (self._today(),),
            ).fetchall()
        return {r["agent_name"]: float(r["total"] or 0.0) for r in rows}

    def remaining_budget_usd(self) -> float:
        return max(0.0, self._config.daily_cap_usd - self.spent_today_usd())

    def should_downgrade(self) -> bool:
        """True when prefer_cheap should be forced. Used by model_router."""
        return self.spent_today_usd() >= self._config.soft_amount()

    def should_block(self) -> bool:
        """True when no more premium calls — hard daily cap reached."""
        return self.spent_today_usd() >= self._config.daily_cap_usd

    def _maybe_alert(self, day: str, spent: float) -> None:
        if self._alerted_for_day == day:
            return
        if spent >= self._config.alert_amount():
            self._alerted_for_day = day
            try:
                self._sink(
                    "OPENROUTER_BUDGET_ALERT",
                    {
                        "day_utc": day,
                        "spent_usd": round(spent, 4),
                        "daily_cap_usd": self._config.daily_cap_usd,
                        "pct_of_cap": round(spent / self._config.daily_cap_usd, 2),
                    },
                )
            except Exception:  # alert failure must never break a call
                logger.exception("cost alert sink raised; continuing")


def _sum_for_day(conn: sqlite3.Connection, day: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM cost_events WHERE day_utc = ?",
        (day,),
    ).fetchone()
    return float(row["total"] or 0.0)


def _default_alert_sink(code: str, payload: dict[str, Any]) -> None:
    logger.warning("cost alert %s: %s", code, payload)
