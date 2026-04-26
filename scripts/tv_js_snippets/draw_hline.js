// Aetheer cdp_drawing — draw a horizontal price line.
// See draw_rect.js for the injection contract.

(function (p) {
    if (!window.__aetheerDrawings) {
        window.__aetheerDrawings = {};
    }

    var record = {
        id: p.id,
        kind: "hline",
        symbol: p.symbol,
        timeframe: p.timeframe,
        price: p.price,
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
            node.setAttribute("data-aetheer", "hline");
            node.setAttribute("data-symbol", p.symbol);
            node.style.position = "absolute";
            node.style.left = "0";
            node.style.right = "0";
            node.style.height = "1px";
            node.style.background = p.color;
            node.style.zIndex = "9999";
            node.title = p.label + " @ " + p.price;
            host.appendChild(node);
            record.node_id = p.id;
        }
    } catch (e) {
        record.dom_error = String(e);
    }

    window.__aetheerDrawings[p.id] = record;
    return p.id;
})(__PARAMS__);
