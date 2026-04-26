#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[DEPRECATED] scripts/bootstrap.sh ahora delega a scripts/bootstrap_v3.sh"
exec bash scripts/bootstrap_v3.sh
