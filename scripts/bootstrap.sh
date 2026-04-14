#!/bin/bash
set -e
cd "$(dirname "$0")/.." || exit 1
if [ -f ".env" ]; then
  export $(grep -v '^#' .env | xargs)
fi

echo "=== AETHEER BOOTSTRAP ==="
echo "[BOOT] Verificando entorno..."

# Verificar Python
python3 --version || { echo "[ERROR] Python 3 no encontrado"; exit 1; }

# Crear/activar venv si no existe
if [ ! -d ".venv" ]; then
    echo "[BOOT] Creando entorno virtual..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# Instalar dependencias
echo "[BOOT] Instalando dependencias..."
pip install -r requirements.txt

# Crear directorio de logs si no existe
mkdir -p logs

# Crear base de datos
echo "[BOOT] Inicializando base de datos..."
sqlite3 db/aetheer.db < mcp-servers/memory/schema.sql

# Migrar columnas de events (nuevas columnas para pre-carga)
echo "[BOOT] Migrando esquema de eventos..."
sqlite3 db/aetheer.db "ALTER TABLE events ADD COLUMN source TEXT DEFAULT 'live';" 2>/dev/null || true
sqlite3 db/aetheer.db "ALTER TABLE events ADD COLUMN result_status TEXT DEFAULT 'pending';" 2>/dev/null || true
sqlite3 db/aetheer.db "ALTER TABLE events ADD COLUMN preloaded_at TEXT;" 2>/dev/null || true

# Insertar perfil de usuario por defecto
sqlite3 db/aetheer.db "INSERT OR IGNORE INTO user_profile (id) VALUES (1);"

# Verificar MCP servers
echo "[BOOT] Verificando MCP servers..."
for server in price-feed economic-calendar macro-data news-feed memory; do
    if [ -f "mcp-servers/$server/server.py" ]; then
        echo "  [OK] $server"
    else
        echo "  [FAIL] $server — archivo no encontrado"
        exit 1
    fi
done

echo "[BOOT] Verificando agentes..."
for agent in liquidity events price-behavior macro context-orchestrator synthesis; do
    if [ -f ".claude/agents/$agent.md" ]; then
        echo "  [OK] $agent"
    else
        echo "  [FAIL] $agent — archivo no encontrado"
        exit 1
    fi
done

# Verificar módulos nuevos
echo "[BOOT] Verificando módulos..."
for module in mcp-servers/shared/cache.py mcp-servers/shared/tv_availability.py mcp-servers/price-feed/alpha_vantage.py; do
    if [ -f "$module" ]; then
        echo "  [OK] $module"
    else
        echo "  [WARN] $module — no encontrado"
    fi
done

# Verificar TradingView MCP
echo "[BOOT] Verificando TradingView MCP..."
if [ -d "$HOME/tradingview-mcp" ] && [ -f "$HOME/tradingview-mcp/package.json" ]; then
    echo "  [OK] tradingview-mcp encontrado en ~/tradingview-mcp"
    if [ -d "$HOME/tradingview-mcp/node_modules" ]; then
        echo "  [OK] dependencias npm instaladas"
    else
        echo "  [WARN] Ejecutar: cd ~/tradingview-mcp && npm install"
    fi
else
    echo "  [INFO] tradingview-mcp no instalado. Para instalar:"
    echo "         git clone https://github.com/tradesdontlie/tradingview-mcp.git ~/tradingview-mcp"
    echo "         cd ~/tradingview-mcp && npm install"
fi

# Ejecutar test del Kill Switch
echo ""
echo "[BOOT] Ejecutando test del Kill Switch..."
python3 scripts/test-kill-switch.py
if [ $? -ne 0 ]; then
    echo "[WARN] Kill Switch test falló. Revisa antes de operar."
else
    echo "[BOOT] Kill Switch verificado"
fi

# Pre-cargar calendario económico
echo ""
echo "[BOOT] Pre-cargando calendario económico..."
python3 scripts/preload-calendar.py || echo "[WARN] Pre-carga de calendario falló. Se usará scraping en vivo."

echo ""
echo "[CRON] Para pre-cargar el calendario automáticamente cada 6 horas:"
echo "  Ejecuta: crontab -e"
echo "  Agrega: 0 */6 * * * cd ~/aetheer && .venv/bin/python scripts/preload-calendar.py >> logs/calendar-preload.log 2>&1"
echo ""

echo "=== AETHEER OPERATIVO ==="
echo "Ejecuta 'claude' desde este directorio para iniciar."
