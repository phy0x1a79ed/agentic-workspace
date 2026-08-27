// penpot-view-refresh — plugin.js
//
// Runs in Penpot's sandboxed plugin context (see the `penpot` global; no DOM,
// no window). Two jobs, both manual — see index.html for the UI that drives
// them:
//
//   1. "Link"    — remember a shape's source /penpot-view/<file>/<page>/<board>
//                  render URL, via plugin data (setPluginData/getPluginData —
//                  see plugin-types/index.d.ts). Plugin data is per-shape and
//                  travels with the file.
//   2. "Refresh" — re-fetch that URL through penpot.uploadMediaUrl() and
//                  reassign the shape's fill image. Penpot's own backend does
//                  the HTTP fetch server-side (see docs/plugins/api.md +
//                  index.d.ts's uploadMediaUrl doc comment), so this plugin
//                  never touches the image bytes itself and there is no CORS
//                  or exposure concern from the sandbox.
//
// No automatic refresh (on focus, on interval, ...) is wired up yet — see
// INSTALL.md for why: this ships manual-only until that has been verified
// against a real running instance.

var DATA_KEY = "penpot-view-refresh:sourceUrl";

// Three plain UUID path segments only — scale/swap/crop live in the query
// string. A path segment that looks like a query param (e.g. someone pasted
// "...?scale=2" as a fourth *segment* instead of a query string) is refused
// here rather than silently accepted and 404ing later at the edge.
var VIEW_PATH_RE =
  /^\/penpot-view\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/?$/i;

penpot.ui.open("Penpot View Refresh", "index.html", { width: 340, height: 280 });

function selectedShape() {
  return penpot.selection && penpot.selection[0];
}

// Validates and normalizes a pasted source URL. Must be absolute: Penpot's
// backend fetches it directly from its own process, so a bare "/penpot-view/…"
// path (correct for a browser resolving against the current origin) is not
// resolvable there — see the deployment note in INSTALL.md.
function parseViewUrl(raw) {
  var trimmed = (raw || "").trim();
  var url;
  try {
    url = new URL(trimmed);
  } catch (err) {
    return {
      ok: false,
      error:
        "Must be an absolute URL, e.g. http://host/penpot-view/<file>/<page>/<board> " +
        "— Penpot's backend fetches it server-side and cannot resolve a relative path.",
    };
  }
  if (!VIEW_PATH_RE.test(url.pathname)) {
    return {
      ok: false,
      error:
        "Path must be exactly /penpot-view/<file-uuid>/<page-uuid>/<board-uuid> " +
        "— scale/swap/crop belong in the query string, never as extra path segments.",
    };
  }
  return { ok: true, url: trimmed };
}

function sendState() {
  var shape = selectedShape();
  penpot.ui.sendMessage({
    type: "state",
    shapeId: shape ? shape.id : null,
    shapeName: shape ? shape.name : null,
    sourceUrl: shape ? shape.getPluginData(DATA_KEY) || "" : "",
  });
}

penpot.on("selectionchange", sendState);

penpot.ui.onMessage(function (message) {
  if (!message || typeof message !== "object") return;
  if (message.type === "ready") {
    sendState();
  } else if (message.type === "link") {
    link(message.url);
  } else if (message.type === "refresh") {
    refresh();
  }
});

function link(raw) {
  var shape = selectedShape();
  if (!shape) {
    penpot.ui.sendMessage({ type: "error", message: "Select a shape first." });
    return;
  }
  var parsed = parseViewUrl(raw);
  if (!parsed.ok) {
    penpot.ui.sendMessage({ type: "error", message: parsed.error });
    return;
  }
  shape.setPluginData(DATA_KEY, parsed.url);
  sendState();
}

function refresh() {
  var shape = selectedShape();
  if (!shape) {
    penpot.ui.sendMessage({ type: "error", message: "Select a shape first." });
    return;
  }
  var sourceUrl = shape.getPluginData(DATA_KEY);
  if (!sourceUrl) {
    penpot.ui.sendMessage({
      type: "error",
      message: "This shape has no linked source yet — link one first.",
    });
    return;
  }
  penpot.ui.sendMessage({ type: "busy", busy: true });
  var name = shape.name || "penpot-view-refresh";
  penpot
    .uploadMediaUrl(name, sourceUrl)
    .then(function (imageData) {
      // Replaces the whole fill list with the freshly-fetched render. Any
      // other fill the shape carried (a background color behind a
      // transparent SVG, say) is intentionally dropped — this plugin's job
      // is "this shape shows the current render," not partial compositing.
      shape.fills = [{ fillOpacity: 1, fillImage: imageData }];
      penpot.ui.sendMessage({ type: "refreshed", shapeId: shape.id });
    })
    .catch(function (err) {
      penpot.ui.sendMessage({
        type: "error",
        message: "Refresh failed: " + (err && err.message ? err.message : String(err)),
      });
    })
    .finally(function () {
      penpot.ui.sendMessage({ type: "busy", busy: false });
    });
}
