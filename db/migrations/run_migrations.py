#!/usr/bin/env python3
"""Run pending database migrations for Aetheer.

Usage:
    python3 db/migrations/run_migrations.py

Safe to run multiple times — all migrations use IF NOT EXISTS.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "aetheer.db"
MIGRATIONS_DIR = Path(__file__).resolve().parent


def run_migrations():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))

    # Track applied migrations
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL UNIQUE,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)

    applied = {row[0] for row in conn.execute("SELECT filename FROM _migrations").fetchall()}

    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    new_count = 0

    for sql_file in sql_files:
        if sql_file.name in applied:
            print(f"  [SKIP] {sql_file.name} (already applied)")
            continue

        print(f"  [APPLY] {sql_file.name}...")
        sql = sql_file.read_text()
        try:
            conn.executescript(sql)
            conn.execute("INSERT INTO _migrations (filename) VALUES (?)", (sql_file.name,))
            conn.commit()
            print(f"  [OK] {sql_file.name}")
            new_count += 1
        except Exception as e:
            print(f"  [FAIL] {sql_file.name}: {e}")
            conn.rollback()
            sys.exit(1)

    conn.close()

    if new_count == 0:
        print("No new migrations to apply.")
    else:
        print(f"\n{new_count} migration(s) applied successfully.")


if __name__ == "__main__":
    print(f"Aetheer DB Migration — {DB_PATH}")
    run_migrations()
