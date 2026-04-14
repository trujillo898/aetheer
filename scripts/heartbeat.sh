#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
if [ -f ".env" ]; then
  export $(grep -v '^#' .env | xargs)
fi
echo "=== AETHEER HEARTBEAT ==="
echo "Timestamp: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo ""

# Verificar DB
if [ -f "db/aetheer.db" ]; then
    echo "[DB] aetheer.db presente"
    echo "[DB] Tamaño: $(du -h db/aetheer.db | cut -f1)"
    echo "[DB] Snapshots: $(sqlite3 db/aetheer.db 'SELECT COUNT(*) FROM price_snapshots;' 2>/dev/null || echo 'N/A')"
    echo "[DB] Eventos: $(sqlite3 db/aetheer.db 'SELECT COUNT(*) FROM events;' 2>/dev/null || echo 'N/A')"
    echo "[DB] Eventos preloaded: $(sqlite3 db/aetheer.db "SELECT COUNT(*) FROM events WHERE source='preloaded';" 2>/dev/null || echo 'N/A')"
    echo "[DB] Memoria: $(sqlite3 db/aetheer.db 'SELECT COUNT(*) FROM context_memory;' 2>/dev/null || echo 'N/A')"
    echo "[DB] Items obsoletos: $(sqlite3 db/aetheer.db 'SELECT COUNT(*) FROM context_memory WHERE relevance_current < 0.05;' 2>/dev/null || echo 'N/A')"
else
    echo "[DB] aetheer.db NO encontrada"
fi

echo ""

# Verificar MCP servers
echo "[MCP SERVERS]"
for server in price-feed economic-calendar macro-data news-feed memory; do
    if [ -f "mcp-servers/$server/server.py" ]; then
        echo "  [OK] $server"
    else
        echo "  [FAIL] $server"
    fi
done

echo ""

# Verificar agentes
echo "[AGENTS]"
for agent in liquidity events price-behavior macro context-orchestrator synthesis; do
    if [ -f ".claude/agents/$agent.md" ]; then
        echo "  [OK] $agent"
    else
        echo "  [FAIL] $agent"
    fi
done

echo ""

# Verificar fuentes de precio API
echo "[PRICE SOURCES]"
if [ -n "$ALPHA_VANTAGE_API_KEY" ] && [ "$ALPHA_VANTAGE_API_KEY" != "" ]; then
    echo "  [OK] Alpha Vantage — key configurada"
else
    echo "  [--] Alpha Vantage — sin key (usando scraping fallback)"
fi

echo ""

# Verificar TradingView MCP
echo "[TRADINGVIEW MCP]"
TV_SERVER="$HOME/tradingview-mcp/src/server.js"
TV_CLI="$HOME/tradingview-mcp/src/cli/index.js"
if [ ! -f "$TV_CLI" ]; then
    echo "  TradingView MCP: not installed"
    echo "  Para instalar: git clone https://github.com/tradesdontlie/tradingview-mcp ~/tradingview-mcp && cd ~/tradingview-mcp && npm install"
else
    TV_RESULT=$(timeout 5 node "$TV_CLI" status 2>/dev/null)
    TV_SUCCESS=$(echo "$TV_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('success',''))" 2>/dev/null)
    if [ "$TV_SUCCESS" = "True" ]; then
        TV_SYMBOL=$(echo "$TV_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('chart_symbol','unknown'))" 2>/dev/null)
        echo "  TradingView MCP: connected (primary data source) — símbolo activo: $TV_SYMBOL"
    else
        echo "  TradingView MCP: not available (using API fallback)"
        echo "  Para activar: lanzar TradingView Desktop con --remote-debugging-port=9222"
    fi
fi

echo ""

# Kill Switch status
echo "[KILL SWITCH]"
echo "  Último test: $(python3 -c "
import os
log_path = 'logs/kill-switch-test.log'
if os.path.exists(log_path):
    with open(log_path) as f:
        lines = f.readlines()
        print(lines[-1].strip() if lines else 'Sin registro')
else:
    print('No ejecutado aún. Corre: python3 scripts/test-kill-switch.py')
" 2>/dev/null || echo "No disponible")"

echo ""

# Calendar preload status
echo "[CALENDAR PRELOAD]"
echo "  Última pre-carga: $(sqlite3 db/aetheer.db "SELECT MAX(preloaded_at) FROM events WHERE preloaded_at IS NOT NULL;" 2>/dev/null || echo 'Nunca')"

echo ""
echo "=== FIN HEARTBEAT ==="
