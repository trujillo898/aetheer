#!/bin/bash
set -e

echo "=== AETHEER — Instalación de dependencias ==="

python3 --version || { echo "[ERROR] Python 3 no encontrado"; exit 1; }

echo "[DEPS] Instalando dependencias globales..."
pip3 install -r requirements.txt

echo "[DEPS] Verificando imports..."
python3 -c "import httpx; print(f'  httpx {httpx.__version__}')"
python3 -c "import feedparser; print(f'  feedparser {feedparser.__version__}')"
python3 -c "import dateutil; print('  python-dateutil OK')"
python3 -c "import yaml; print(f'  pyyaml {yaml.__version__}')"
python3 -c "import mcp; print('  mcp OK')"
python3 -c "import bs4; print(f'  beautifulsoup4 {bs4.__version__}')"
python3 -c "import lxml; print('  lxml OK')"

echo ""
echo "=== Dependencias instaladas correctamente ==="
