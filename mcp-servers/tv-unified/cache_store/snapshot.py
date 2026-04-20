"""SQLite-backed snapshot cache for tv-unified.

Single-table design with namespace discriminator. Cleaner than N tables for the
same (key, data_json, fetched_at, ttl) pattern.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class SnapshotCache:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tv_snapshots (
                    namespace TEXT NOT NULL,
                    key       TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
                    PRIMARY KEY (namespace, key)
                );
                CREATE INDEX IF NOT EXISTS idx_tv_snapshots_fetched
                    ON tv_snapshots(fetched_at);

                CREATE TABLE IF NOT EXISTS tv_health_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    operating_mode TEXT,
                    details_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tv_health_ts
                    ON tv_health_log(timestamp);
            """)

    @staticmethod
    def _now() -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def get(
        self,
        namespace: str,
        key: str,
        max_age_seconds: Optional[int] = None,
    ) -> Optional[tuple[dict, int]]:
        """Return (data, age_seconds) or None if miss / expired.

        Si max_age_seconds es None, usa el ttl_seconds guardado al set.
        Si se pasa un valor, lo usa como cota superior (útil para "stale fallback"
        con ventana extendida).
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT data_json, fetched_at, ttl_seconds FROM tv_snapshots "
                "WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        age = self._now() - row["fetched_at"]
        cutoff = max_age_seconds if max_age_seconds is not None else row["ttl_seconds"]
        if age > cutoff:
            return None
        return json.loads(row["data_json"]), age

    def set(
        self,
        namespace: str,
        key: str,
        data: dict,
        ttl_seconds: int,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tv_snapshots "
                "(namespace, key, data_json, fetched_at, ttl_seconds) "
                "VALUES (?, ?, ?, ?, ?)",
                (namespace, key, json.dumps(data, default=str), self._now(), ttl_seconds),
            )

    def log_health(
        self,
        status: str,
        operating_mode: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO tv_health_log (timestamp, status, operating_mode, details_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    self._now(),
                    status,
                    operating_mode,
                    json.dumps(details) if details else None,
                ),
            )

    def last_health(self, limit: int = 1) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, status, operating_mode, details_json "
                "FROM tv_health_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "timestamp": r["timestamp"],
                "status": r["status"],
                "operating_mode": r["operating_mode"],
                "details": json.loads(r["details_json"]) if r["details_json"] else None,
            }
            for r in rows
        ]


# Default TTLs por namespace (segundos)
DEFAULT_TTLS: dict[str, int] = {
    "price": 30,        # precio spot
    "ohlcv": 60,        # velas
    "correlations": 60, # paquete multi-instrumento
    "indicators": 60,   # Aetheer Pine label
    "news": 1800,       # 30 min — noticias cambian lento
    "calendar": 3600,   # 1 h — calendario estable
}

# Ventana extendida para "stale fallback" (cuando TV está caído)
STALE_MAX_SECONDS = 1800  # 30 min. Alineado con el kill-switch (D010).
