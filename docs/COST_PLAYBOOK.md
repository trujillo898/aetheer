# COST PLAYBOOK (OpenRouter + Routing v3)

Objetivo: controlar gasto sin romper calidad de analisis.

## 1. Dials de costo disponibles

1. `services/cost_monitor.BudgetConfig`
- `daily_cap_usd`: corte duro de presupuesto.
- `soft_threshold_pct`: desde aqui se fuerza `prefer_cheap`.
- `alert_threshold_pct`: alerta operativa.

2. `agents/model_router.AetheerModelRouter`
- `prefer_cheap=True`: baja a modelos economicos.
- fallback chain por agente para resiliencia.

3. `config/feature_flags.yaml`
- `openrouter.use_openrouter_by_agent.<agent>` permite migracion gradual.

## 2. Estrategia de ajuste

1. Mantener `daily_cap_usd` bajo durante rollout (ej. 5-10 USD/dia).
2. Activar agentes uno por uno y medir impacto marginal.
3. Priorizar downgrade en agentes de mayor frecuencia (`liquidity`, `events`).
4. Mantener `governor`/`synthesis` con modelos estables (no los mas baratos por defecto).

## 3. Presupuesto recomendado

Referencia de `CONTEXT_FOR_CLAUDE.md`:

- Perfil individual: ~`$5.79/mes` (+/- 20%)
- Perfil pro: ~`$19.30/mes` (+/- 20%)

Rango operativo aceptable:

- Individual: `4.63 - 6.95 USD/mes`
- Pro: `15.44 - 23.16 USD/mes`

## 4. Monitoreo operativo

```bash
python - <<'PY'
from services.cost_monitor import CostMonitor
m = CostMonitor("db/cost_monitor.db")
print("today:", m.spent_today_usd())
print("by_agent:", m.spent_by_agent_today())
print("downgrade?", m.should_downgrade())
print("block?", m.should_block())
PY
```

## 5. Acciones segun alertas

### Alerta 80% cap diario

1. Verificar agentes con mayor consumo.
2. Activar `prefer_cheap` en rutas no criticas.
3. Reducir frecuencia de `full_analysis` scheduler.

### Cap diario alcanzado

1. Mantener kill de premium calls (`should_block()`).
2. Permitir solo flujos esenciales o consultas puntuales.
3. Revisar configuracion al siguiente dia UTC.

## 6. Checklist de release

- Confirmar costo proyectado dentro de +/-20% del objetivo.
- Adjuntar captura de `spent_by_agent_today` en PR release.
- Registrar cualquier override temporal de budget en `OPERATIONS.md`.
