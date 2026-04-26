// Aetheer cdp_drawing — draw a price-zone rectangle.
//
// Injected via CDP Runtime.evaluate. The Python caller prefixes
//   const __PARAMS__ = JSON.parse(<safe-json-literal>);
// before this body executes. Returns the drawing id (string).
//
// We don't rely on TradingView's private chart API (it changes between
// versions). Instead we maintain a window.__aetheerDrawings registry of
// records that synthesis can later clean up via remove_by_id.js. Visual
// presentation degrades to a registry-only mode if no DOM target is found —
// which is what we want under tests, and harmless in prod (the rollback path
// still purges the registry).

(function (p) {
    if (!window.__aetheerDrawings) {
        window.__aetheerDrawings = {};
    }

    var record = {
        id: p.id,
        kind: "zone",
        symbol: p.symbol,
        timeframe: p.timeframe,
        price_top: p.price_top,
        price_bottom: p.price_bottom,
        label: p.label,
        confidence: p.confidence,
        color: p.color,
        created_at: Date.now(),
        node_id: null
    };

    try {
        var host = document.querySelector(".chart-container, .layout__area--center, body");
        if (host) {
            var node = document.createElement("div");
            node.id = p.id;
            node.setAttribute("data-aetheer", "zone");
            node.setAttribute("data-symbol", p.symbol);
            node.style.position = "absolute";
            node.style.pointerEvents = "none";
            node.style.background = p.color;
            node.style.opacity = "0.18";
            node.style.zIndex = "9999";
            node.title = p.label;
            host.appendChild(node);
            record.node_id = p.id;
        }
    } catch (e) {
        record.dom_error = String(e);
    }

    window.__aetheerDrawings[p.id] = record;
    return p.id;
})(__PARAMS__);
