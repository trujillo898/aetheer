# OPERATIONS RUNBOOK (v3.0)

Guia operativa para incidentes en produccion/local-run de Aetheer.

## Checklist de primer nivel

1. Validar estado TV: `python3 scripts/tv-health-monitor.py`
2. Validar modo operativo: `mcp__tv-unified__get_system_health`
3. Revisar feature flags: `config/feature_flags.yaml`
4. Revisar costo diario: `services.cost_monitor.CostMonitor`
5. Confirmar scheduler: `scheduler.enabled` y `AETHEER_SCHEDULE_*`

## Escenarios de error (6)

### 1) `tv_fully_unavailable` (Kill Switch)

Sintoma:
- `operating_mode=OFFLINE`
- respuesta estructurada `ANALYSIS_UNAVAILABLE`

Acciones:
1. Confirmar `get_system_health`:
   `cdp_connected=false`, `news_api_ok=false`, `calendar_api_ok=false`, `cache_fallback_available=false`.
2. Verificar TradingView Desktop con puerto `9222`.
3. Reiniciar TV Desktop y ejecutar `scripts/tv-health-monitor.py`.
4. Reintentar una consulta puntual.

Criterio de salida:
- `operating_mode=ONLINE` y `/api/health` estable.

### 2) `tv-unified_timeout` (recuperacion por cache)

Sintoma:
- timeouts intermitentes en herramientas TV.
- eventos con `meta.stale=true`.

Acciones:
1. No forzar fallback externo: mantener regla D013.
2. Permitir respuesta desde cache (hasta 30 min) marcada `(cache N min)`.
3. Monitorear `tv-unified.stale_cache_served_count`.
4. Si supera umbral operativo, tratarlo como incidente de infraestructura TV.

Criterio de salida:
- disminuyen lecturas stale y vuelven datos `source=tradingview_cdp`.

### 3) `online_to_offline_on_quality_drop`

Sintoma:
- `quality_score_global < 0.60`.
- respuesta OFFLINE aunque TV este vivo.

Acciones:
1. Revisar breakdown de calidad (freshness/completeness/consistency/source/aetheer_validity).
2. Identificar el factor dominante de degradacion.
3. Ajustar input (instrumentos/timeframes) o resolver contradicciones de agentes.
4. Repetir analisis cuando la señal vuelva a >= 0.60.

Criterio de salida:
- `quality_score_global >= 0.60` y `approved=true`.

### 4) `memory_write_failure`

Sintoma:
- errores de escritura en memory store/trajectory.

Acciones:
1. Loggear incidente (no bloquear respuesta al usuario).
2. Verificar integridad de `db/aetheer.db` y permisos de filesystem.
3. Ejecutar alerta si `memory.write_failures > 5/hour`.
4. Rehabilitar escritura cuando se normalice IO/DB.

Criterio de salida:
- los errores se reducen a baseline y la cola de writes drena.

### 5) `governor_timeout`

Sintoma:
- timeout o error de llamada a governor.

Acciones:
1. Confirmar budget y disponibilidad del modelo governor.
2. Aplicar fallback documentado:
   - respuesta conservadora (`approved=false`) o
   - aprobacion condicional de baja certeza (si tu politica operativa lo habilita).
3. Registrar incidente con `trace_id` para post-mortem.

Criterio de salida:
- governor vuelve a responder consistentemente.

### 6) `cache_stale_transparency` (TV flicker)

Sintoma:
- datos servidos desde cache por flicker CDP.

Acciones:
1. Verificar que la salida final marque explicitamente `(cache N min)`.
2. Confirmar que `source` sea `tradingview_cdp_stale`.
3. No ocultar degradacion al usuario.
4. Si la edad de cache supera 30 min, activar OFFLINE total.

Criterio de salida:
- transparencia mantenida; no hay datos stale sin marca.

## Comandos utiles

```bash
# Salud TV
python3 scripts/tv-health-monitor.py

# Costo diario
python - <<'PY'
from services.cost_monitor import CostMonitor
m = CostMonitor("db/cost_monitor.db")
print(m.spent_today_usd(), m.spent_by_agent_today())
PY

# Scheduler
pytest tests/test_scheduler.py -v
```

## Politica de merge a `main`

- Requiere tests verdes.
- Requiere aprobacion explicita.
- No auto-merge para release v3.0.
