// Aetheer cdp_drawing — draw a text annotation pinned to a price.
// See draw_rect.js for the injection contract.
//
// p.text is set via .textContent (NOT innerHTML) so any HTML/script in the
// label is rendered as inert text — defense-in-depth against the Python-side
// sanitizer being bypassed.

(function (p) {
    if (!window.__aetheerDrawings) {
        window.__aetheerDrawings = {};
    }

    var record = {
        id: p.id,
        kind: "text",
        symbol: p.symbol,
        timeframe: p.timeframe,
        price: p.price,
        text: p.text,
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
            node.setAttribute("data-aetheer", "text");
            node.setAttribute("data-symbol", p.symbol);
            node.style.position = "absolute";
            node.style.color = p.color;
            node.style.font = "12px sans-serif";
            node.style.zIndex = "9999";
            node.textContent = p.text;
            host.appendChild(node);
            record.node_id = p.id;
        }
    } catch (e) {
        record.dom_error = String(e);
    }

    window.__aetheerDrawings[p.id] = record;
    return p.id;
})(__PARAMS__);
