// Aetheer cdp_drawing — remove a drawing by id.
// Returns true if the drawing was removed from the registry, false if it
// wasn't found. DOM removal is best-effort and does not affect the boolean.

(function (p) {
    if (!window.__aetheerDrawings) {
        window.__aetheerDrawings = {};
    }
    var record = window.__aetheerDrawings[p.id];
    if (!record) {
        return false;
    }
    try {
        var node = document.getElementById(p.id);
        if (node && node.parentNode) {
            node.parentNode.removeChild(node);
        }
    } catch (e) {
        // swallow — registry purge below is the source of truth.
    }
    delete window.__aetheerDrawings[p.id];
    return true;
})(__PARAMS__);
