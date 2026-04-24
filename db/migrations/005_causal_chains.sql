-- ============================================================================
-- AETHEER MIGRATION 005
-- Fecha: 2026-04-22
-- Propósito: Persistir causal chains generadas por price-behavior y validarlas
--            en el tiempo (loop horario). Permite calibrar confidence scoring
--            con feedback real: chains que se validan ganan peso, chains que
--            se invalidan pierden peso y alimentan post-mortems.
-- ============================================================================

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS causal_chains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cc_id TEXT,                              -- "CC-001" del agente price-behavior
    instrument TEXT NOT NULL,
    timeframe TEXT,
    cause TEXT NOT NULL,
    effect TEXT NOT NULL,
    invalid_condition TEXT NOT NULL,
    -- Trigger estructurado (opcional) — permite validación automática
    -- JSON con shape: {"type":"price_break","level":1.35100,"side":"below","instrument":"GBPUSD"}
    -- Tipos soportados: price_break, ema_cross, atr_threshold, manual_only
    trigger_struct_json TEXT,
    confidence_initial REAL NOT NULL,
    confidence_current REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','validated','invalidated','expired'))
            DEFAULT 'open',
    -- Metadata
    market_state_at_creation_json TEXT,      -- snapshot Aetheer al crear
    market_state_at_resolution_json TEXT,    -- snapshot al validar/invalidar
    invalidation_reason TEXT,
    -- Timing
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,                -- default created + decay window (~14d)
    resolved_at TEXT,
    last_checked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cc_status ON causal_chains(status);
CREATE INDEX IF NOT EXISTS idx_cc_instrument ON causal_chains(instrument);
CREATE INDEX IF NOT EXISTS idx_cc_expires ON causal_chains(expires_at);

COMMIT;
