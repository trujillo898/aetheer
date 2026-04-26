#!/usr/bin/env bash
set -euo pipefail

START_TS="$(date +%s)"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "=== AETHEER ROLLBACK DRILL (v3 -> v1.2/main) ==="

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[FAIL] Este script debe ejecutarse dentro del repositorio git."
  exit 1
fi

BACKUP_FILE="${1:-}"
if [[ -z "${BACKUP_FILE}" ]]; then
  BACKUP_FILE="$(ls -1t "$ROOT_DIR"/../aetheer-backup-v1.2-*.tar.gz 2>/dev/null | head -n1 || true)"
fi

if [[ -z "${BACKUP_FILE}" || ! -f "${BACKUP_FILE}" ]]; then
  echo "[FAIL] Backup no encontrado. Pasa la ruta como argumento:"
  echo "       bash scripts/test_rollback_to_v2.sh /ruta/aetheer-backup-v1.2-*.tar.gz"
  exit 1
fi

echo "[1/5] Checkout a main..."
git checkout main

echo "[2/5] Restaurando backup en /tmp para verificacion..."
RESTORE_DIR="/tmp/aetheer-rollback-$(date +%Y%m%d-%H%M%S)"
mkdir -p "${RESTORE_DIR}"
tar -xzf "${BACKUP_FILE}" -C "${RESTORE_DIR}"

echo "[3/5] Verificando artefactos minimos del backup..."
if ! find "${RESTORE_DIR}" -maxdepth 3 -name "CLAUDE.md" | grep -q .; then
  echo "[FAIL] El backup restaurado no contiene estructura esperada (CLAUDE.md)."
  exit 1
fi

echo "[4/5] Smoke test en main..."
AETHEER_EMBEDDING_STUB=1 python3 -m pytest \
  tests/test_cost_monitor.py \
  tests/test_model_router.py \
  tests/test_openrouter_client.py -v

echo "[5/5] Validacion de tiempo..."
END_TS="$(date +%s)"
ELAPSED="$((END_TS - START_TS))"
echo "Tiempo total: ${ELAPSED}s"
if [[ "${ELAPSED}" -gt 900 ]]; then
  echo "[FAIL] Rollback drill excedio 15 minutos."
  exit 1
fi

echo "Rollback drill OK. Repo funcional en branch main."
echo "Backup verificado en: ${RESTORE_DIR}"
