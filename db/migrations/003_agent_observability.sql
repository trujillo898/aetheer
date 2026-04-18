-- ============================================================================
-- AETHEER MIGRATION 003
-- Fecha: 2026-04-19
-- Propósito: Agregar columnas de observabilidad a agent_outputs
--            (Requerido por Governor Agent y archivado de memoria)
-- ============================================================================

BEGIN TRANSACTION;

-- 1. Columnas faltantes en agent_outputs
ALTER TABLE agent_outputs ADD COLUMN output_hash TEXT;
ALTER TABLE agent_outputs ADD COLUMN operating_mode TEXT DEFAULT 'UNKNOWN';
ALTER TABLE agent_outputs ADD COLUMN quality_score REAL;

-- 2. Índices para deduplicación y filtrado por calidad
CREATE INDEX IF NOT EXISTS idx_agent_outputs_hash ON agent_outputs(output_hash);
CREATE INDEX IF NOT EXISTS idx_agent_outputs_quality ON agent_outputs(quality_score);
CREATE INDEX IF NOT EXISTS idx_agent_outputs_mode ON agent_outputs(operating_mode);

COMMIT;