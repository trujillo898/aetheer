"""
Pre-carga el calendario económico de las próximas 72 horas en SQLite.

Ejecución:
  python3 scripts/preload-calendar.py

Diseñado para correr como cron job cada 6 horas:
  0 */6 * * * cd ~/aetheer && .venv/bin/python scripts/preload-calendar.py >> logs/calendar-preload.log 2>&1

Flujo:
1. Scrapear calendario económico (misma lógica que economic-calendar/scraper.py)
2. Parsear eventos: nombre, datetime, moneda, importancia, consenso, anterior
3. Upsert en tabla `events` de SQLite (no duplicar si ya existe)
4. Marcar eventos pasados que ya tienen resultado como "completed"
5. Log: cuántos eventos nuevos insertados, cuántos actualizados, cuántos ya existían
"""

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "mcp-servers" / "economic-calendar"))

from scraper import get_calendar_events

DB_PATH = os.environ.get("DB_PATH", str(project_root / "db" / "aetheer.db"))


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrate_columns(db: sqlite3.Connection) -> None:
    """Agrega columnas nuevas a la tabla events si no existen."""
    columns_to_add = [
        ("source", "TEXT DEFAULT 'live'"),
        ("result_status", "TEXT DEFAULT 'pending'"),
        ("preloaded_at", "TEXT"),
    ]
    for col_name, col_def in columns_to_add:
        try:
            db.execute(f"ALTER TABLE events ADD COLUMN {col_name} {col_def}")
        except sqlite3.OperationalError:
            pass  # Column already exists


def preload(hours_ahead: int = 72) -> dict:
    """Pre-carga eventos en SQLite. Retorna estadísticas."""
    stats = {"new": 0, "updated": 0, "unchanged": 0, "completed": 0, "total": 0}
    now_str = _now_utc()

    # Scrape events
    events = asyncio.run(get_calendar_events(hours_ahead))
    stats["total"] = len(events)

    if events:
        sources = set(e.get("source", "unknown") for e in events)
        print(f"[PRELOAD] Fuente exitosa: {', '.join(sorted(sources))}")

    if not events:
        print(f"[PRELOAD] {now_str}")
        print("[PRELOAD] No se obtuvieron eventos del scraping.")
        return stats

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # Ensure migration
    _migrate_columns(db)

    for evt in events:
        event_name = evt.get("event", "Unknown")
        currency = evt.get("currency", "")
        event_dt = evt.get("datetime_utc", "")
        importance = evt.get("importance", "low")
        consensus = evt.get("consensus")
        previous = evt.get("previous")
        actual = evt.get("actual")

        # Check if event already exists
        existing = db.execute(
            """SELECT id, expected, previous, actual, result_status
               FROM events
               WHERE event_name = ? AND currency = ? AND event_datetime_utc = ?""",
            (event_name, currency, event_dt),
        ).fetchone()

        if existing:
            # Check if anything changed
            needs_update = False
            updates = []
            params = []

            if consensus is not None and existing["expected"] != consensus:
                updates.append("expected = ?")
                params.append(consensus)
                needs_update = True

            if previous is not None and existing["previous"] != previous:
                updates.append("previous = ?")
                params.append(previous)
                needs_update = True

            if actual is not None and existing["actual"] is None:
                updates.append("actual = ?")
                params.append(actual)
                updates.append("result_status = ?")
                params.append("completed")
                needs_update = True
                stats["completed"] += 1

            if needs_update:
                updates.append("preloaded_at = ?")
                params.append(now_str)
                params.append(existing["id"])
                db.execute(
                    f"UPDATE events SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
        else:
            # Insert new event
            result_status = "completed" if actual is not None else "pending"
            if actual is not None:
                stats["completed"] += 1

            db.execute(
                """INSERT INTO events
                   (event_name, currency, importance, expected, actual, previous,
                    event_datetime_utc, source, result_status, preloaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_name, currency, importance, consensus, actual, previous,
                 event_dt, "preloaded", result_status, now_str),
            )
            stats["new"] += 1

    db.commit()
    db.close()

    return stats


def main():
    now_str = _now_utc()
    print(f"[PRELOAD] {now_str}")

    try:
        stats = preload(72)
    except Exception as e:
        print(f"[PRELOAD] Error: {e}")
        sys.exit(1)

    print(f"[PRELOAD] Eventos próximas 72h: {stats['total']}")
    print(f"[PRELOAD] Nuevos insertados: {stats['new']}")
    print(f"[PRELOAD] Actualizados: {stats['updated']}")
    print(f"[PRELOAD] Sin cambios: {stats['unchanged']}")
    print(f"[PRELOAD] Completados (con resultado): {stats['completed']}")


if __name__ == "__main__":
    main()
