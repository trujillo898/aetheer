"""
tv_commands.py — Comandos de TradingView para Aetheer.

Implementa las ~10 operaciones que Aetheer necesita usando TVBridge (CDP).
Los snippets de JavaScript fueron extraídos de tradingview-mcp (MIT License),
ubicado en ~/tradingview-mcp/src/core/*.js.

Archivos fuente de referencia:
  connection.js  → CHART_API, BARS_PATH, safeString()
  chart.js       → getState, setTimeframe, setSymbol
  data.js        → getQuote, getOhlcv, getStudyValues, getPineTables
  tab.js         → list, switchTab
  health.js      → healthCheck, launch
  wait.js        → waitForChartReady
"""

import asyncio
import json
import logging
import re
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
        """Tablas de indicadores Pine Script custom (de data.js getPineTables).

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
                        var pc    = g._primitivesCollection;
                        var items = [];
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
                        if (items.length > 0)
                            results.push({{name: name, count: items.length, items: items}});
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

            studies.append({"name": s["name"], "tables": table_list})

        return {"success": True, "study_count": len(studies), "studies": studies}

    # ── NAVEGACIÓN ────────────────────────────────────────────────────────────

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

    # ── TABS ──────────────────────────────────────────────────────────────────

    async def tab_list(self) -> dict:
        """Listar tabs abiertos en TradingView Desktop (de tab.js list)."""
        targets = await self.bridge.get_all_targets()
        chart_re = re.compile(r"tradingview\.com/chart", re.IGNORECASE)
        chart_id_re = re.compile(r"/chart/([^/?]+)")

        tabs = []
        for i, t in enumerate(targets):
            if t.get("type") != "page":
                continue
            url = t.get("url", "")
            if not chart_re.search(url):
                continue
            title = re.sub(r"^Live stock.*charts on ", "", t.get("title", ""))
            match = chart_id_re.search(url)
            tabs.append(
                {
                    "index": i,
                    "id": t["id"],
                    "title": title,
                    "url": url,
                    "chart_id": match.group(1) if match else None,
                }
            )

        return {"success": True, "tab_count": len(tabs), "tabs": tabs}

    async def tab_switch(self, index: int) -> bool:
        """Cambiar al tab por índice y reconectar WebSocket (de tab.js switchTab).

        Usa `json/activate/{id}` vía HTTP + reconexión WebSocket al nuevo target.
        """
        async with self._nav_lock:
            tab_result = await self.tab_list()
            tabs = tab_result.get("tabs", [])

            if index >= len(tabs):
                raise ValueError(
                    f"Tab index {index} out of range (hay {len(tabs)} tabs)"
                )

            target_id = tabs[index]["id"]

            ok = await self.bridge.activate_target(target_id)
            if not ok:
                raise RuntimeError(f"No se pudo activar tab {index} (id={target_id})")

            # Reconectar WebSocket al nuevo target activo
            reconnected = await self.bridge.reconnect_to_active_target()
            if not reconnected:
                raise RuntimeError(
                    f"No se pudo reconectar CDP tras tab switch a índice {index}"
                )

            await asyncio.sleep(CHART_LOAD_DELAY)
        return True

    # ── DEEP READ (operación compuesta) ───────────────────────────────────────

    async def deep_read(
        self,
        tabs_config: dict[str, int],
        timeframes: list[str],
    ) -> dict:
        """Lectura profunda: itera tabs y timeframes, lee quote + OHLCV + Pine tables.

        INTERFIERE con el chart del trader (cambia tabs y timeframes).
        Siempre restaura el estado original, incluso en caso de error.

        Args:
            tabs_config: {"DXY": 0, "EURUSD": 1, "GBPUSD": 2}
            timeframes: ["D", "240", "60", "15"]  (valores TV)

        Returns:
            {"DXY": {"D": {...}, "240": {...}}, "EURUSD": {...}, ...}
        """
        results: dict = {}

        # Guardar estado original
        try:
            original_state = await self.get_chart_state()
            original_tf = original_state.get("resolution", "60")
        except Exception:
            original_state = None
            original_tf = "60"

        original_tab = 0  # Restaurar al tab 0 si no podemos determinar el activo

        try:
            for symbol_name, tab_index in tabs_config.items():
                results[symbol_name] = {}

                try:
                    await self.tab_switch(tab_index)
                except Exception as e:
                    results[symbol_name]["tab_error"] = str(e)
                    logger.warning(f"deep_read: tab_switch({tab_index}) falló: {e}")
                    continue

                try:
                    state = await self.get_chart_state()
                    actual_symbol = state.get("symbol", "") if state else ""
                except Exception:
                    actual_symbol = ""

                for tf in timeframes:
                    try:
                        await self.set_timeframe(tf)
                    except Exception as e:
                        results[symbol_name][tf] = {
                            "error": f"set_timeframe({tf}) falló: {e}"
                        }
                        continue

                    tf_data: dict = {
                        "timeframe":      tf,
                        "symbol":         symbol_name,
                        "actual_symbol":  actual_symbol,
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

                    results[symbol_name][tf] = tf_data

        finally:
            # SIEMPRE restaurar estado original del trader
            try:
                await self.tab_switch(original_tab)
                if original_tf:
                    await self.set_timeframe(str(original_tf))
            except Exception as e:
                logger.error(f"deep_read: error restaurando estado: {e}")

        return results
