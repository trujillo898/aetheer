#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "=== AETHEER v3 BOOTSTRAP ==="
echo "[1/6] Verificando Python..."
python3 --version

echo "[2/6] Recreando entorno virtual (.venv)..."
if [[ -d ".venv" ]]; then
  rm -rf .venv
fi
python3 -m venv .venv
source .venv/bin/activate

echo "[3/6] Instalando dependencias..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[4/6] Inicializando DB base..."
mkdir -p db logs
if [[ ! -f "db/aetheer.db" ]]; then
  sqlite3 db/aetheer.db < mcp-servers/memory/schema.sql
fi

echo "[5/6] Ejecutando migraciones..."
python db/migrations/run_migrations.py

echo "[6/6] Smoke tests..."
AETHEER_EMBEDDING_STUB=1 python -m pytest tests/ -v

echo ""
echo "Bootstrap v3 completado."
echo "Siguiente paso: copiar .env.example -> .env y configurar OPENROUTER_API_KEY."
