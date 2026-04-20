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

# Verificar MCP servers (D010 — 3 servers)
echo "[MCP SERVERS]"
for server in tv-unified macro-data memory; do
    if [ -f "mcp-servers/$server/server.py" ]; then
        echo "  [OK] $server"
    else
        echo "  [FAIL] $server"
    fi
done

echo ""

# Verificar agentes (incluye governor)
echo "[AGENTS]"
for agent in liquidity events price-behavior macro context-orchestrator synthesis governor; do
    if [ -f ".claude/agents/$agent.md" ]; then
        echo "  [OK] $agent"
    else
        echo "  [FAIL] $agent"
    fi
done

echo ""

# Verificar TV cache
echo "[TV CACHE]"
if [ -f "db/tv_cache.sqlite" ]; then
    echo "  [OK] tv_cache.sqlite presente ($(du -h db/tv_cache.sqlite | cut -f1))"
    echo "  Entries: $(sqlite3 db/tv_cache.sqlite 'SELECT COUNT(*) FROM snapshots;' 2>/dev/null || echo 'N/A')"
else
    echo "  [--] tv_cache.sqlite no existe (se creará al primer uso)"
fi

echo ""

# Verificar TradingView Desktop CDP (fuente única — D010)
echo "[TRADINGVIEW CDP — D010]"
CDP_PORT=${TV_CDP_PORT:-9222}
if curl -s -m 2 "http://127.0.0.1:${CDP_PORT}/json/version" > /dev/null 2>&1; then
    echo "  [OK] TradingView Desktop CDP activo en puerto ${CDP_PORT}"
else
    echo "  [FAIL] TradingView Desktop NO responde en puerto ${CDP_PORT}"
    echo "  Para activar: lanzar TV Desktop con --remote-debugging-port=${CDP_PORT}"
    echo "  Monitor automático: scripts/tv-health-monitor.py"
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
