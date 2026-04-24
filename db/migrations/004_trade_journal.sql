-- ============================================================================
-- AETHEER MIGRATION 004
-- Fecha: 2026-04-22
-- Propósito: Trade journal con outcome loop. Permite que Aetheer aprenda
--            de las operaciones reales del usuario (entry, exit, R:R, contexto
--            de mercado en el momento del trade) y use ese histórico al
--            calibrar futuros análisis.
-- ============================================================================

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Identificación
    instrument TEXT NOT NULL,              -- 'EURUSD', 'GBPUSD', 'DXY', ...
    direction TEXT NOT NULL CHECK(direction IN ('long', 'short')),
    -- Niveles del trade
    entry_price REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    exit_price REAL,                       -- NULL hasta que se cierre
    -- Tamaño y riesgo
    risk_pct REAL,                         -- ej. 0.5 (0.5% de cuenta)
    size_units REAL,                       -- opcional
    -- Outcome (poblado al cerrar)
    outcome TEXT CHECK(outcome IN ('win', 'loss', 'be', 'open', 'cancelled')) DEFAULT 'open',
    pips REAL,                             -- + para win, - para loss
    r_multiple REAL,                       -- ej. +2.0R, -1.0R
    duration_minutes INTEGER,              -- desde entry hasta exit
    -- Contexto en el momento del entry (snapshot del análisis Aetheer)
    -- JSON con: regime, session, ema_align, price_phase, rsi_div, dxy_bias, event_risk_24h
    market_context_json TEXT,
    -- Tesis del trader (texto libre, opcional)
    thesis TEXT,
    -- Etiquetas (CSV de tags, ej. "breakout,london_open,sweep")
    tags TEXT,
    -- Razón de cierre (texto libre o predefinido: 'tp_hit','sl_hit','manual','time_stop','reverse_signal')
    exit_reason TEXT,
    -- Lecciones / notas post-trade
    post_mortem TEXT,
    -- Timestamps
    entry_time_utc TEXT NOT NULL,
    exit_time_utc TEXT,                    -- NULL hasta que se cierre
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trade_log_instrument ON trade_log(instrument);
CREATE INDEX IF NOT EXISTS idx_trade_log_outcome ON trade_log(outcome);
CREATE INDEX IF NOT EXISTS idx_trade_log_entry_time ON trade_log(entry_time_utc);

-- Trigger: auto-actualizar updated_at en cada UPDATE
CREATE TRIGGER IF NOT EXISTS trg_trade_log_updated
AFTER UPDATE ON trade_log
FOR EACH ROW
BEGIN
    UPDATE trade_log SET updated_at = datetime('now') WHERE id = OLD.id;
END;

COMMIT;
