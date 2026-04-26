-- ============================================================================
-- AETHEER MIGRATION 006
-- Fecha: 2026-04-25
-- Propósito: Persistir consentimiento explícito del trader para acciones
--            con side-effect sobre TradingView Desktop (Fase 3 — cdp_drawing).
--            Sin un registro `granted=1` para el feature correspondiente,
--            TradingViewCDPDrawer rechaza cualquier draw_* con
--            ConsentRequiredError.
-- ============================================================================

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS user_consents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature TEXT NOT NULL,                   -- 'cdp_drawing', futuros: 'cdp_alerts', etc.
    granted INTEGER NOT NULL DEFAULT 0,      -- 0|1 (boolean)
    granted_at TEXT,                         -- ISO timestamp del grant
    revoked_at TEXT,                         -- ISO timestamp del revoke (si aplica)
    note TEXT,                               -- contexto opcional ("aceptado vía CLI", etc.)
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_consents_feature
    ON user_consents(feature);

COMMIT;
