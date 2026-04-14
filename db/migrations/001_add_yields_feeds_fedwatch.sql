-- Migration 001: Add yields_history, feed_status, fedwatch_history
-- Date: 2026-04-13
-- Dependency: none (additive to existing schema)
-- Required by: FRED fix, RSS fix, FedWatch fix, Bund/Gilt fallback

-- ============================================================
-- yields_history: almacena yields de bonos con timestamp
-- Permite fallback a último dato conocido cuando fuente no disponible
-- ============================================================
CREATE TABLE IF NOT EXISTS yields_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bond TEXT NOT NULL,           -- 'us_10y', 'us_02y', 'de_10y', 'gb_10y'
    yield_pct REAL NOT NULL,
    source TEXT NOT NULL,         -- 'tradingview', 'fred', 'yahoo', 'tradingeconomics'
    quality_score REAL DEFAULT 1.0,
    timestamp_utc TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_yields_bond_ts
    ON yields_history(bond, timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_yields_bond_latest
    ON yields_history(bond, created_at DESC);

-- ============================================================
-- feed_status: monitoreo de salud de feeds RSS y scrapers
-- Permite detectar feeds muertos y activar fallbacks
-- ============================================================
CREATE TABLE IF NOT EXISTS feed_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_name TEXT NOT NULL UNIQUE, -- 'reuters_business', 'reuters_world', etc.
    feed_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',  -- 'active', 'dead', 'degraded', 'unknown'
    last_success_utc TEXT,
    last_failure_utc TEXT,
    last_error TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    articles_last_24h INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feed_status_name
    ON feed_status(feed_name);

-- ============================================================
-- fedwatch_history: historial de probabilidades FedWatch
-- Permite mostrar última lectura conocida cuando CME bloquea
-- ============================================================
CREATE TABLE IF NOT EXISTS fedwatch_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date TEXT,               -- fecha del FOMC meeting
    prob_hold REAL,                  -- probabilidad de hold (%)
    prob_cut_25 REAL,                -- probabilidad de cut 25bp (%)
    prob_cut_50 REAL,                -- probabilidad de cut 50bp (%)
    prob_hike_25 REAL,               -- probabilidad de hike 25bp (%)
    current_rate TEXT,               -- tasa actual (e.g. "5.25-5.50")
    source TEXT NOT NULL,            -- 'cme_scrape', 'manual', 'alternative'
    raw_data TEXT,                   -- JSON con datos adicionales
    timestamp_utc TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fedwatch_ts
    ON fedwatch_history(timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_fedwatch_meeting
    ON fedwatch_history(meeting_date, timestamp_utc DESC);
