-- Migration 002: Add deep_snapshots table for multi-timeframe data
-- Date: 2026-04-14
-- Required by: D008 Multi-Timeframe reading

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
