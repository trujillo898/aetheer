CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL,
    price REAL NOT NULL,
    source TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL,
    currency TEXT NOT NULL,
    importance TEXT NOT NULL,
    expected REAL,
    actual REAL,
    previous REAL,
    previous_revision REAL,
    surprise_direction TEXT,
    price_reaction_dxy_pct REAL,
    reaction_duration_min INTEGER,
    priced_in INTEGER DEFAULT 0,
    event_datetime_utc TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    date TEXT NOT NULL,
    avg_volatility_pips REAL,
    volume_relative REAL,
    liquidity_level TEXT,
    dominant_pattern TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS context_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT NOT NULL CHECK(layer IN ('short', 'medium', 'long')),
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    relevance_base REAL DEFAULT 1.0,
    relevance_current REAL DEFAULT 1.0,
    decay_factor REAL DEFAULT 0.95,
    access_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    last_accessed TEXT DEFAULT (datetime('now')),
    compressed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_profile (
    id INTEGER PRIMARY KEY DEFAULT 1,
    interaction_count INTEGER DEFAULT 0,
    preferred_detail_level TEXT DEFAULT 'medium',
    recurring_topics TEXT DEFAULT '[]',
    typical_query_pattern TEXT DEFAULT '',
    last_interaction TEXT,
    session_preference TEXT DEFAULT 'london_ny_overlap',
    tone_calibration TEXT DEFAULT 'institutional_direct',
    known_patterns TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    output_json TEXT NOT NULL,
    query_intent TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS heartbeat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    agents_status TEXT NOT NULL,
    sources_status TEXT NOT NULL,
    context_health TEXT NOT NULL,
    kill_switch_active INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_instrument ON price_snapshots(instrument, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_events_currency ON events(currency, event_datetime_utc);
CREATE INDEX IF NOT EXISTS idx_context_memory_layer ON context_memory(layer, category);
CREATE INDEX IF NOT EXISTS idx_context_memory_relevance ON context_memory(relevance_current);
CREATE INDEX IF NOT EXISTS idx_agent_outputs_agent ON agent_outputs(agent_name, created_at);

-- ============================================================
-- yields_history: almacena yields de bonos con timestamp
-- ============================================================
CREATE TABLE IF NOT EXISTS yields_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bond TEXT NOT NULL,
    yield_pct REAL NOT NULL,
    source TEXT NOT NULL,
    quality_score REAL DEFAULT 1.0,
    timestamp_utc TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_yields_bond_ts
    ON yields_history(bond, timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_yields_bond_latest
    ON yields_history(bond, created_at DESC);

-- ============================================================
-- feed_status: monitoreo de salud de feeds RSS
-- ============================================================
CREATE TABLE IF NOT EXISTS feed_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_name TEXT NOT NULL UNIQUE,
    feed_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
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
-- ============================================================
CREATE TABLE IF NOT EXISTS fedwatch_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date TEXT,
    prob_hold REAL,
    prob_cut_25 REAL,
    prob_cut_50 REAL,
    prob_hike_25 REAL,
    current_rate TEXT,
    source TEXT NOT NULL,
    raw_data TEXT,
    timestamp_utc TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fedwatch_ts
    ON fedwatch_history(timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_fedwatch_meeting
    ON fedwatch_history(meeting_date, timestamp_utc DESC);

-- ============================================================
-- deep_snapshots: datos multi-timeframe del indicador Aetheer (D008)
-- ============================================================
CREATE TABLE IF NOT EXISTS deep_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,            -- 'DXY', 'EURUSD', 'GBPUSD'
    timeframe TEXT NOT NULL,         -- 'D1', 'H4', 'H1', 'M15'
    ohlcv_json TEXT,                 -- OHLCV summary como JSON
    aetheer_data_json TEXT,          -- Datos del indicador Aetheer como JSON
    native_indicators_json TEXT,     -- Indicadores nativos como JSON
    source TEXT DEFAULT 'tv_deep_read',
    timestamp_utc TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_deep_symbol_tf_time
    ON deep_snapshots(symbol, timeframe, timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_deep_latest
    ON deep_snapshots(symbol, timeframe, created_at DESC);

-- Migration tracking
CREATE TABLE IF NOT EXISTS _migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL UNIQUE,
    applied_at TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- trajectories: análisis completos para retrieval semántico
-- (no reemplaza context_memory; lo extiende con casos completos
-- query → mcp_data → causal chains → quality → feedback)
-- ============================================================
CREATE TABLE IF NOT EXISTS trajectories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL UNIQUE,
    query_intent TEXT NOT NULL,
    instruments_csv TEXT NOT NULL DEFAULT '',  -- 'EURUSD,GBPUSD' para filtros baratos
    query_json TEXT NOT NULL,                  -- CognitiveQuery completo
    response_json TEXT NOT NULL,               -- CognitiveResponse completo
    mcp_data_json TEXT NOT NULL DEFAULT '{}',  -- snapshot de tv-unified
    routing_json TEXT NOT NULL DEFAULT '{}',   -- {agent: {model_id, cost_usd, latency_ms}}
    approved INTEGER NOT NULL DEFAULT 0,       -- 0|1
    operating_mode TEXT NOT NULL,              -- ONLINE|OFFLINE
    quality_score REAL NOT NULL DEFAULT 0.0,
    user_feedback TEXT NOT NULL DEFAULT 'none' -- positive|negative|mixed|none
        CHECK(user_feedback IN ('positive','negative','mixed','none')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trajectories_intent
    ON trajectories(query_intent, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trajectories_quality
    ON trajectories(quality_score DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trajectories_approved
    ON trajectories(approved, operating_mode);

-- Embeddings se guardan en tabla separada porque:
-- 1) suelen ser BLOBs grandes (1536 floats = 6KB+ por fila)
-- 2) el vector model puede cambiar (versionado)
-- 3) facilita rebuilds sin tocar la trayectoria principal
CREATE TABLE IF NOT EXISTS trajectory_embeddings (
    trajectory_id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,                       -- 'text-embedding-3-small' | 'stub-hash-v1'
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,                      -- float32 little-endian, length = dim*4
    norm REAL NOT NULL,                        -- precomputed L2 norm para cosine rápido
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (trajectory_id) REFERENCES trajectories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trajectory_embeddings_model
    ON trajectory_embeddings(model);
