"""
tv_commands.py — Comandos de TradingView para Aetheer.

Implementa las operaciones que Aetheer necesita usando TVBridge (CDP).
Los snippets de JavaScript fueron extraídos de tradingview-mcp (MIT License),
ubicado en ~/tradingview-mcp/src/core/*.js.

Modelo: un solo chart activo. Lectura multi-símbolo se hace con set_symbol
secuencial + guard de verificación. Multi-tab fue eliminado (2026-04-21).

Archivos fuente de referencia:
  connection.js  → CHART_API, BARS_PATH, safeString()
  chart.js       → getState, setTimeframe, setSymbol
  data.js        → getQuote, getOhlcv, getStudyValues, getPineTables
  health.js      → healthCheck, launch
  wait.js        → waitForChartReady
"""

import asyncio
import json
import logging
import time
from typing import Any, Optional

from .tv_bridge import TVBridge

logger = logging.getLogger("aetheer.tv_commands")

# Paths de la API de TradingView Desktop (de connection.js / chart.js / data.js)
_CHART_API = "window.TradingViewApi._activeChartWidgetWV.value()"
_BARS_PATH = (
    "window.TradingViewApi._activeChartWidgetWV.value()"
    "._chartWidget.model().mainSeries().bars()"
)

CHART_LOAD_DELAY = 1.5  # segundos después de cambiar tab/TF


def _js_str(s: str) -> str:
    """Escapa string para interpolación segura en JS (como safeString en connection.js)."""
    return json.dumps(str(s))


class TVCommands:
    """Comandos de TradingView Desktop para Aetheer.

    Uso típico:
        bridge = TVBridge()
        await bridge.connect()
        tv = TVCommands(bridge)
        quote = await tv.get_quote()
    """

    def __init__(self, bridge: TVBridge):
        self.bridge = bridge
        self._nav_lock = asyncio.Lock()

    # ── HEALTH ────────────────────────────────────────────────────────────────

    async def health_check(self) -> dict:
        """Verificar conexión y estado básico del chart (de health.js healthCheck)."""
        try:
            result = await self.bridge.evaluate(
                f"""
                (function() {{
                    var result = {{ url: window.location.href, title: document.title }};
                    try {{
                        var chart = {_CHART_API};
                        result.symbol = chart.symbol();
                        result.resolution = chart.resolution();
                        result.chartType = chart.chartType();
                        result.apiAvailable = true;
                    }} catch(e) {{
                        result.symbol = 'unknown';
                        result.resolution = 'unknown';
                        result.chartType = null;
                        result.apiAvailable = false;
                        result.apiError = e.message;
                    }}
                    return result;
                }})()
                """,
                timeout=5,
            )
            return {
                "connected": True,
                "port": self.bridge.port,
                "api_available": bool(result and result.get("apiAvailable")),
                "symbol": result.get("symbol") if result else None,
                "resolution": result.get("resolution") if result else None,
                "url": result.get("url") if result else None,
            }
        except Exception as e:
            return {"connected": False, "port": self.bridge.port, "error": str(e)}

    # ── CHART STATE ───────────────────────────────────────────────────────────

    async def get_chart_state(self) -> dict:
        """Estado del chart activo: símbolo, timeframe, indicadores (de chart.js getState)."""
        result = await self.bridge.evaluate(
            f"""
            (function() {{
                var chart = {_CHART_API};
                var studies = [];
                try {{
                    var allStudies = chart.getAllStudies();
                    studies = allStudies.map(function(s) {{
                        return {{ id: s.id, name: s.name || s.title || 'unknown' }};
                    }});
                }} catch(e) {{}}
                return {{
                    symbol: chart.symbol(),
                    resolution: chart.resolution(),
                    chartType: chart.chartType(),
                    studies: studies,
                }};
            }})()
            """
        )
        return {"success": True, **(result or {})}

    # ── PRECIO / QUOTE ────────────────────────────────────────────────────────

    async def get_quote(self) -> dict:
        """Quote del chart activo (de data.js getQuote, líneas 245-278).

        NOTA: Siempre retorna datos del chart activo — no acepta symbol param.
        Esta es una limitación del acceso vía BARS_PATH en TradingView Desktop,
        igual al bug documentado en tradingview-mcp.
        """
        data = await self.bridge.evaluate(
            f"""
            (function() {{
                var api = {_CHART_API};
                var sym = '';
                try {{ sym = api.symbol(); }} catch(e) {{}}
                if (!sym) {{ try {{ sym = api.symbolExt().symbol; }} catch(e) {{}} }}
                var ext = {{}};
                try {{ ext = api.symbolExt() || {{}}; }} catch(e) {{}}
                var bars = {_BARS_PATH};
                var quote = {{ symbol: sym }};
                if (bars && typeof bars.lastIndex === 'function') {{
                    var last = bars.valueAt(bars.lastIndex());
                    if (last) {{
                        quote.time   = last[0];
                        quote.open   = last[1];
                        quote.high   = last[2];
                        quote.low    = last[3];
                        quote.close  = last[4];
                        quote.last   = last[4];
                        quote.volume = last[5] || 0;
                    }}
                }}
                try {{
                    var hdr = document.querySelector('[class*="headerRow"] [class*="last-"]');
                    if (hdr) {{
                        var hdrPrice = parseFloat(hdr.textContent.replace(/[^0-9.\\-]/g, ''));
                        if (!isNaN(hdrPrice)) quote.header_price = hdrPrice;
                    }}
                }} catch(e) {{}}
                if (ext.description) quote.description = ext.description;
                if (ext.exchange)    quote.exchange = ext.exchange;
                if (ext.type)        quote.type = ext.type;
                return quote;
            }})()
            """
        )
        if not data or (not data.get("last") and not data.get("close")):
            raise RuntimeError(
                "No se pudo obtener quote. El chart puede estar cargando."
            )
        return {"success": True, **data}

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    async def get_ohlcv(self, count: int = 100, summary: bool = True) -> dict:
        """Datos OHLCV del chart activo (de data.js getOhlcv, líneas 62-107).

        Args:
            count: Máximo de barras a leer (tope: 500).
            summary: True → stats compactos (open/close/high/low/range/change).
                     False → todas las barras.
        """
        limit = min(count, 500)
        data = await self.bridge.evaluate(
            f"""
            (function() {{
                var bars = {_BARS_PATH};
                if (!bars || typeof bars.lastIndex !== 'function') return null;
                var result = [];
                var end   = bars.lastIndex();
                var start = Math.max(bars.firstIndex(), end - {limit} + 1);
                for (var i = start; i <= end; i++) {{
                    var v = bars.valueAt(i);
                    if (v) result.push({{
                        time: v[0], open: v[1], high: v[2],
                        low:  v[3], close: v[4], volume: v[5] || 0
                    }});
                }}
                return {{bars: result, total_bars: bars.size(), source: 'direct_bars'}};
            }})()
            """
        )

        if not data or not data.get("bars"):
            raise RuntimeError(
                "No se pudo extraer OHLCV. El chart puede estar cargando."
            )

        if summary:
            bars = data["bars"]
            highs   = [b["high"] for b in bars]
            lows    = [b["low"] for b in bars]
            volumes = [b["volume"] for b in bars]
            first, last_bar = bars[0], bars[-1]
            return {
                "success":    True,
                "bar_count":  len(bars),
                "period":     {"from": first["time"], "to": last_bar["time"]},
                "open":       first["open"],
                "close":      last_bar["close"],
                "high":       max(highs),
                "low":        min(lows),
                "range":      round(max(highs) - min(lows), 5),
                "change":     round(last_bar["close"] - first["open"], 5),
                "change_pct": f"{round((last_bar['close'] - first['open']) / first['open'] * 10000) / 100}%",
                "avg_volume": round(sum(volumes) / len(volumes)) if volumes else 0,
                "last_5_bars": bars[-5:],
            }

        return {
            "success":         True,
            "bar_count":       len(data["bars"]),
            "total_available": data.get("total_bars"),
            "source":          data.get("source"),
            "bars":            data["bars"],
        }

    # ── INDICADORES ───────────────────────────────────────────────────────────

    async def get_study_values(self) -> dict:
        """Valores de indicadores nativos del chart activo (de data.js getStudyValues)."""
        data = await self.bridge.evaluate(
            """
            (function() {
                var chart  = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget;
                var model  = chart.model();
                var sources = model.model().dataSources();
                var results = [];
                for (var si = 0; si < sources.length; si++) {
                    var s = sources[si];
                    if (!s.metaInfo) continue;
                    try {
                        var meta = s.metaInfo();
                        var name = meta.description || meta.shortDescription || '';
                        if (!name) continue;
                        var values = {};
                        try {
                            var dwv = s.dataWindowView();
                            if (dwv) {
                                var items = dwv.items();
                                if (items) {
                                    for (var i = 0; i < items.length; i++) {
                                        var item = items[i];
                                        if (item._value && item._value !== '\\u2205' && item._title)
                                            values[item._title] = item._value;
                                    }
                                }
                            }
                        } catch(e) {}
                        if (Object.keys(values).length > 0)
                            results.push({ name: name, values: values });
                    } catch(e) {}
                }
                return results;
            })()
            """
        )
        return {"success": True, "study_count": len(data or []), "studies": data or []}

    async def get_pine_tables(self, study_filter: Optional[str] = None) -> dict:
        """Datos expuestos por indicadores Pine Script custom.

        Extrae dos tipos de payloads:
          1. Tableceldas visibles (`dwgtablecells`) — tabla renderizada en pantalla.
          2. Texto de labels invisibles (`dwglabels`) — usado por indicadores
             como Aetheer v1.2.0 que emiten el payload completo como JSON en
             un label con `style=label.style_none` para rendimiento (sin pintar
             tabla). Este path se añadió en 2026-04-22 tras confirmarse que
             el extractor anterior retornaba `study_count=0` para Aetheer.

        Args:
            study_filter: Nombre del indicador a filtrar (e.g., "Aetheer").
        """
        filter_js = _js_str(study_filter or "")

        raw = await self.bridge.evaluate(
            f"""
            (function() {{
                var chart   = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget;
                var model   = chart.model();
                var sources = model.model().dataSources();
                var results = [];
                var filter  = {filter_js};
                for (var si = 0; si < sources.length; si++) {{
                    var s = sources[si];
                    if (!s.metaInfo) continue;
                    try {{
                        var meta = s.metaInfo();
                        var name = meta.description || meta.shortDescription || '';
                        if (!name) continue;
                        if (filter && name.indexOf(filter) === -1) continue;
                        var g = s._graphics;
                        if (!g || !g._primitivesCollection) continue;
                        var pc     = g._primitivesCollection;
                        var items  = [];
                        var labels = [];
                        // 1) Tableceldas visibles
                        try {{
                            var tcOuter = pc.dwgtablecells;
                            if (tcOuter) {{
                                var tcColl = tcOuter.get('tableCells');
                                if (tcColl && tcColl._primitivesDataById
                                        && tcColl._primitivesDataById.size > 0) {{
                                    tcColl._primitivesDataById.forEach(function(v, id) {{
                                        items.push({{id: id, raw: v}});
                                    }});
                                }}
                            }}
                        }} catch(e) {{}}
                        // 2) Texto de labels (payloads JSON de indicadores como Aetheer)
                        try {{
                            var lblOuter = pc.dwglabels;
                            if (lblOuter && typeof lblOuter.get === 'function') {{
                                var inner = lblOuter.get('labels');
                                if (inner && inner.forEach) {{
                                    inner.forEach(function(ival) {{
                                        if (!ival || !ival._primitivesDataById) return;
                                        ival._primitivesDataById.forEach(function(pv, pk) {{
                                            if (!pv) return;
                                            var txt = (pv.t !== undefined) ? pv.t
                                                   : (pv.text !== undefined) ? pv.text
                                                   : (pv.data && pv.data.text) ? pv.data.text
                                                   : null;
                                            if (txt !== null && txt !== undefined && String(txt).length > 0) {{
                                                labels.push({{id: String(pk), text: String(txt)}});
                                            }}
                                        }});
                                    }});
                                }}
                            }}
                        }} catch(e) {{}}
                        if (items.length > 0 || labels.length > 0)
                            results.push({{
                                name: name,
                                count: items.length,
                                items: items,
                                labels: labels
                            }});
                    }} catch(e) {{}}
                }}
                return results;
            }})()
            """
        )

        if not raw:
            return {"success": True, "study_count": 0, "studies": []}

        studies = []
        for s in raw:
            # 1) Reconstruir tablas de dwgtablecells
            tables: dict[Any, dict] = {}
            for item in s.get("items", []):
                v   = item.get("raw", {})
                tid = v.get("tid", 0)
                row = v.get("row")
                col = v.get("col")
                if row is None or col is None:
                    continue
                if tid not in tables:
                    tables[tid] = {}
                if row not in tables[tid]:
                    tables[tid][row] = {}
                tables[tid][row][col] = v.get("t", "")

            table_list = []
            for _tid, rows in tables.items():
                formatted = []
                for rn in sorted(rows.keys()):
                    cols = rows[rn]
                    row_text = " | ".join(
                        cols[cn] for cn in sorted(cols.keys()) if cols[cn]
                    )
                    if row_text:
                        formatted.append(row_text)
                table_list.append({"rows": formatted})

            # 2) Parsear labels: intentar JSON primero; si falla, exponer texto plano
            label_payloads = []
            for lbl in s.get("labels", []):
                txt = lbl.get("text", "")
                if not txt:
                    continue
                entry: dict[str, Any] = {"id": lbl.get("id"), "text": txt}
                try:
                    parsed = json.loads(txt)
                    if isinstance(parsed, dict):
                        entry["json"] = parsed
                except (json.JSONDecodeError, ValueError):
                    pass
                label_payloads.append(entry)

            studies.append({
                "name": s["name"],
                "tables": table_list,
                "labels": label_payloads,
            })

        return {"success": True, "study_count": len(studies), "studies": studies}

    async def get_quote_for_symbol(self, tv_symbol: str) -> Optional[dict]:
        """Quote para un símbolo específico usando set_symbol sobre el chart activo.

        Estrategia single-chart (2026-04-21):
          1. Si el chart activo ya muestra tv_symbol → lee directo, sin tocar.
          2. Si no → captura el símbolo activo, set_symbol(tv_symbol), verifica
             que el switch tomó efecto (guard), lee quote, restaura al original.

        Retorna None si la verificación post-switch falla (evita contaminar
        caches con bars del símbolo equivocado).
        """
        active_symbol = ""
        try:
            state = await self.get_chart_state()
            active_symbol = (state.get("symbol") or "").upper()
            if active_symbol == tv_symbol.upper():
                return await self.get_quote()
        except Exception as e:
            logger.debug(f"get_quote_for_symbol: get_chart_state falló: {e}")

        result: Optional[dict] = None
        switched = False
        try:
            await self.set_symbol(tv_symbol)
            post_state = await self.get_chart_state()
            post_symbol = (post_state.get("symbol") or "").upper()
            if post_symbol != tv_symbol.upper():
                logger.error(
                    f"get_quote_for_symbol: set_symbol({tv_symbol}) no tomó efecto "
                    f"(chart aún en {post_symbol}) — abortando lectura"
                )
                return None
            switched = True
            result = await self.get_quote()
        except Exception as e:
            logger.warning(f"get_quote_for_symbol: set_symbol({tv_symbol}) falló: {e}")
            result = None
        finally:
            if switched and active_symbol and active_symbol != tv_symbol.upper():
                try:
                    await self.set_symbol(active_symbol)
                except Exception as e:
                    logger.warning(f"get_quote_for_symbol: restaurar {active_symbol} falló: {e}")
        return result

    # ── NAVEGACIÓN ────────────────────────────────────────────────────────────

    async def set_symbol(self, tv_symbol: str) -> bool:
        """Cambiar el símbolo del chart activo y esperar a que cargue.

        Args:
            tv_symbol: Símbolo en formato TradingView, e.g. "TVC:DXY", "OANDA:EURUSD"
        """
        async with self._nav_lock:
            await self.bridge.evaluate(
                f"""
                (function() {{
                    var chart = {_CHART_API};
                    chart.setSymbol({_js_str(tv_symbol)}, {{}});
                }})()
                """
            )
            await asyncio.sleep(CHART_LOAD_DELAY)
            await self._wait_for_chart_ready(timeout=8.0)
        return True

    async def set_timeframe(self, timeframe: str) -> bool:
        """Cambiar timeframe del chart activo y esperar a que cargue (de chart.js setTimeframe).

        Args:
            timeframe: "1", "5", "15", "60", "240", "D", "W", "M"
        """
        async with self._nav_lock:
            await self.bridge.evaluate(
                f"""
                (function() {{
                    var chart = {_CHART_API};
                    chart.setResolution({_js_str(timeframe)}, {{}});
                }})()
                """
            )
            await self._wait_for_chart_ready(timeout=8.0)
        return True

    async def _wait_for_chart_ready(
        self,
        expected_symbol: Optional[str] = None,
        timeout: float = 10.0,
        poll_interval: float = 0.2,
    ) -> bool:
        """Poll hasta que el chart no esté cargando (de wait.js waitForChartReady)."""
        start = time.monotonic()
        last_bar_count = -1
        stable_count = 0

        while time.monotonic() - start < timeout:
            try:
                state = await self.bridge.evaluate(
                    """
                    (function() {
                        var spinner = document.querySelector('[class*="loader"]')
                            || document.querySelector('[class*="loading"]')
                            || document.querySelector('[data-name="loading"]');
                        var isLoading = spinner && spinner.offsetParent !== null;
                        var barCount = -1;
                        try {
                            var bars = document.querySelectorAll('[class*="bar"]');
                            barCount = bars.length;
                        } catch(e) {}
                        return { isLoading: !!isLoading, barCount: barCount };
                    })()
                    """,
                    timeout=3,
                )
                if not state or state.get("isLoading"):
                    stable_count = 0
                    await asyncio.sleep(poll_interval)
                    continue

                bar_count = state.get("barCount", -1)
                if bar_count == last_bar_count and bar_count > 0:
                    stable_count += 1
                else:
                    stable_count = 0
                last_bar_count = bar_count

                if stable_count >= 2:
                    return True
            except Exception:
                pass
            await asyncio.sleep(poll_interval)

        return False

    # ── DEEP READ (operación compuesta, single-chart) ─────────────────────────

    async def deep_read(
        self,
        symbols_config: dict[str, str],
        timeframes: list[str],
        restore_to_symbol: Optional[str] = None,
    ) -> dict:
        """Lectura profunda sobre un chart único: itera símbolos y timeframes.

        Para cada (símbolo, TF) hace set_symbol + set_timeframe, verifica que
        el chart efectivamente cambió (guard), y lee quote + OHLCV + Pine tables.
        Siempre intenta restaurar el estado final al terminar, incluso si hay error.

        Args:
            symbols_config: {"DXY": "TVC:DXY", "EURUSD": "OANDA:EURUSD", ...}
            timeframes: ["D", "240", "60", "15"] (valores TV)
            restore_to_symbol: TV symbol al que volver al terminar. Si es None,
                restaura al símbolo activo antes de iniciar (default).

        Returns:
            {"DXY": {"D": {...}, "240": {...}}, "EURUSD": {...}, ...}
            Cada tf_data incluye "symbol_verified" (bool) para que el caller
            pueda descartar lecturas con guard fallido.
        """
        results: dict = {}

        try:
            original_state = await self.get_chart_state()
            original_symbol = (original_state or {}).get("symbol") or ""
            original_tf = (original_state or {}).get("resolution") or "60"
        except Exception:
            original_symbol = ""
            original_tf = "60"

        final_restore = restore_to_symbol if restore_to_symbol else original_symbol

        try:
            for symbol_name, tv_symbol in symbols_config.items():
                results[symbol_name] = {}

                try:
                    await self.set_symbol(tv_symbol)
                except Exception as e:
                    results[symbol_name]["symbol_error"] = str(e)
                    logger.warning(f"deep_read: set_symbol({tv_symbol}) falló: {e}")
                    continue

                # Guard: verificar que el chart realmente cargó el símbolo pedido
                try:
                    state = await self.get_chart_state()
                    actual_symbol = (state or {}).get("symbol") or ""
                except Exception:
                    actual_symbol = ""

                if actual_symbol.upper() != tv_symbol.upper():
                    results[symbol_name]["symbol_error"] = (
                        f"guard: pedí {tv_symbol} pero chart muestra {actual_symbol!r}"
                    )
                    logger.error(
                        f"deep_read guard: esperaba {tv_symbol}, chart en {actual_symbol} — saltando"
                    )
                    continue

                for tf in timeframes:
                    try:
                        await self.set_timeframe(tf)
                    except Exception as e:
                        results[symbol_name][tf] = {
                            "error": f"set_timeframe({tf}) falló: {e}",
                            "symbol_verified": False,
                        }
                        continue

                    # Guard post-set_timeframe: el símbolo puede haber cambiado
                    # entre set_symbol y el final de set_timeframe (race con TV).
                    # Sin este chequeo, OHLCV y Aetheer se leen del símbolo viejo
                    # y terminan etiquetados con el nuevo → cross-contamination.
                    try:
                        post_state = await self.get_chart_state()
                        post_symbol = (post_state or {}).get("symbol") or ""
                    except Exception:
                        post_symbol = ""

                    if post_symbol.upper() != tv_symbol.upper():
                        results[symbol_name][tf] = {
                            "error": (
                                f"guard post-TF: esperaba {tv_symbol}, "
                                f"chart drift a {post_symbol!r}"
                            ),
                            "symbol_verified": False,
                        }
                        logger.error(
                            f"deep_read TF-guard: {tv_symbol}@{tf} drift a "
                            f"{post_symbol} — descartando lectura"
                        )
                        continue

                    tf_data: dict = {
                        "timeframe":       tf,
                        "symbol":          symbol_name,
                        "tv_symbol":       tv_symbol,
                        "actual_symbol":   post_symbol,
                        "symbol_verified": True,
                    }

                    try:
                        tf_data["quote"] = await self.get_quote()
                    except Exception as e:
                        tf_data["quote_error"] = str(e)

                    try:
                        tf_data["ohlcv"] = await self.get_ohlcv(summary=True)
                    except Exception as e:
                        tf_data["ohlcv_error"] = str(e)

                    try:
                        tf_data["aetheer"] = await self.get_pine_tables(
                            study_filter="Aetheer"
                        )
                    except Exception as e:
                        tf_data["aetheer_error"] = str(e)

                    try:
                        tf_data["studies"] = await self.get_study_values()
                    except Exception as e:
                        tf_data["studies_error"] = str(e)

                    # Cross-validation: el Pine Aetheer emite SYMBOL en su payload.
                    # Si no coincide con el esperado → datos contaminados, descartar.
                    aet_payload = tf_data.get("aetheer") or {}
                    payload_syms = [
                        (lbl.get("json") or {}).get("symbol")
                        for st in aet_payload.get("studies", []) or []
                        for lbl in st.get("labels", []) or []
                        if isinstance(lbl.get("json"), dict)
                    ]
                    payload_syms = [s for s in payload_syms if s]
                    if payload_syms:
                        expected_short = tv_symbol.split(":")[-1].upper()
                        mismatch = [s for s in payload_syms
                                    if s.upper() != expected_short]
                        if mismatch:
                            tf_data["symbol_verified"] = False
                            tf_data["aetheer_symbol_mismatch"] = {
                                "expected": expected_short,
                                "got": payload_syms,
                            }
                            logger.error(
                                f"deep_read payload-guard: {tv_symbol}@{tf} "
                                f"Aetheer reporta {payload_syms}, esperaba "
                                f"{expected_short} — marcando unverified"
                            )

                    results[symbol_name][tf] = tf_data

        finally:
            try:
                if final_restore:
                    await self.set_symbol(final_restore)
                if original_tf:
                    await self.set_timeframe(str(original_tf))
            except Exception as e:
                logger.error(f"deep_read: error restaurando chart a {final_restore}: {e}")

        return results
