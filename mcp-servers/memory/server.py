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
from trajectory_store import AnalysisTrajectory, TrajectoryStore

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


def _trajectory_store() -> TrajectoryStore:
    """Single shared store (cheap to construct; sqlite handles concurrency)."""
    return TrajectoryStore(DB_PATH)


VALID_TABLES = {
    "price_snapshots", "events", "session_stats",
    "context_memory", "user_profile", "agent_outputs", "heartbeat_log",
    "trade_log",
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


@mcp.tool()
async def log_trade(trade: str) -> str:
    """Registrar una operación nueva en el trade journal.

    Permite que Aetheer aprenda del histórico real del trader. El contexto
    de mercado (regime, ema_align, dxy_bias, etc.) se guarda como snapshot
    para análisis post-mortem y calibración futura.

    Args:
        trade: JSON con campos del trade. Mínimo:
          - instrument (str): "EURUSD", "GBPUSD", "DXY", ...
          - direction (str): "long" o "short"
          - entry_price (float)
          - entry_time_utc (str ISO8601, opcional — default now)
          Opcionales:
          - stop_loss, take_profit, risk_pct, size_units (float)
          - thesis (str), tags (str CSV)
          - market_context (dict) — se serializa como JSON

    Returns:
        JSON {status, id, ...} con el id del trade creado.
    """
    try:
        t = json.loads(trade)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    required = ("instrument", "direction", "entry_price")
    missing = [f for f in required if f not in t]
    if missing:
        return json.dumps({"error": f"Faltan campos: {missing}"})

    if t["direction"] not in ("long", "short"):
        return json.dumps({"error": "direction debe ser 'long' o 'short'"})

    market_ctx = t.pop("market_context", None)
    if market_ctx is not None and not isinstance(market_ctx, str):
        t["market_context_json"] = json.dumps(market_ctx)
    elif market_ctx is not None:
        t["market_context_json"] = market_ctx

    t.setdefault("entry_time_utc", _now_utc())
    t.setdefault("outcome", "open")

    allowed = {
        "instrument", "direction", "entry_price", "stop_loss", "take_profit",
        "exit_price", "risk_pct", "size_units", "outcome", "pips",
        "r_multiple", "duration_minutes", "market_context_json",
        "thesis", "tags", "exit_reason", "post_mortem",
        "entry_time_utc", "exit_time_utc",
    }
    row = {k: v for k, v in t.items() if k in allowed}

    try:
        db = _get_db()
        cols = ", ".join(row.keys())
        ph = ", ".join(["?"] * len(row))
        db.execute(f"INSERT INTO trade_log ({cols}) VALUES ({ph})", list(row.values()))
        db.commit()
        last_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return json.dumps({"status": "ok", "id": last_id, "entry_time_utc": row["entry_time_utc"]})
    except Exception as e:
        logger.error(f"log_trade falló: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def update_trade_outcome(updates: str) -> str:
    """Cerrar un trade abierto o actualizar campos post-trade.

    Calcula automáticamente pips y r_multiple si hay datos suficientes
    (entry_price, exit_price, stop_loss). Calcula duration_minutes desde
    entry_time_utc hasta exit_time_utc (o ahora si no se provee).

    Args:
        updates: JSON con:
          - id (int, requerido): ID del trade
          - exit_price (float, opcional)
          - exit_time_utc (str ISO8601, opcional — default now)
          - outcome (str, opcional): "win"|"loss"|"be"|"cancelled"
            (auto-derivado si no se provee y hay exit_price + direction)
          - exit_reason (str, opcional)
          - post_mortem (str, opcional)
          - tags (str, opcional)

    Returns:
        JSON {status, id, computed: {pips, r_multiple, duration_minutes}}
    """
    try:
        u = json.loads(updates)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    trade_id = u.get("id")
    if not trade_id:
        return json.dumps({"error": "id requerido"})

    try:
        db = _get_db()
        existing = db.execute(
            "SELECT * FROM trade_log WHERE id = ?", (trade_id,)
        ).fetchone()
        if not existing:
            db.close()
            return json.dumps({"error": f"Trade id={trade_id} no existe"})

        e_dict = dict(existing)
        exit_price = u.get("exit_price", e_dict.get("exit_price"))
        exit_time = u.get("exit_time_utc") or _now_utc()
        u.setdefault("exit_time_utc", exit_time)

        # Auto-cálculo de pips, r_multiple, duration, outcome
        computed: dict = {}
        if exit_price is not None and e_dict.get("entry_price") is not None:
            entry = float(e_dict["entry_price"])
            exit_p = float(exit_price)
            direction = e_dict["direction"]

            # pips: convención Forex (4 decimales para majors, 2 para JPY)
            instrument = (e_dict.get("instrument") or "").upper()
            pip_size = 0.01 if "JPY" in instrument else 0.0001
            raw_diff = (exit_p - entry) if direction == "long" else (entry - exit_p)
            pips = round(raw_diff / pip_size, 1)
            computed["pips"] = pips
            u["pips"] = pips

            # r_multiple: requiere stop_loss
            sl = e_dict.get("stop_loss")
            if sl is not None:
                risk = abs(entry - float(sl))
                if risk > 0:
                    r = round(raw_diff / risk, 2)
                    computed["r_multiple"] = r
                    u["r_multiple"] = r

            # outcome auto si no provisto
            if "outcome" not in u:
                if pips > 0.5:
                    u["outcome"] = "win"
                elif pips < -0.5:
                    u["outcome"] = "loss"
                else:
                    u["outcome"] = "be"
                computed["outcome"] = u["outcome"]

        # duration_minutes
        try:
            entry_t = datetime.fromisoformat(
                e_dict["entry_time_utc"].replace("Z", "+00:00")
            )
            exit_t = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
            dur = int((exit_t - entry_t).total_seconds() / 60)
            u["duration_minutes"] = dur
            computed["duration_minutes"] = dur
        except (ValueError, TypeError, AttributeError):
            pass

        allowed = {
            "exit_price", "exit_time_utc", "outcome", "pips", "r_multiple",
            "duration_minutes", "exit_reason", "post_mortem", "tags",
        }
        update_fields = {k: v for k, v in u.items() if k in allowed and v is not None}
        if not update_fields:
            db.close()
            return json.dumps({"error": "Nada que actualizar"})

        set_clause = ", ".join(f"{k} = ?" for k in update_fields)
        db.execute(
            f"UPDATE trade_log SET {set_clause} WHERE id = ?",
            list(update_fields.values()) + [trade_id],
        )
        db.commit()
        db.close()
        return json.dumps({"status": "ok", "id": trade_id, "computed": computed})
    except Exception as e:
        logger.error(f"update_trade_outcome falló: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def record_causal_chain(chain: str) -> str:
    """Persistir una causal chain generada por price-behavior.

    Args:
        chain: JSON con campos:
          - cc_id, instrument, timeframe (str)
          - cause, effect, invalid_condition (str)
          - confidence (float)
          - trigger_struct (dict, opcional) — para validación automática.
            Ejemplo: {"type":"price_break","level":1.35100,"side":"below","instrument":"GBPUSD"}
          - market_state (dict, opcional) — snapshot Aetheer al crear

    Returns:
        JSON {status, id, expires_at}
    """
    try:
        c = json.loads(chain)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    required = ("instrument", "cause", "effect", "invalid_condition", "confidence")
    missing = [f for f in required if f not in c]
    if missing:
        return json.dumps({"error": f"Faltan campos: {missing}"})

    import sys as _sys
    from pathlib import Path as _Path
    _shared = _Path(__file__).resolve().parent.parent / "shared"
    if str(_shared) not in _sys.path:
        _sys.path.insert(0, str(_shared))
    from causal_chain_validator import calc_expiry

    trigger = c.get("trigger_struct")
    market = c.get("market_state")
    expires_at = calc_expiry(c.get("timeframe"))

    row = {
        "cc_id": c.get("cc_id"),
        "instrument": c["instrument"].upper(),
        "timeframe": c.get("timeframe"),
        "cause": c["cause"],
        "effect": c["effect"],
        "invalid_condition": c["invalid_condition"],
        "trigger_struct_json": json.dumps(trigger) if trigger else None,
        "confidence_initial": float(c["confidence"]),
        "confidence_current": float(c["confidence"]),
        "status": "open",
        "market_state_at_creation_json": json.dumps(market) if market else None,
        "expires_at": expires_at,
    }

    try:
        db = _get_db()
        cols = ", ".join(row.keys())
        ph = ", ".join(["?"] * len(row))
        db.execute(f"INSERT INTO causal_chains ({cols}) VALUES ({ph})", list(row.values()))
        db.commit()
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return json.dumps({"status": "ok", "id": cid, "expires_at": expires_at})
    except Exception as e:
        logger.error(f"record_causal_chain falló: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def validate_causal_chains(market_snapshot_json: str = "{}") -> str:
    """Loop de validación: recorre chains 'open', evalúa triggers, ajusta confidence.

    Diseñado para ejecutarse cada hora (scripts/validate_chains.py o cron).

    Args:
        market_snapshot_json: JSON {instrument: {price, aetheer_indicator, aetheer_per_tf}}
            con el snapshot actual de mercado. Si vacío, solo se procesan expiraciones.

    Returns:
        JSON con counters de chains procesadas, invalidadas, expiradas, etc.
    """
    try:
        snap = json.loads(market_snapshot_json) if market_snapshot_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid snapshot JSON: {e}"})

    import sys as _sys
    from pathlib import Path as _Path
    _shared = _Path(__file__).resolve().parent.parent / "shared"
    if str(_shared) not in _sys.path:
        _sys.path.insert(0, str(_shared))
    from causal_chain_validator import (
        evaluate_trigger,
        adjust_confidence_post_validation,
    )

    counters = {
        "processed": 0,
        "invalidated": 0,
        "expired": 0,
        "still_open": 0,
        "no_trigger_skipped": 0,
    }
    invalidations = []

    try:
        db = _get_db()
        # 1) Marcar expiradas
        expired_rows = db.execute(
            "SELECT id, cc_id, instrument FROM causal_chains "
            "WHERE status = 'open' AND expires_at < datetime('now')"
        ).fetchall()
        for r in expired_rows:
            db.execute(
                "UPDATE causal_chains SET status='expired', resolved_at=? WHERE id=?",
                (_now_utc(), r["id"]),
            )
            counters["expired"] += 1

        # 2) Evaluar triggers de chains aún abiertas
        open_rows = db.execute(
            "SELECT * FROM causal_chains WHERE status='open'"
        ).fetchall()

        for row in open_rows:
            counters["processed"] += 1
            trig_json = row["trigger_struct_json"]
            if not trig_json:
                counters["no_trigger_skipped"] += 1
                db.execute(
                    "UPDATE causal_chains SET last_checked_at=? WHERE id=?",
                    (_now_utc(), row["id"]),
                )
                continue
            try:
                trigger = json.loads(trig_json)
            except json.JSONDecodeError:
                counters["no_trigger_skipped"] += 1
                continue

            reason = evaluate_trigger(trigger, snap)
            if reason:
                new_conf = adjust_confidence_post_validation(
                    row["confidence_initial"], validated=False
                )
                db.execute(
                    """UPDATE causal_chains
                       SET status='invalidated',
                           confidence_current=?,
                           invalidation_reason=?,
                           market_state_at_resolution_json=?,
                           resolved_at=?,
                           last_checked_at=?
                       WHERE id=?""",
                    (
                        new_conf, reason, json.dumps(snap),
                        _now_utc(), _now_utc(), row["id"],
                    ),
                )
                counters["invalidated"] += 1
                invalidations.append({
                    "id": row["id"],
                    "cc_id": row["cc_id"],
                    "instrument": row["instrument"],
                    "reason": reason,
                    "new_confidence": new_conf,
                })
            else:
                counters["still_open"] += 1
                db.execute(
                    "UPDATE causal_chains SET last_checked_at=? WHERE id=?",
                    (_now_utc(), row["id"]),
                )

        db.commit()
        db.close()
        return json.dumps({
            "status": "ok",
            "counters": counters,
            "invalidations": invalidations,
            "timestamp": _now_utc(),
        })
    except Exception as e:
        logger.error(f"validate_causal_chains falló: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_causal_chains_stats(days_back: int = 30) -> str:
    """Estadísticas de causal chains para calibrar confidence scoring del agente.

    Returns:
        JSON con counters por status, hit_rate (validated/(validated+invalidated)),
        breakdown por instrument y timeframe, top invalidations recientes.
    """
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT status, instrument, timeframe, confidence_initial, "
            "confidence_current, invalidation_reason "
            "FROM causal_chains WHERE created_at >= datetime('now', '-' || ? || ' days')",
            (days_back,),
        ).fetchall()

        by_status: dict = {}
        by_instrument: dict = {}
        by_tf: dict = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            by_instrument.setdefault(r["instrument"], {"open": 0, "validated": 0, "invalidated": 0, "expired": 0})
            by_instrument[r["instrument"]][r["status"]] = by_instrument[r["instrument"]].get(r["status"], 0) + 1
            tf = r["timeframe"] or "unknown"
            by_tf.setdefault(tf, {"open": 0, "validated": 0, "invalidated": 0, "expired": 0})
            by_tf[tf][r["status"]] = by_tf[tf].get(r["status"], 0) + 1

        validated = by_status.get("validated", 0)
        invalidated = by_status.get("invalidated", 0)
        resolved = validated + invalidated
        hit_rate = round(validated / resolved, 2) if resolved else None

        recent_invalidations = [
            dict(r) for r in db.execute(
                "SELECT cc_id, instrument, invalidation_reason, resolved_at "
                "FROM causal_chains WHERE status='invalidated' "
                "ORDER BY resolved_at DESC LIMIT 10"
            ).fetchall()
        ]
        db.close()

        return json.dumps({
            "total": len(rows),
            "by_status": by_status,
            "by_instrument": by_instrument,
            "by_timeframe": by_tf,
            "hit_rate": hit_rate,
            "recent_invalidations": recent_invalidations,
        })
    except Exception as e:
        logger.error(f"get_causal_chains_stats falló: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def detect_regime(
    aetheer_per_pair_json: str = "{}",
    use_recent_trades: bool = True,
    days_back: int = 30,
) -> str:
    """Clasificar el régimen actual de mercado: trending|transition|ranging.

    Combina indicador Aetheer multi-TF + histórico de trades del usuario
    + priors de calendario. Devuelve recomendación operativa según régimen.

    Args:
        aetheer_per_pair_json: JSON string con {pair: {tf: aetheer_data}}.
            Ejemplo: {"EURUSD": {"60": {"ema_align":"mixed",...}}}.
            Vacío = clasificar solo por calendario + trades.
        use_recent_trades: True para incluir win_rate de los últimos N días
            como señal de régimen.
        days_back: Ventana de trades a considerar.

    Returns:
        JSON con {regime, confidence, symptoms, recommendation, calendar_bias}.
    """
    import sys as _sys
    from pathlib import Path as _Path
    _shared = _Path(__file__).resolve().parent.parent / "shared"
    if str(_shared) not in _sys.path:
        _sys.path.insert(0, str(_shared))
    from regime_detector import detect_regime as _detect

    try:
        aetheer = json.loads(aetheer_per_pair_json) if aetheer_per_pair_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid aetheer JSON: {e}"})

    stats = None
    if use_recent_trades:
        try:
            db = _get_db()
            sql = (
                "SELECT outcome, r_multiple FROM trade_log "
                "WHERE entry_time_utc >= datetime('now', '-' || ? || ' days') "
                "AND outcome IN ('win','loss','be')"
            )
            rows = [dict(r) for r in db.execute(sql, (days_back,)).fetchall()]
            db.close()

            wins = sum(1 for r in rows if r["outcome"] == "win")
            losses = sum(1 for r in rows if r["outcome"] == "loss")
            closed = len(rows)
            rs = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
            stats = {
                "closed": closed,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / closed, 2) if closed else None,
                "avg_r_multiple": round(sum(rs) / len(rs), 2) if rs else None,
            }
        except Exception as e:
            logger.warning(f"detect_regime: trade stats fetch falló: {e}")

    try:
        result = _detect(aetheer_per_pair=aetheer, recent_trades_stats=stats)
        result["trade_stats_used"] = stats
        return json.dumps(result)
    except Exception as e:
        logger.error(f"detect_regime falló: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_recent_trades(
    instrument: str = "",
    outcome: str = "",
    limit: int = 20,
    days_back: int = 30,
    include_open: bool = True,
) -> str:
    """Obtener trades recientes para informar el análisis actual.

    El context-orchestrator debe llamar esta tool antes de full_analysis
    o validate_setup para que synthesis pueda referirse a patrones recurrentes,
    rachas de loss, o contexto de trades abiertos.

    Args:
        instrument: Filtrar por símbolo (ej. "GBPUSD"). Vacío = todos.
        outcome: "win"|"loss"|"be"|"open"|"cancelled". Vacío = todos.
        limit: Máximo de filas a retornar.
        days_back: Solo trades con entry_time_utc dentro de N días.
        include_open: Incluir trades aún abiertos.

    Returns:
        JSON {trades: [...], stats: {win_rate, avg_r, total, by_instrument}}
    """
    try:
        db = _get_db()
        where = ["entry_time_utc >= datetime('now', '-' || ? || ' days')"]
        params: list = [days_back]
        if instrument:
            where.append("instrument = ?")
            params.append(instrument.upper())
        if outcome:
            where.append("outcome = ?")
            params.append(outcome)
        elif not include_open:
            where.append("outcome != 'open'")

        sql = (
            "SELECT * FROM trade_log WHERE " + " AND ".join(where) +
            " ORDER BY entry_time_utc DESC LIMIT ?"
        )
        params.append(min(limit, 100))
        rows = [dict(r) for r in db.execute(sql, params).fetchall()]

        # Deserializar market_context_json para consumo directo
        for r in rows:
            mctx = r.get("market_context_json")
            if mctx:
                try:
                    r["market_context"] = json.loads(mctx)
                except (json.JSONDecodeError, TypeError):
                    pass

        # Stats agregadas (solo trades cerrados)
        closed = [r for r in rows if r.get("outcome") in ("win", "loss", "be")]
        wins = [r for r in closed if r["outcome"] == "win"]
        losses = [r for r in closed if r["outcome"] == "loss"]
        rs = [r["r_multiple"] for r in closed if r.get("r_multiple") is not None]
        by_inst: dict = {}
        for r in closed:
            inst = r["instrument"]
            by_inst.setdefault(inst, {"total": 0, "wins": 0, "losses": 0})
            by_inst[inst]["total"] += 1
            if r["outcome"] == "win":
                by_inst[inst]["wins"] += 1
            elif r["outcome"] == "loss":
                by_inst[inst]["losses"] += 1

        stats = {
            "total_returned": len(rows),
            "closed": len(closed),
            "open": sum(1 for r in rows if r.get("outcome") == "open"),
            "win_rate": round(len(wins) / len(closed), 2) if closed else None,
            "avg_r_multiple": round(sum(rs) / len(rs), 2) if rs else None,
            "wins": len(wins),
            "losses": len(losses),
            "by_instrument": by_inst,
        }

        db.close()
        return json.dumps({"trades": rows, "stats": stats})
    except Exception as e:
        logger.error(f"get_recent_trades falló: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def store_trajectory(trajectory_json: str) -> str:
    """Persistir una trayectoria de análisis para retrieval semántico futuro.

    Una trayectoria captura un análisis completo: query → MCP data → causal
    chains → quality → routing → feedback. Permite que el router aprenda
    qué modelos funcionaron en casos similares.

    Política de persistencia (D-trayectoria):
      - approved=True → se guarda.
      - approved=False por OFFLINE/KILL_SWITCH/BUDGET → se guarda (diagnóstico).
      - approved=False por quality_score_global < floor → NO se guarda
        (ruido — synthesis ni siquiera corrió).

    Args:
        trajectory_json: JSON con shape AnalysisTrajectory:
            { "trace_id": str,
              "query": {CognitiveQuery dict},
              "response": {CognitiveResponse dict},
              "mcp_data_snapshot": {...},
              "model_routing": {agent_name: {model_id, cost_usd, latency_ms}},
              "user_feedback": "positive"|"negative"|"mixed"|"none" }

    Returns:
        JSON {status, trace_id, id} en éxito; {error, ...} en fallo.
        Si la trayectoria fue rechazada por política, status="skipped".
    """
    try:
        payload = json.loads(trajectory_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    try:
        trajectory = AnalysisTrajectory.model_validate(payload)
    except Exception as e:
        return json.dumps({"error": f"Schema validation failed: {e}"})

    store = _trajectory_store()
    if not store.should_persist(trajectory.response):
        return json.dumps({
            "status": "skipped",
            "trace_id": trajectory.trace_id,
            "reason": "quality_floor_rejection",
        })

    try:
        traj_id = await store.store(trajectory)
        return json.dumps({
            "status": "ok",
            "id": traj_id,
            "trace_id": trajectory.trace_id,
        })
    except Exception as e:
        logger.error(f"store_trajectory failed: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def retrieve_similar(
    query_json: str,
    k: int = 5,
    min_quality: float = 0.70,
    min_similarity: float = 0.30,
    only_approved: bool = True,
    same_intent: bool = True,
) -> str:
    """Recuperar casos similares a la query actual via cosine similarity.

    Útil para que el orquestador y el router consulten "¿qué pasó la última
    vez que se preguntó algo así?" y ajusten priors (modelo a usar, peso
    de cada agente, expected quality).

    Args:
        query_json: JSON con campos de CognitiveQuery (al menos query_intent
            y query_text; instruments/timeframes ayudan al filtro).
        k: máximo de casos a devolver.
        min_quality: floor de quality_score_global del caso recuperado.
        min_similarity: umbral mínimo de cosine [0..1].
        only_approved: si True, solo casos approved=True.
        same_intent: si True, restringe al mismo query_intent (recomendado).

    Returns:
        JSON {results: [{similarity, trajectory: {...}}], count}.
    """
    try:
        query = json.loads(query_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    if not isinstance(query, dict) or "query_intent" not in query:
        return json.dumps({"error": "query_json must contain query_intent"})

    try:
        cases = await _trajectory_store().retrieve_similar(
            query,
            k=k,
            min_quality=min_quality,
            min_similarity=min_similarity,
            only_approved=only_approved,
            same_intent=same_intent,
        )
        return json.dumps({
            "count": len(cases),
            "results": [c.model_dump() for c in cases],
        })
    except Exception as e:
        logger.error(f"retrieve_similar failed: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def update_trajectory_feedback(trace_id: str, feedback: str) -> str:
    """Actualizar el `user_feedback` de una trayectoria existente.

    Args:
        trace_id: identificador de la trayectoria.
        feedback: "positive"|"negative"|"mixed"|"none".

    Returns:
        JSON {status, trace_id, updated}.
    """
    if feedback not in ("positive", "negative", "mixed", "none"):
        return json.dumps({"error": f"Invalid feedback: {feedback}"})
    try:
        updated = _trajectory_store().update_feedback(trace_id, feedback)  # type: ignore[arg-type]
        return json.dumps({
            "status": "ok" if updated else "not_found",
            "trace_id": trace_id,
            "updated": updated,
        })
    except Exception as e:
        logger.error(f"update_trajectory_feedback failed: {e}")
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
