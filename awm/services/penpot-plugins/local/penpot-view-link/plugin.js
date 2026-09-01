// penpot-view-link — plugin.js
//
// Runs in Penpot's sandboxed plugin context. It has the `penpot` global and
// nothing else: no `document`, no `window`, no `location`, no `navigator`
// (the allowlist is in plugins/libs/plugins-runtime/src/lib/create-sandbox.ts
// in the Penpot fork). So this file only collects ids. Building the URL and
// copying it happen in index.html, which is a real page with a real DOM.
//
// The problem it solves: a /penpot-view render URL is addressed by three
// UUIDs — file, page, board. Penpot's workspace URL carries the first two and
// no board id appears in any visible surface, so the link cannot be written
// by hand at all.
//
// CAUTION: boards only, deliberately. The whole-page address — the page root
// frame, id 00000000-0000-0000-0000-000000000000 — renders a 0.01x0.01 empty
// SVG and the render service answers 502. That defect is owned by the
// penpot/page-render scope. This file filters the root frame out rather than
// offering a link that cannot work.

var ROOT_FRAME_ID = "00000000-0000-0000-0000-000000000000";

penpot.ui.open("Penpot View Link", "index.html", { width: 360, height: 460 });

// Collects every board on the current page, top-level and nested alike. A
// nested board renders exactly as well as a top-level one, so both are worth
// listing; `parentName` is carried only so the panel can tell them apart when
// two boards share a name.
function collectBoards(page) {
  var rootId = page.root ? page.root.id : ROOT_FRAME_ID;
  // findShapes walks the page's flat objects map, so it returns boards at any
  // depth — and it returns the page root frame too, which is a frame in
  // Penpot's data model. Drop that one.
  var shapes = page.findShapes({ type: "board" }) || [];
  var boards = [];
  for (var i = 0; i < shapes.length; i++) {
    var shape = shapes[i];
    if (!shape || shape.id === rootId || shape.id === ROOT_FRAME_ID) continue;
    var parent = null;
    try {
      parent = shape.parent;
    } catch (err) {
      parent = null;
    }
    var nested = !!(parent && parent.id !== rootId && parent.id !== ROOT_FRAME_ID);
    boards.push({
      id: shape.id,
      name: shape.name || shape.id,
      nested: nested,
      // Carried only for a nested board, where it is what tells two
      // same-named boards apart. Empty for a top-level board, whose parent is
      // always the page root and so says nothing.
      parentName: nested && parent.name ? parent.name : "",
    });
  }
  boards.sort(function (a, b) {
    return a.name.localeCompare(b.name);
  });
  return boards;
}

function sendState() {
  var file = penpot.currentFile;
  var page = penpot.currentPage;

  if (!file || !page) {
    penpot.ui.sendMessage({ type: "state", ready: false });
    return;
  }

  penpot.ui.sendMessage({
    type: "state",
    ready: true,
    fileId: file.id,
    fileName: file.name || "",
    pageId: page.id,
    pageName: page.name || "",
    boards: collectBoards(page),
  });
}

// pagechange is the one event that must be handled: the panel stays open
// across a sitemap click, and a stale board list would hand out links to the
// wrong page. Board creation and renaming are picked up by the panel's own
// Refresh button rather than by a shapechange listener, which fires per shape
// edit and would rebuild the list on every drag.
penpot.on("pagechange", sendState);

penpot.ui.onMessage(function (message) {
  if (!message || typeof message !== "object") return;
  if (message.type === "ready" || message.type === "refresh") {
    sendState();
  }
});
