/**
 * awm drawio integration — replaces the stock PreConfig.js.
 *
 * Copyright (c) 2006-2024, JGraph Holdings Ltd
 * Copyright (c) 2006-2024, draw.io AG
 *
 * What this replaces
 * ------------------
 * The prototype polled the file every 2 s: HEAD for Last-Modified, GET the
 * whole document if it moved, PUT the whole document whenever the editor's
 * snapshot changed. That had no protocol between writers, and it cost real
 * work: a tab left open across a scripted rebuild PUT its stale in-memory
 * model back over the new file and took one page from 47 cells to 2.
 *
 * Here, every save carries the revision the tab is based on. The service
 * rejects a save from a stale tab instead of applying it, which makes that
 * whole class of loss unrepresentable rather than merely unlikely. There is no
 * polling: the service pushes over an emit socket when the diagram moves.
 *
 * The merge handshake
 * -------------------
 * When an agent lands a checkout, the service emits `flush`. We save
 * immediately, acknowledge, and stop saving until the following `push` — so an
 * autosave that was already in flight cannot land after the merge and revert
 * it. Editing stays live throughout; only the *save* is deferred, for well
 * under a second.
 *
 * Reconciliation
 * --------------
 * A rejected save is not a reload. If the tab has unsaved work, throwing it
 * away to refetch would be the same data loss wearing a different hat, so a
 * stale rejection hands the user a choice and keeps their edits on screen
 * until they make it.
 */

// Overrides of global vars need to be pre-loaded.
window.DRAWIO_PUBLIC_BUILD = true;
window.PLANT_URL = null;
window.DRAWIO_BASE_URL = null;
window.DRAWIO_VIEWER_URL = null;
window.DRAWIO_LIGHTBOX_URL = null;
window.DRAW_MATH_URL = 'math4/es5';
window.DRAWIO_CONFIG = null;
// Origin-relative: the export server is proxied by the gateway, so this works
// through the HTTPS front from any device instead of only on the host's
// loopback (which is what the prototype's hardcoded 127.0.0.1:8000 meant).
window.EXPORT_URL = '/svc/drawio/export';

urlParams['sync'] = 'manual';
urlParams['dark'] = '0';
urlParams['splash'] = '0';
// No urlParams['url']: content arrives through the service's `read` verb, so
// the editor works identically for a live diagram and for a checkout, and
// never needs the document to be reachable as a static file.

(function () {
  'use strict';

  var SVC = '/svc/drawio/fn/';
  var POLL_MS = 2000;
  var params = new URLSearchParams(window.location.search);
  var HANDLE = params.get('checkout');
  var SAVE = params.get('save');
  var TAB = 'tab-' + Math.random().toString(36).slice(2, 10);

  var target = null;      // {save, handle} — what we are editing
  var baseRev = null;     // revision this tab's content is based on
  var lastXml = null;     // last snapshot we saved or loaded
  var inflight = false;
  var loading = false;
  var holding = false;    // a merge is landing; defer saves
  var attached = false;

  function call(fn, args) {
    return fetch(SVC + fn, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(args || {}),
    }).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok || body.error) {
          throw new Error(body.error || ('HTTP ' + r.status));
        }
        return body;
      });
    });
  }

  function status(message) {
    try {
      var ui = window.__drawioUi;
      if (ui && ui.editor && ui.editor.setStatus) ui.editor.setStatus(message);
    } catch (e) { /* status line is cosmetic */ }
  }

  function snapshot(ui) {
    // getFileData(forceXml=true, ..., uncompressed=true) is the same payload
    // as File > Save. Uncompressed is mandatory: the service refuses a
    // deflated document, since nothing downstream could diff or merge it.
    try {
      return ui.getFileData(true, null, null, null, null, null,
                            null, null, null, true);
    } catch (e) {
      try { return ui.getFileData(true); } catch (e2) { return null; }
    }
  }

  function markClean(ui) {
    try {
      if (ui.editor && typeof ui.editor.setModified === 'function') {
        ui.editor.setModified(false);
      }
      var file = ui.getCurrentFile && ui.getCurrentFile();
      if (file) {
        if (typeof file.setModified === 'function') file.setModified(false);
        else file.modified = false;
        if (typeof file.savingFile !== 'undefined') file.savingFile = false;
      }
      if (typeof ui.updateDocumentTitle === 'function') ui.updateDocumentTitle();
    } catch (e) {
      console.warn('[awm] markClean failed', e);
    }
  }

  // -- loading ------------------------------------------------------------

  function applyXml(ui, xml, rev) {
    // Preserve the page and viewport across a reload, so a push from someone
    // else's merge does not throw away where the user was looking.
    loading = true;
    var pageIdx = -1, scale = null, tx = 0, ty = 0;
    try {
      if (ui.pages && ui.currentPage) pageIdx = ui.pages.indexOf(ui.currentPage);
      var v = ui.editor && ui.editor.graph && ui.editor.graph.view;
      if (v) { scale = v.scale; tx = v.translate.x; ty = v.translate.y; }
    } catch (e) { /* best effort */ }

    try {
      if (ui.editor && ui.editor.setModified) ui.editor.setModified(false);
      var name = (target && target.save ? target.save : 'diagram.drawio')
        .split('/').pop();
      if (typeof LocalFile !== 'undefined' && typeof ui.fileLoaded === 'function') {
        ui.fileLoaded(new LocalFile(ui, xml, name, false));
      } else if (typeof mxUtils !== 'undefined') {
        var doc = mxUtils.parseXml(xml);
        var root = doc.documentElement;
        if (root.nodeName === 'mxfile') {
          var d = root.getElementsByTagName('diagram')[0];
          var m = d && d.getElementsByTagName('mxGraphModel')[0];
          if (m) root = m;
        }
        ui.editor.setGraphXml(root);
      }
      try {
        if (pageIdx >= 0 && ui.pages && ui.pages[pageIdx] &&
            typeof ui.selectPage === 'function') {
          ui.selectPage(ui.pages[pageIdx]);
        }
        var v2 = ui.editor && ui.editor.graph && ui.editor.graph.view;
        if (v2 && scale !== null) v2.scaleAndTranslate(scale, tx, ty);
      } catch (e) { /* best effort */ }

      if (rev !== undefined) baseRev = rev;
      lastXml = snapshot(ui);
      markClean(ui);
    } catch (e) {
      console.warn('[awm] load failed', e);
      status('Load failed: ' + e.message);
    } finally {
      loading = false;
    }
  }

  function load(ui) {
    // `read` covers a live diagram and a checkout alike, so the tab does not
    // care which it was pointed at.
    var announce = HANDLE
      ? call('status', { handle: HANDLE }).then(function (s) {
          return { save: s.save, handle: HANDLE };
        })
      : call('editor_open', { save: SAVE, tab: TAB }).then(function (info) {
          return { save: info.save, handle: null };
        });

    return announce.then(function (t) {
      target = t;
      return call('read', HANDLE ? { handle: HANDLE } : { save: t.save });
    }).then(function (body) {
      applyXml(ui, body.xml, body.rev);
    }).catch(function (e) {
      console.error('[awm] could not open diagram', e);
      status('Could not open ' + (SAVE || HANDLE) + ': ' + e.message);
    });
  }

  // -- saving -------------------------------------------------------------

  function save(ui, xml, reason) {
    if (inflight || !target) return Promise.resolve(false);
    inflight = true;
    return call('editor_save', {
      save: target.save,
      xml: xml,
      base_rev: baseRev,
      tab: TAB,
      handle: target.handle,
    }).then(function (result) {
      if (result.accepted) {
        baseRev = result.rev;
        lastXml = xml;
        markClean(ui);
        status(reason === 'flush' ? 'Saved (merge pending)' : 'All changes saved');
        return true;
      }
      if (result.reason === 'lease') {
        // A merge is landing. Keep the edits; the push that follows will
        // reconcile and we will save again after it.
        holding = true;
        status('Waiting for a merge to land…');
        return false;
      }
      if (result.reason === 'stale') {
        onStale(ui, result.rev);
        return false;
      }
      status('Save rejected: ' + (result.note || result.reason));
      return false;
    }).catch(function (e) {
      console.warn('[awm] save failed', e);
      status('Save failed: ' + e.message);
      return false;
    }).finally(function () {
      inflight = false;
    });
  }

  function onStale(ui, liveRev) {
    // The diagram moved under us. Reloading would discard whatever is on
    // screen, so only do it when there is nothing to discard.
    var current = snapshot(ui);
    if (current === lastXml) {
      status('Diagram changed elsewhere — reloading…');
      pull(ui);
      return;
    }
    status('This diagram changed elsewhere and you have unsaved edits. ' +
           'Copy anything you need, then reload to get the current version.');
    console.warn('[awm] stale tab with local edits; live rev is', liveRev);
  }

  function pull(ui) {
    if (!target) return;
    call('read', target.handle ? { handle: target.handle } : { save: target.save })
      .then(function (body) { applyXml(ui, body.xml, body.rev); })
      .catch(function (e) { console.warn('[awm] pull failed', e); });
  }

  // -- live coordination ---------------------------------------------------

  function subscribe(ui) {
    if (!target) return;
    var scheme = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
    var url = scheme + window.location.host +
              '/svc/drawio/emit/drawio:' + target.save;
    var ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      console.warn('[awm] emit socket unavailable', e);
      return;
    }
    ws.onmessage = function (event) {
      var msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      var payload = msg.payload || msg;
      if (payload.type === 'flush') {
        holding = true;
        var xml = snapshot(ui);
        var done = function () {
          call('editor_flush_ack', { save: target.save, tab: TAB })
            .catch(function () { /* the merge's timeout covers this */ });
        };
        if (xml && xml !== lastXml) save(ui, xml, 'flush').then(done);
        else done();
      } else if (payload.type === 'push') {
        holding = false;
        if (payload.rev && payload.rev === baseRev) return;
        var current = snapshot(ui);
        if (current === lastXml) pull(ui);
        else status('The diagram was updated — reload to see the merged version.');
      }
    };
    ws.onclose = function () {
      // The gateway restarts; without this the tab goes quietly deaf to
      // flush requests and starts losing merges.
      setTimeout(function () { subscribe(ui); }, 2000);
    };
  }

  // -- the loop ------------------------------------------------------------

  function tick() {
    var ui = window.__drawioUi;
    if (!ui || !ui.editor || loading) return;
    if (!attached) {
      attached = true;
      load(ui).then(function () {
        subscribe(ui);
        console.log('[awm] editing', target && target.save,
                    target && target.handle ? '(checkout ' + target.handle + ')' : '');
      });
      return;
    }
    if (!target || holding || inflight) return;
    var xml = snapshot(ui);
    if (!xml || xml === lastXml) return;
    save(ui, xml, 'edit');
  }

  window.addEventListener('beforeunload', function () {
    if (!target || target.handle) return;
    // Fire-and-forget: keepalive lets it outlive the page so the reception
    // page's "open editors" count does not drift upward forever.
    try {
      fetch(SVC + 'editor_close', {
        method: 'POST', keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ save: target.save, tab: TAB }),
      });
    } catch (e) { /* best effort */ }
  });

  setInterval(tick, POLL_MS);
})();
