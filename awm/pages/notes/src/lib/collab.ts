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
// `shadow` is therefore load-bearing and must ALWAYS name text the server has
// actually seen. A failed send rolls it back to its pre-send value; discarding
// it makes the next remote update diff as "insert the whole document", which
// appends the server's copy onto the user's.
//
// Transport: edits POST to `collab_edit` (fn); updates arrive on a WS
// subscription to `/svc/notes/emit/note:<id>`. Both carry {version, content}.

import { HttpError, svc, toWsUrl } from '@awm/client';
import { diff_match_patch } from 'diff-match-patch';

const dmp = new diff_match_patch();
dmp.Match_Threshold = 0.5;
dmp.Patch_DeleteThreshold = 0.5;

/** Transport health, as a function of intent (is a note joined?) and reality
 *  (is a socket open?) — never a suppression flag, so the UI can distinguish
 *  "not collaborating" from "collaboration is broken". */
export type LinkState =
  | 'idle'        // no note joined — render nothing
  | 'connecting'  // joined, socket not open (yet / again)
  | 'live'        // socket open
  | 'down';       // joined, and the socket has stayed shut past the grace period

export type SyncState = 'saving' | 'synced' | 'offline';

export interface CollabHandlers {
  // Adopt authoritative text into the editor (caret-preserving is the app's job).
  onText: (text: string) => void;
  // Connection lifecycle, for the status-bar link pill.
  onLink?: (state: LinkState) => void;
  // Sync lifecycle of the local text vs the server room, for the save-state
  // indicator: 'saving' while an edit is in flight, 'synced' once the server has
  // confirmed our latest text, 'offline' when a send failed (edits remain safe
  // in the local draft). The note id is carried because a send that completes
  // after the user switched notes must not be applied to the new one.
  onSync?: (state: SyncState, noteId: string) => void;
}

/** How long a closed socket may stay amber before the pill goes red. Long
 *  enough that a note switch or a one-second blip never flashes "Disconnected". */
const DOWN_GRACE_MS = 1500;
/** Socket reconnect ladder. Jittered: every tab on a note drops at the same
 *  instant when the service restarts, and they must not all retry in lockstep. */
const RECONNECT_MIN = 500;
const RECONNECT_MAX = 10_000;
/** Failed-send ladder, so an unreachable server is polled rather than hammered. */
const SEND_BACKOFF_MIN = 600;
const SEND_BACKOFF_MAX = 10_000;
/** Floor between snapshot probes used as a liveness check. */
const PROBE_MIN_MS = 4000;

const rid = () => Math.random().toString(36).slice(2, 10);

/** Is this failure the link's fault? Letting one genuinely-broken note flap the
 *  connection indicator forever is worse than missing a real outage for one
 *  keystroke, so this is deliberately narrow.
 *
 *  Read the gateway's codes before widening it: `hub/proxy.py` wraps **every**
 *  service-side error envelope as a **502**, so `{"error":"no such note"}` from
 *  a perfectly healthy service arrives as 502 — it is an application error here,
 *  not a dead upstream. The transport-shaped ones are 503 (the service's control
 *  channel is not open) and 504 (it never replied). A *stopped* service is a 404
 *  and is deliberately absent: its socket has already closed, and the close is
 *  what drives the pill red. */
function isLinkError(e: unknown): boolean {
  if (e instanceof HttpError) return e.status === 0 || e.status === 503 || e.status === 504;
  return e instanceof TypeError;   // fetch() rejects with TypeError on network failure
}

export function createCollab(h: CollabHandlers) {
  const clientId = rid();
  let ws: WebSocket | null = null;
  let noteId: string | null = null;
  // Bumped on every join/leave/destroy. Every timer callback and every `await`
  // continuation re-checks it: without this a socket whose close fires *after* a
  // leave writes a stale link state, and a send that fails after a note switch
  // rolls the *new* note's shadow back to the old note's text.
  let gen = 0;

  let shadow = '';          // last content agreed with the server
  let shadowV = -1;         // its version
  let text = '';            // the editor's current content (fed via update())
  let sending = false;      // one edit in flight at a time
  let pendingSend = false;  // more edits arrived while sending
  let sendTimer: ReturnType<typeof setTimeout> | null = null;
  let sendBackoff = SEND_BACKOFF_MIN;
  let nextSendAt = 0;       // absolute floor, so typing can't outrun the backoff

  let link: LinkState = 'idle';
  let reconnTimer: ReturnType<typeof setTimeout> | null = null;
  let downTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnBackoff = RECONNECT_MIN;
  let lastProbe = 0;

  // ---- link state ---------------------------------------------------------

  function setLink(next: LinkState) {
    if (link === next) return;
    link = next;
    h.onLink?.(next);
  }

  function armDown() {
    if (downTimer) return;
    downTimer = setTimeout(() => {
      downTimer = null;
      if (noteId !== null && link === 'connecting') setLink('down');
    }, DOWN_GRACE_MS);
  }
  function clearDown() {
    if (downTimer) { clearTimeout(downTimer); downTimer = null; }
  }

  /** A network-shaped failure on the fn channel. The socket may still *claim*
   *  to be open — one that slept through a suspend does, and the gateway's event
   *  relay is write-only toward the browser so nothing else would notice — so
   *  bounce it and let the reconnect ladder prove the link one way or the other. */
  function linkTrouble() {
    const id = noteId;
    if (id === null) return;
    if (link === 'live') {
      setLink('connecting');
      armDown();
      const sock = ws;
      ws = null;
      try { sock?.close(); } catch { /* its onclose sees ws !== sock and defers */ }
      retry(id);
    } else if (link === 'connecting') {
      armDown();
    }
  }

  // ---- content ------------------------------------------------------------

  /** Fold a server-authoritative content@version into the local editor.
   *  `force` bypasses the staleness guard — see resync(). */
  function integrate(serverText: string, version: number, force = false) {
    if (!force && version <= shadowV) return;   // stale / our own echo already handled
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
    const id = noteId;
    const myGen = gen;
    if (text === shadow) { h.onSync?.('synced', id); return; }  // nothing new — already agreed
    sending = true;
    h.onSync?.('saving', id);
    const mine = text;
    const base = shadowV;
    // Optimistically treat `mine` as agreed, so integrating the reply (which
    // echoes `mine` back merged with any remote change) applies only the remote
    // delta rather than re-applying our own edit. Rolled back if the send fails.
    const prevShadow = shadow;
    const prevShadowV = shadowV;
    shadow = mine;
    let failed = false;
    let err: unknown = null;
    try {
      const res = await svc('notes').fn<{ version: number; content: string; changed: boolean }>(
        'collab_edit', { id, base_version: base, content: mine, client_id: clientId },
      );
      if (gen === myGen) integrate(res.content, res.version);
    } catch (e) {
      failed = true;
      err = e;
      if (gen === myGen) { shadow = prevShadow; shadowV = prevShadowV; }
    } finally {
      sending = false;
      if (gen !== myGen) {
        // Joined elsewhere while this was in flight — the result belongs to a
        // note that is no longer open. Drop it; don't touch the new note's state.
      } else if (failed) {
        if (isLinkError(err)) linkTrouble();
        h.onSync?.('offline', id);          // edits stay safe in the local draft
        pendingSend = false;
        nextSendAt = Date.now() + sendBackoff;
        scheduleSend(sendBackoff);
        sendBackoff = Math.min(sendBackoff * 2, SEND_BACKOFF_MAX);
      } else {
        sendBackoff = SEND_BACKOFF_MIN;
        nextSendAt = 0;
        if (pendingSend || text !== shadow) {
          pendingSend = false;
          scheduleSend(0);
        } else {
          // Server confirmed our latest text and nothing new is queued → synced.
          h.onSync?.('synced', id);
        }
      }
    }
  }

  function scheduleSend(delay = 180) {
    if (sending) { pendingSend = true; return; }
    // Typing must not reset the failure backoff to the 180ms keystroke debounce,
    // or a disconnected editor streams a request per typing pause.
    const wait = Math.max(delay, nextSendAt - Date.now());
    if (sendTimer) clearTimeout(sendTimer);
    sendTimer = setTimeout(() => { sendTimer = null; void doSend(); }, wait);
  }

  // ---- socket -------------------------------------------------------------

  function openSocket(id: string, reopen: boolean) {
    const myGen = gen;
    let sock: WebSocket;
    try {
      sock = new WebSocket(toWsUrl(svc('notes').url(`/emit/note:${id}`)));
    } catch {
      retry(id);
      return;
    }
    ws = sock;
    sock.onopen = () => {
      if (gen !== myGen || ws !== sock) return;
      reconnBackoff = RECONNECT_MIN;
      clearDown();
      setLink('live');
      // Nothing else schedules work when the socket comes back, so without this
      // the editor sits on "Saved locally" until the next keystroke.
      if (reopen) void resync();
    };
    sock.onclose = () => {
      if (gen !== myGen || ws !== sock) return;
      ws = null;
      // Once down, stay down until a socket actually opens — deriving the state
      // fresh on each retry strobes the pill red/amber at the backoff rate.
      if (link !== 'down') { setLink('connecting'); armDown(); }
      retry(id);
    };
    sock.onerror = () => { try { sock.close(); } catch { /* ignore */ } };
    sock.onmessage = (ev) => {
      if (gen !== myGen || ws !== sock) return;
      if (typeof ev.data !== 'string') return;
      let msg: any;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (typeof msg?.content !== 'string' || typeof msg?.version !== 'number') return;
      if (msg.origin === clientId) { if (msg.version > shadowV) shadowV = msg.version; return; }
      integrate(msg.content, msg.version);
    };
  }

  function retry(id: string) {
    if (noteId !== id || reconnTimer) return;
    const wait = reconnBackoff * (0.7 + Math.random() * 0.6);
    const myGen = gen;
    reconnTimer = setTimeout(() => {
      reconnTimer = null;
      if (gen === myGen && noteId === id) openSocket(id, true);
    }, wait);
    reconnBackoff = Math.min(reconnBackoff * 2, RECONNECT_MAX);
  }

  /** Re-fetch the room snapshot, fold it in, and push whatever the server hasn't
   *  seen. Runs when a socket re-opens, and (throttled) as a liveness probe.
   *
   *  The integrate is FORCED past the staleness guard on purpose: the service
   *  rebuilds a note's room from disk **at version 0** when it restarts, which is
   *  the usual reason the socket dropped. A client still holding version 42 would
   *  otherwise discard every later update as stale and sit on a healthy-looking
   *  green pill while being completely deaf. */
  async function resync(): Promise<void> {
    const id = noteId;
    if (id === null) return;
    const myGen = gen;
    lastProbe = Date.now();
    try {
      const snap = await svc('notes').fn<{ version: number; content: string }>('collab_open', { id });
      if (gen !== myGen) return;
      integrate(snap.content, snap.version, true);
      if (text !== shadow) scheduleSend(0);
      else h.onSync?.('synced', id);
    } catch (e) {
      if (gen !== myGen) return;
      if (isLinkError(e)) linkTrouble();
    }
  }

  // ---- wake-ups -----------------------------------------------------------

  /** The browser says the network is back, or the tab was brought forward.
   *  Skip the remaining backoff — the user is looking at it now. */
  function onWake() {
    const id = noteId;
    if (id === null) return;
    if (link === 'live') {
      // A socket that survived a suspend is open but dead and there is no ping
      // to detect it, so use the snapshot call as a (throttled) probe.
      if (Date.now() - lastProbe >= PROBE_MIN_MS) void resync();
      return;
    }
    if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }
    reconnBackoff = RECONNECT_MIN;
    if (!ws) openSocket(id, true);
  }
  function onVisible() {
    if (document.visibilityState === 'visible') onWake();
  }
  window.addEventListener('online', onWake);
  document.addEventListener('visibilitychange', onVisible);

  // ---- lifecycle ----------------------------------------------------------

  function leave() {
    gen++;
    if (sendTimer) { clearTimeout(sendTimer); sendTimer = null; }
    if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }
    clearDown();
    const sock = ws;
    ws = null; noteId = null;
    sending = false; pendingSend = false;
    sendBackoff = SEND_BACKOFF_MIN; nextSendAt = 0;
    reconnBackoff = RECONNECT_MIN;
    try { sock?.close(); } catch { /* ignore */ }
    // A deliberate leave is not a failure: idle renders nothing.
    setLink('idle');
  }

  /** Join a note's room. `content` is the editor's current text (normally the
   *  live copy, since notes.get returns room content; may be ahead of the
   *  snapshot in the create-then-keep-typing case). */
  async function join(id: string, content: string) {
    leave();
    const myGen = gen;
    noteId = id;
    text = content;
    shadow = content;
    shadowV = 0;
    setLink('connecting');
    armDown();
    try {
      const snap = await svc('notes').fn<{ version: number; content: string }>('collab_open', { id });
      if (gen !== myGen) return;
      // The snapshot is the common ancestor: our local `text` is edits on top
      // of it. Don't patch backwards — just set the base and push any diff.
      shadow = snap.content;
      shadowV = snap.version;
    } catch {
      if (gen !== myGen) return;    // room open failed — stay local, still usable
    }
    if (gen !== myGen) return;
    if (text !== shadow) scheduleSend(0);
    openSocket(id, false);
  }

  return {
    join,
    leave,

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

    /** Tear down for good: leave, and drop the window listeners. */
    destroy() {
      leave();
      window.removeEventListener('online', onWake);
      document.removeEventListener('visibilitychange', onVisible);
    },

    get clientId() { return clientId; },
    get link(): LinkState { return link; },
  };
}
