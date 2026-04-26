# MIGRATION GUIDE v1.2 -> v3.0

Este documento describe el camino operativo para pasar de v1.2 a v3.0 en la rama `feature/v3-hybrid-rewrite`.

## 1. Verificar backup existente (Fase 0)

Confirma que el backup generado exista y sea legible:

```bash
ls -lh ../aetheer-backup-v1.2-*.tar.gz
```

Referencia esperada: `aetheer-backup-v1.2-*.tar.gz`.

## 2. Checkout de rama v3

```bash
git fetch --all --prune
git checkout feature/v3-hybrid-rewrite
```

## 3. Recrear entorno virtual (obligatorio)

El `.venv` heredado puede contener shebangs hardcodeados a `/home/thomas/aetheer/`.
Recrear el entorno en la ubicacion actual:

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
```

## 4. Instalar dependencias

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 5. Configurar variables de entorno

```bash
cp .env.example .env
```

Completar al menos:

- `OPENROUTER_API_KEY=...`
- `TELEGRAM_BOT_TOKEN=...` (si usaras Telegram)
- `TELEGRAM_ALLOWED_CHAT_IDS=...` (coma-separado)
- `REDIS_URL=redis://localhost:6379/0` (si usaras sync real por Redis)

## 6. Smoke test de la suite

```bash
AETHEER_EMBEDDING_STUB=1 pytest tests/ -v
```

## 7. Activacion gradual por feature flags

Editar `config/feature_flags.yaml` y habilitar por capas, no todo a la vez:

1. `openrouter.use_openrouter_by_agent.<agent>=true` por agente.
2. `memory.enable_trajectory_learning=true` cuando retrieval este validado.
3. `scheduler.enabled=true` cuando jobs esten verificados.
4. `interfaces.webapp_enabled=true` y `interfaces.telegram_enabled=true`.
5. `interfaces.sync_via_redis=true` cuando Redis este estable.

## 8. Monitoreo de costos

Ejemplo rapido:

```bash
python - <<'PY'
from services.cost_monitor import CostMonitor
m = CostMonitor("db/cost_monitor.db")
print("spent_today_usd=", m.spent_today_usd())
print("by_agent=", m.spent_by_agent_today())
PY
```

## Comando recomendado (fresh setup)

Si quieres ejecutar todo en bloque:

```bash
bash scripts/bootstrap_v3.sh
```
