"""Aetheer Memory MCP Server.

Provides persistent storage, retrieval, compression, and time decay
for the Aetheer context memory system using SQLite.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))
from compression import calculate_relevance_after_decay, compress_entry, should_compress

logging.basicConfig(level=logging.INFO, format="[memory] %(levelname)s %(message)s")
logger = logging.getLogger("aetheer.memory")

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent.parent.parent / "db" / "aetheer.db"))

mcp = FastMCP("memory")


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _init_db():
    """Initialize database from schema if tables don't exist."""
    schema_path = Path(__file__).parent / "schema.sql"
    if schema_path.exists():
        db = _get_db()
        db.executescript(schema_path.read_text())
        db.commit()
        db.close()
        logger.info("Database initialized from schema.sql")


# Initialize on import
_init_db()


VALID_TABLES = {
    "price_snapshots", "events", "session_stats",
    "context_memory", "user_profile", "agent_outputs", "heartbeat_log",
}


@mcp.tool()
async def store(table: str, data: str, ttl_days: int = 30) -> str:
    """Store data in a database table.

    Args:
        table: Table name (price_snapshots, events, session_stats, context_memory, agent_outputs, heartbeat_log)
        data: JSON string with column values to insert
        ttl_days: Time to live in days (used for context_memory decay_factor calculation)
    """
    if table not in VALID_TABLES:
        return json.dumps({"error": f"Invalid table. Valid: {', '.join(VALID_TABLES)}"})

    try:
        row = json.loads(data)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    try:
        db = _get_db()
        columns = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(row.values()))
        db.commit()
        last_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return json.dumps({"status": "ok", "id": last_id, "table": table})
    except Exception as e:
        logger.error(f"Store failed: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def query(table: str, filters: str = "{}", limit: int = 20, order: str = "recent") -> str:
    """Query data from a database table.

    Args:
        table: Table name to query
        filters: JSON string with column=value filters
        limit: Maximum rows to return (default: 20)
        order: "recent" (newest first) or "relevant" (highest relevance first, for context_memory)
    """
    if table not in VALID_TABLES:
        return json.dumps({"error": f"Invalid table. Valid: {', '.join(VALID_TABLES)}"})

    try:
        filter_dict = json.loads(filters) if filters else {}
    except json.JSONDecodeError:
        filter_dict = {}

    try:
        db = _get_db()
        query_sql = f"SELECT * FROM {table}"
        params = []

        if filter_dict:
            conditions = []
            for k, v in filter_dict.items():
                if isinstance(v, str) and "%" in v:
                    conditions.append(f"{k} LIKE ?")
                else:
                    conditions.append(f"{k} = ?")
                params.append(v)
            query_sql += " WHERE " + " AND ".join(conditions)

        if order == "relevant" and table == "context_memory":
            query_sql += " ORDER BY relevance_current DESC"
        else:
            query_sql += " ORDER BY id DESC"

        query_sql += f" LIMIT {min(limit, 100)}"

        rows = db.execute(query_sql, params).fetchall()

        # Update access count and last_accessed for context_memory
        if table == "context_memory" and rows:
            ids = [r["id"] for r in rows]
            db.execute(
                f"UPDATE context_memory SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({','.join('?' * len(ids))})",
                [_now_utc()] + ids,
            )
            db.commit()

        db.close()
        return json.dumps({"results": [dict(r) for r in rows], "count": len(rows)})
    except Exception as e:
        logger.error(f"Query failed: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def compress_old_entries(table: str = "context_memory", older_than_days: int = 7) -> str:
    """Compress old entries in context_memory to save space.

    Extracts key fields from verbose entries and replaces with compressed versions.

    Args:
        table: Table to compress (default: context_memory)
        older_than_days: Only compress entries older than N days
    """
    if table != "context_memory":
        return json.dumps({"error": "Compression only supported for context_memory"})

    try:
        db = _get_db()
        rows = db.execute(
            """SELECT id, content, category FROM context_memory
               WHERE compressed = 0
                 AND created_at < datetime('now', '-' || ? || ' days')""",
            (older_than_days,),
        ).fetchall()

        compressed_count = 0
        for row in rows:
            if should_compress(row["content"]):
                new_content = compress_entry(row["content"], row["category"])
                db.execute(
                    "UPDATE context_memory SET content = ?, compressed = 1 WHERE id = ?",
                    (new_content, row["id"]),
                )
                compressed_count += 1

        db.commit()
        db.close()

        return json.dumps({
            "status": "ok",
            "candidates": len(rows),
            "compressed": compressed_count,
            "timestamp": _now_utc(),
        })
    except Exception as e:
        logger.error(f"Compression failed: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def apply_time_decay() -> str:
    """Apply time decay to all context_memory entries.

    Reduces relevance_current based on decay_factor and time since last access.
    Deletes entries with relevance < 0.05.
    """
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT id, relevance_current, decay_factor, last_accessed FROM context_memory"
        ).fetchall()

        now = datetime.now(timezone.utc)
        updated = 0
        deleted = 0

        for row in rows:
            try:
                last_accessed = datetime.fromisoformat(row["last_accessed"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                last_accessed = now

            hours_since = (now - last_accessed).total_seconds() / 3600
            new_relevance = calculate_relevance_after_decay(
                row["relevance_current"], row["decay_factor"], hours_since
            )

            if new_relevance < 0.05:
                db.execute("DELETE FROM context_memory WHERE id = ?", (row["id"],))
                deleted += 1
            elif new_relevance != row["relevance_current"]:
                db.execute(
                    "UPDATE context_memory SET relevance_current = ? WHERE id = ?",
                    (new_relevance, row["id"]),
                )
                updated += 1

        db.commit()
        db.close()

        return json.dumps({
            "status": "ok",
            "total_entries": len(rows),
            "updated": updated,
            "deleted": deleted,
            "timestamp": _now_utc(),
        })
    except Exception as e:
        logger.error(f"Time decay failed: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_user_profile() -> str:
    """Get the user's contextual profile."""
    try:
        db = _get_db()
        row = db.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
        db.close()
        if row:
            return json.dumps(dict(row))
        return json.dumps({"message": "No profile found. Run bootstrap first."})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def update_user_profile(updates: str) -> str:
    """Update the user's contextual profile with partial data.

    Args:
        updates: JSON string with fields to update (partial merge)
    """
    try:
        update_dict = json.loads(updates)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    valid_fields = {
        "interaction_count", "preferred_detail_level", "recurring_topics",
        "typical_query_pattern", "last_interaction", "session_preference",
        "tone_calibration", "known_patterns",
    }

    filtered = {k: v for k, v in update_dict.items() if k in valid_fields}
    if not filtered:
        return json.dumps({"error": f"No valid fields. Valid: {', '.join(valid_fields)}"})

    try:
        db = _get_db()
        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [_now_utc()]
        db.execute(
            f"UPDATE user_profile SET {set_clause}, updated_at = ? WHERE id = 1",
            values,
        )
        db.commit()
        db.close()
        return json.dumps({"status": "ok", "updated_fields": list(filtered.keys())})
    except Exception as e:
        logger.error(f"Profile update failed: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_system_health() -> str:
    """Get system health metrics: table counts, obsolete entries, fragmentation."""
    try:
        db = _get_db()

        counts = {}
        for table in VALID_TABLES:
            try:
                row = db.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
                counts[table] = row["cnt"]
            except Exception:
                counts[table] = -1

        obsolete = 0
        try:
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM context_memory WHERE relevance_current < 0.05"
            ).fetchone()
            obsolete = row["cnt"]
        except Exception:
            pass

        # Fragmentation estimate: ratio of compressed to total in context_memory
        fragmentation = 0.0
        try:
            total = db.execute("SELECT COUNT(*) as cnt FROM context_memory").fetchone()["cnt"]
            compressed = db.execute(
                "SELECT COUNT(*) as cnt FROM context_memory WHERE compressed = 1"
            ).fetchone()["cnt"]
            if total > 0:
                fragmentation = round(compressed / total, 2)
        except Exception:
            pass

        # Last decay
        last_decay = None
        try:
            row = db.execute(
                "SELECT created_at FROM heartbeat_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                last_decay = row["created_at"]
        except Exception:
            pass

        db.close()

        return json.dumps({
            "status": "ok",
            "table_counts": counts,
            "obsolete_entries": obsolete,
            "fragmentation_estimate": fragmentation,
            "last_heartbeat": last_decay,
            "timestamp": _now_utc(),
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_current_time(compact: bool = False) -> str:
    """Get current date, time, active Forex sessions, and market status.

    REGLA: Todo agente que necesite hacer referencia temporal DEBE usar esta
    tool. Nunca calcular fechas, días de la semana ni horas mentalmente.

    Args:
        compact: True = una línea legible. False = JSON completo.

    Returns:
        JSON con UTC, hora local (America/Santiago), sesiones activas,
        solapamiento, estado del mercado Forex.
    """
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent.parent / "scripts" / "now.py"
    args = ["python3", str(script)]
    if compact:
        args.append("--compact")

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    # Fallback mínimo si el script falla
    now = datetime.now(timezone.utc)
    return json.dumps({
        "utc": {"datetime": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "warning": "now.py failed, fallback to basic datetime",
    })


if __name__ == "__main__":
    mcp.run()
