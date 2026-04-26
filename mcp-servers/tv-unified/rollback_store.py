"""SQLite-backed store for CDP drawing rollback tokens.

Each token maps to a list of (drawing_id, chart_symbol) tuples created in a
single draw_* call. Tokens older than 24h are purged on read/write.

Schema is created lazily on the first call so the store is self-contained and
tests can use throwaway DB files without running the migration runner.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_TTL = timedelta(hours=24)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cdp_rollback_tokens (
    rollback_token TEXT NOT NULL,
    drawing_id     TEXT NOT NULL,
    chart_symbol   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (rollback_token, drawing_id)
);
CREATE INDEX IF NOT EXISTS idx_cdp_rollback_created
    ON cdp_rollback_tokens(created_at);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RollbackStore:
    def __init__(self, db_path: str | Path, ttl: timedelta = DEFAULT_TTL):
        self.db_path = str(db_path)
        self.ttl = ttl
        # SQLite connections are not thread-safe across threads when
        # check_same_thread is left default; tests sometimes share an instance,
        # so we lock writes ourselves.
        self._lock = threading.Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, isolation_level=None)

    # ---------------------------------------------------------------- public

    def new_token(self) -> str:
        return uuid.uuid4().hex

    def save(
        self,
        rollback_token: str,
        entries: Iterable[tuple[str, str]],
    ) -> None:
        """Persist (drawing_id, chart_symbol) pairs under one rollback_token."""
        rows = [(rollback_token, did, sym, _now_iso()) for did, sym in entries]
        if not rows:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO cdp_rollback_tokens "
                "(rollback_token, drawing_id, chart_symbol, created_at) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
        self.purge_expired()

    def load(self, rollback_token: str) -> list[tuple[str, str]]:
        """Return [(drawing_id, chart_symbol), ...] for a token (after purge)."""
        self.purge_expired()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "SELECT drawing_id, chart_symbol FROM cdp_rollback_tokens "
                "WHERE rollback_token = ? ORDER BY drawing_id",
                (rollback_token,),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]

    def delete(self, rollback_token: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM cdp_rollback_tokens WHERE rollback_token = ?",
                (rollback_token,),
            )

    def purge_expired(self) -> int:
        cutoff = (datetime.now(timezone.utc) - self.ttl).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM cdp_rollback_tokens WHERE created_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0
