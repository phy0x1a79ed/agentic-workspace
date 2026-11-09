// Shared by the board, Ops and the bot button. The installer puts this text in front of each page
// script, so every script is one self-contained unit with no module loading to go wrong.
//
// Every press is a fetch to alcor's API with the device credential (queue.md R64 item 5). The only
// frames from alcor are the sign-in and the approval modal.

// Overlays, toasts and menus live on the document, outside the page's own container.
if (!document.getElementById("awm-common-style")) {
  document.head.append(el0("style", "awm-common-style", AWM_COMMON_CSS));
}
function el0(tag, id, text) { const e = document.createElement(tag); e.id = id; e.textContent = text; return e; }

const SWARM_BOARD_ID = "awmSwarmBoard";
const SWARM_ROSTER_ID = "awmSwarmRoster";
const TASKS_ID = "awmSwarmTasks";

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k);
  return e;
}

// Display text for a value that may be absent: never the word null.
const show = (v) => (v === null || v === undefined ? "" : String(v));

async function swarmOrigin() {
  const board = await api.getNote(SWARM_BOARD_ID);
  return ((board && board.getLabelValue("alcorOrigin")) || "").replace(/\/$/, "");
}

async function swarmRoster() {
  try {
    const note = await api.getNote(SWARM_ROSTER_ID);
    const blob = note && (await note.getBlob());
    return JSON.parse((blob && blob.content) || "{}");
  } catch (e) {
    return {};
  }
}

// -- overlay frames from alcor: sign-in and the approval modal ---------------------------------

// A frame from alcor over the page. onMessage gets alcor's postMessage and returns true to close;
// onClose runs once however the overlay ends.
function overlay(title, src, origin, onMessage, onClose) {
  const frame = el("iframe", { src, title, allow: "publickey-credentials-get; publickey-credentials-create",
                               class: "awm-frame" });
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    window.removeEventListener("message", listener);
    shade.remove();
    onClose();
  };
  const listener = (ev) => {
    if (ev.origin !== origin || ev.source !== frame.contentWindow) return;
    if (onMessage(ev.data || {}) === true) close();
  };
  const shade = el("div", { class: "awm-shade", role: "dialog", "aria-label": title,
                            onclick: (ev) => { if (ev.target === shade) close(); } },
    el("div", { class: "awm-dialog" },
      el("button", { class: "awm-x", type: "button", title: "Close", onclick: close }, "×"), frame));
  window.addEventListener("message", listener);
  document.body.append(shade);
}

function signIn(origin) {
  return new Promise((resolve) => {
    let ok = false;
    overlay("Sign in", `${origin}/signin?frame=1`, origin,
            (m) => { if (m.type === "awm-signed-in") { ok = true; return true; } return false; },
            () => resolve(ok));
  });
}

// Resolves true when alcor says the request was approved.
function approvalModal(origin, requestId) {
  return new Promise((resolve) => {
    let ok = false;
    overlay("Approve", `${origin}/modal/approve/${encodeURIComponent(requestId)}`, origin,
            (m) => { if (m.type === "awm-approval" && m.request_id === requestId) { ok = !!m.ok; return true; } return false; },
            () => resolve(ok));
  });
}

function twofaModal(origin, count) {
  return new Promise((resolve) => {
    let ok = false;
    overlay("2fa", `${origin}/modal/twofa?count=${encodeURIComponent(count)}`, origin,
            (m) => { if (m.type === "awm-approval") { ok = !!m.ok; return true; } return false; },
            () => resolve(ok));
  });
}

// -- the API -------------------------------------------------------------------------------------

class SwarmError extends Error {}

// POST (or GET) to alcor with the device credential. A 401 or 403 opens the sign-in frame once and
// repeats the call; a refused request with no answer at all is reported as unreachable.
async function swarmCall(origin, path, body, method = "POST") {
  if (!origin) throw new SwarmError("The board has no #alcorOrigin label: run board_install.");
  for (let attempt = 0; attempt < 2; attempt++) {
    let res;
    try {
      res = await fetch(origin + path, {
        method, credentials: "include", headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      if (attempt === 0 && (await signIn(origin))) continue;
      throw new SwarmError("alcor did not answer, or you are not signed in.");
    }
    if ((res.status === 401 || res.status === 403) && attempt === 0) {
      if (await signIn(origin)) continue;
      throw new SwarmError("Not signed in.");
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* an empty answer */ }
    if (!res.ok) throw new SwarmError(show(data && (data.error || data.message)) || `alcor answered ${res.status}`);
    return data || {};
  }
  throw new SwarmError("Not signed in.");
}

function toast(text, bad = false) {
  const t = el("div", { class: "awm-toast" + (bad ? " bad" : "") }, text);
  document.body.append(t);
  // Script-driven, because Trilium's motion setting strips CSS animations.
  t.animate([{ opacity: 1, offset: 0.85 }, { opacity: 0 }], { duration: 4000 }).finished.then(() => t.remove());
}

// -- the node dropdown (queue.md R63 item 1) ----------------------------------------------------------

// A <select>-like popup of the nodes that run agents, fewest sessions in 7 days first.
async function nodeChoices() {
  const roster = await swarmRoster();
  const by = Object.fromEntries((roster.nodes || []).map((n) => [n.name, n]));
  return (roster.agent_nodes || []).map((name) => ({ name, sessions: (by[name] || {}).sessions_7d || 0 }));
}

// Trilium tells the frontend which entities changed; the page re-renders on that, never on a clock.
// `relevant(loadResults)` keeps an edit elsewhere in the vault from costing a render.
function entitiesChanged(box, relevant, handler) {
  const ctx = window.glob && window.glob.appContext;
  if (!ctx) return;
  let queued = false;
  const listener = (data) => {
    if (!box.isConnected) { ctx.removeHandler("entitiesReloaded", listener); return; }
    const lr = data && data.loadResults;
    if (lr && !relevant(lr) || queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; handler(); });
  };
  ctx.registerHandler("entitiesReloaded", listener);
}

const CARD_LABELS = ["section", "status", "peer", "taskId", "pendingApprovals", "historyAt", "updatedAt",
                     "queueChangedAt"];
const cardsChanged = (lr) => lr.getBranchRows().length > 0
  || lr.getAttributeRows().some((r) => CARD_LABELS.includes(r.name))
  || lr.isNoteContentReloaded(SWARM_ROSTER_ID);
const rosterChanged = (lr) => lr.isNoteContentReloaded(SWARM_ROSTER_ID) || lr.isNoteReloaded(SWARM_ROSTER_ID);
