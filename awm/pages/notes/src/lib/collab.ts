// Live collaboration transport for one open note.
//
// The server holds the authoritative copy of a note in an in-memory "room"
// (versioned); it 3-way-merges each client's edit and fans the merged result
// out on the pub/sub topic `note:<id>`. This client speaks that protocol with a
// Differential-Sync-style shadow so un-acked local keystrokes are never
// clobbered by an incoming remote change:
//
//   • `shadow`  — the last content we and the server agreed on (at `shadowV`).
//   • the editor's live `text` is whatever the user has typed since.
//   • integrate(server): patch = diff(shadow, server); apply that patch onto
//     `text`. Because `shadow` is the common ancestor, this folds in ONLY the
//     remote delta — our own local edits stay put — and `text` converges.
//
// Transport: edits POST to `collab_edit` (fn); updates arrive on a WS
// subscription to `/svc/notes/emit/note:<id>`. Both carry {version, content}.

import { svc, toWsUrl } from '@awm/client';
import { diff_match_patch } from 'diff-match-patch';

const dmp = new diff_match_patch();
dmp.Match_Threshold = 0.5;
dmp.Patch_DeleteThreshold = 0.5;

export interface CollabHandlers {
  // Adopt authoritative text into the editor (caret-preserving is the app's job).
  onText: (text: string) => void;
  // Connection lifecycle, for a subtle "live" indicator.
  onStatus?: (live: boolean) => void;
  // Sync lifecycle of the local text vs the server room, for the save-state
  // indicator: 'saving' while an edit is in flight, 'synced' once the server has
  // confirmed our latest text, 'offline' when a send failed (edits remain safe
  // in the local draft). This is what lets the status read "Synced" for a solo
  // editor whose edits round-trip with no remote change.
  onSync?: (state: 'saving' | 'synced' | 'offline') => void;
}

const rid = () => Math.random().toString(36).slice(2, 10);

export function createCollab(h: CollabHandlers) {
  const clientId = rid();
  let ws: WebSocket | null = null;
  let noteId: string | null = null;

  let shadow = '';          // last content agreed with the server
  let shadowV = -1;         // its version
  let text = '';            // the editor's current content (fed via update())
  let sending = false;      // one edit in flight at a time
  let pendingSend = false;  // more edits arrived while sending
  let sendTimer: ReturnType<typeof setTimeout> | null = null;

  /** Fold a server-authoritative content@version into the local editor. */
  function integrate(serverText: string, version: number) {
    if (version <= shadowV) return;         // stale / our own echo already handled
    const patches = dmp.patch_make(shadow, serverText);
    const [merged] = dmp.patch_apply(patches, text) as [string, boolean[]];
    shadow = serverText;
    shadowV = version;
    if (merged !== text) {
      text = merged;
      h.onText(merged);
    }
    // Local edits the server hasn't seen yet → push them.
    if (merged !== serverText) scheduleSend(0);
  }

  async function doSend() {
    if (sending || noteId === null) return;
    if (text === shadow) { h.onSync?.('synced'); return; }  // nothing new — already agreed
    sending = true;
    h.onSync?.('saving');
    const mine = text;
    const base = shadowV;
    // Optimistically treat `mine` as agreed, so integrating the reply (which
    // echoes `mine` back merged with any remote change) applies only the remote
    // delta rather than re-applying our own edit.
    shadow = mine;
    let failed = false;
    try {
      const res = await svc('notes').fn<{ version: number; content: string; changed: boolean }>(
        'collab_edit', { id: noteId, base_version: base, content: mine, client_id: clientId },
      );
      integrate(res.content, res.version);
    } catch {
      shadow = ''; shadowV = -1;            // force a resync on next contact
      failed = true;
    } finally {
      sending = false;
      if (pendingSend || text !== shadow) {
        pendingSend = false;
        scheduleSend(0);
      } else if (failed) {
        h.onSync?.('offline');              // edits stay safe in the local draft
      } else {
        // Server confirmed our latest text and nothing new is queued → synced.
        h.onSync?.('synced');
      }
    }
  }

  function scheduleSend(delay = 180) {
    if (sending) { pendingSend = true; return; }
    if (sendTimer) clearTimeout(sendTimer);
    sendTimer = setTimeout(() => { sendTimer = null; void doSend(); }, delay);
  }

  function openSocket(id: string) {
    const sock = new WebSocket(toWsUrl(svc('notes').url(`/emit/note:${id}`)));
    sock.onopen = () => { if (ws === sock) h.onStatus?.(true); };
    sock.onclose = () => {
      if (ws !== sock) return;
      h.onStatus?.(false);
      // Reconnect while this note is still open (the room fans out live edits).
      if (noteId === id) setTimeout(() => { if (noteId === id) openSocket(id); }, 1000);
    };
    sock.onerror = () => { try { sock.close(); } catch { /* ignore */ } };
    sock.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      let msg: any;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (typeof msg?.content !== 'string' || typeof msg?.version !== 'number') return;
      if (msg.origin === clientId) { if (msg.version > shadowV) shadowV = msg.version; return; }
      integrate(msg.content, msg.version);
    };
    ws = sock;
  }

  return {
    /** Join a note's room. `content` is the editor's current text (normally the
     *  live copy, since notes.get returns room content; may be ahead of the
     *  snapshot in the create-then-keep-typing case). */
    async join(id: string, content: string) {
      this.leave();
      noteId = id;
      text = content;
      shadow = content;
      shadowV = 0;
      try {
        const snap = await svc('notes').fn<{ version: number; content: string }>('collab_open', { id });
        // The snapshot is the common ancestor: our local `text` is edits on top
        // of it. Don't patch backwards — just set the base and push any diff.
        shadow = snap.content;
        shadowV = snap.version;
      } catch { /* room open failed — stay local, still usable */ }
      if (text !== shadow) scheduleSend(0);
      openSocket(id);
    },

    /** Feed the editor's latest text after a user keystroke. */
    update(next: string) {
      if (noteId === null) return;
      text = next;
      scheduleSend();
    },

    /** Flush any pending edit immediately (e.g. before switching notes). */
    async flush() {
      if (sendTimer) { clearTimeout(sendTimer); sendTimer = null; }
      await doSend();
    },

    leave() {
      if (sendTimer) { clearTimeout(sendTimer); sendTimer = null; }
      const sock = ws;
      ws = null; noteId = null;
      try { sock?.close(); } catch { /* ignore */ }
      h.onStatus?.(false);
    },

    get clientId() { return clientId; },
  };
}
