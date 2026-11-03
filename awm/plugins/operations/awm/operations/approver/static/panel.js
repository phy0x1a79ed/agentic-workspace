// The approver's pages: the action panel with its 2fa strip, the embeddable button,
// enrolment and portal login. Each page names itself in <body data-page>.

import { act, assertion, authorize, enrol, loadState, Locked, post, resume, serverKeys, signedGet,
         unlock } from "/static/approver.js";

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) el.append(k);
  return el;
}

function say(text, kind = "info") {
  const box = $("#status");
  if (box) { box.textContent = text; box.dataset.kind = kind; }
}

async function run(label, fn) {
  say(`${label}…`);
  try {
    const out = await fn();
    if (typeof out === "string") { say(out, "ok"); return out; }
    const failed = out && out.ok === false;
    say(failed ? `${label}: ${out.error}` : `${label}: done`, failed ? "error" : "ok");
    return out;
  } catch (e) {
    if (e instanceof Locked) { renderLock(); say(e.message, "error"); return null; }
    say(`${label}: ${e.message}`, "error");
    return null;
  }
}

function renderLock() {
  const box = $("#lock");
  if (!box) return;
  box.replaceChildren(h("p", {}, "This device is locked. Its chain no longer matches alcor's."),
    h("button", { onclick: () => run("Unlock", async () => { await unlock(); box.replaceChildren(); }) },
      "Unlock with passkey"));
}

// -- the 2fa strip (R137): always at the top of the panel ---------------------------

async function renderTwofa() {
  const box = $("#twofa");
  let state;
  try { state = await signedGet("/api/twofa"); } catch (e) { box.replaceChildren(h("p", { class: "muted" }, `2fa: ${e.message}`)); return; }
  const devices = Array.isArray(state.devices) ? state.devices : (state.devices?.devices || []);
  const pending = state.pending || {};
  box.replaceChildren(...devices.map((d) => {
    const name = typeof d === "string" ? d : d.name;
    const held = Array.isArray(pending[name]) ? pending[name] : [];
    return h("div", { class: "twofa-device" },
      h("strong", {}, name),
      ...[1, 2, 4].map((n) => h("button", { "data-burst": n,
        onclick: () => run(`Arm ${n} on ${name}`, () => act("twofa_burst", { device: name, count: n })) },
        `Arm ${n}`)),
      h("button", { onclick: () => run(`Approve all on ${name}`, () => act("twofa_approve_all", { device: name })) },
        "Approve all 5 min"),
      h("ul", { class: "held" }, ...held.map((p) => h("li", {},
        `${p.app || p.title || "push"} ${p.urgid}`,
        h("button", { class: "yes", onclick: () => run("Approve", () => act("twofa_approve", { device: name, urgid: p.urgid })) }, "Approve"),
        h("button", { class: "no", onclick: () => run("Deny", () => act("twofa_deny", { device: name, urgid: p.urgid })) }, "Deny")))));
  }));
}

// -- the card the board selected ----------------------------------------------------

let selected = null;   // {task_id, body_sha256, preview} as the board sent it
let peers = [];

function peerSelect() {
  return h("select", { class: "peer" }, ...peers.map((p) => h("option", { value: p }, p)));
}

async function renderCard() {
  const box = $("#card");
  if (!selected) { box.replaceChildren(h("p", { class: "muted" }, "Select a card on the board.")); return; }
  let card;
  try { card = await signedGet(`/api/card/${encodeURIComponent(selected.task_id)}`); } catch (e) {
    box.replaceChildren(h("p", { class: "error" }, e.message)); return;
  }
  const id = card.task_id;
  const buttons = [];
  if (card.section === "proposed") {
    const sel = peerSelect();
    buttons.push(h("label", {}, "Spawn on ", sel),
      h("button", { onclick: () => run("Spawn", () => act("spawn", { task_id: id, peer: sel.value,
                                                                     body_sha256: selected.body_sha256 || null })) }, "Spawn agent"),
      h("button", { onclick: () => run("Reject", () => act("reject", { task_id: id })) }, "Reject"));
  }
  if (card.section === "working" || card.section === "done") {
    const sel = peerSelect();
    const title = h("input", { placeholder: "New task title" });
    buttons.push(title, sel, h("button", { onclick: () => run("New task", () =>
      act("new_from", { task_id: id, peer: sel.value, title: title.value })) }, "New task from this"));
  }
  if (card.kind === "approval" && card.section === "needs_you") {
    const rid = card.meta && card.meta.request_id;
    let req = null;
    try { req = await signedGet(`/api/request/${encodeURIComponent(rid)}`); } catch (e) { /* shown below */ }
    if (req) buttons.push(h("pre", { class: "request" }, JSON.stringify({ kind: req.kind, node: req.node,
      minutes: req.minutes, reason: req.reason, requester: req.requester_session, payload: req.payload }, null, 1)));
    buttons.push(h("button", { class: "yes", onclick: () => run("Approve", () => act("approve", { request_id: rid })) }, "Approve"),
                 h("button", { class: "no", onclick: () => run("Deny", () => act("deny", { request_id: rid })) }, "Deny"));
  } else if (card.section !== "done" && card.section !== "proposed") {
    buttons.push(h("button", { onclick: () => run("Close", () => act("close", { task_id: id })) }, "Close"),
                 h("button", { onclick: () => run("Reject", () => act("reject", { task_id: id })) }, "Reject"));
  }
  box.replaceChildren(
    h("h2", {}, card.title),
    h("p", { class: "meta" }, `${id} · ${card.section}${card.status ? " · " + card.status : ""}${card.peer ? " · " + card.peer : ""}`),
    selected.preview && card.section === "proposed"
      ? h("blockquote", {}, selected.preview, h("br"), h("small", {}, `sha256 ${selected.body_sha256 || "none"}`)) : null,
    h("div", { class: "actions" }, ...buttons),
    windowForm(id));
}

const WINDOW_KINDS = ["close", "reject", "spawn", "new_from", "note_new", "approve", "deny",
                      "twofa_burst", "twofa_approve", "twofa_deny", "twofa_approve_all"];

function windowForm(cardId) {
  const kinds = WINDOW_KINDS.map((k) => h("label", {}, h("input", { type: "checkbox", value: k }), k));
  const scope = h("select", {}, h("option", { value: "card" }, "this card"), h("option", { value: "any" }, "any card"));
  const minutes = h("input", { type: "number", min: 1, max: 720, value: 60 });
  return h("details", { class: "window" }, h("summary", {}, "Open a window"),
    h("div", {}, ...kinds), h("label", {}, "for ", scope), h("label", {}, " minutes ", minutes),
    h("button", { onclick: () => run("Open window", () => authorize({ window: {
      kinds: kinds.map((l) => l.firstChild).filter((c) => c.checked).map((c) => c.value),
      cards: scope.value === "any" || !cardId ? "any" : [cardId],
      minutes: Math.max(1, Math.min(720, parseInt(minutes.value, 10) || 60)) } })) }, "Approve with passkey"));
}

async function renderWindows() {
  const box = $("#windows");
  try {
    const ws = (await signedGet("/api/windows")).filter((w) => !w.closed_at && w.not_after * 1000 > Date.now());
    box.textContent = ws.length ? ws.map((w) => `window: ${w.kinds.join(", ")} until ${new Date(w.not_after * 1000).toLocaleTimeString()}${w.network_check ? "" : " (no network check)"}`).join(" · ") : "no window open";
  } catch (e) { box.textContent = ""; }
}

async function panel() {
  const keys = await serverKeys();
  peers = keys.peers || [];
  const st = await loadState();
  if (!st) {
    $("#card").replaceChildren(h("p", {}, "This browser is not enrolled. "), h("a", { href: "/enrol", target: "_blank" }, "Enrol it"));
    return;
  }
  $("#device").textContent = `${st.name || st.id}`;
  if (st.nonce) renderLock();
  else if (st.inFlight) await run("Reconnect", () => resume(st));
  window.addEventListener("message", (ev) => {
    if (!keys.board_origins.includes(ev.origin)) return;
    const d = ev.data;
    if (!d || d.type !== "awm-card" || typeof d.task_id !== "string" || !/^[A-Za-z0-9_-]{8}$/.test(d.task_id)) return;
    selected = { task_id: d.task_id,
                 body_sha256: typeof d.body_sha256 === "string" && /^[0-9a-f]{64}$/.test(d.body_sha256) ? d.body_sha256 : null,
                 preview: typeof d.preview === "string" ? d.preview.slice(0, 400) : "" };
    renderCard();
  });
  if (window.parent !== window) for (const o of keys.board_origins) window.parent.postMessage({ type: "awm-panel-ready" }, o);
  const tick = () => { renderTwofa(); renderWindows(); if (selected) renderCard(); };
  tick();
  setInterval(() => { renderTwofa(); renderWindows(); }, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
}

// -- the embeddable button (/panel/button?kind=&node=&label= or ?action=note_new) ----------

// The board's New note button in Proposed (R63 item 1): a title and one signed press.
function noteButton(b) {
  const title = h("input", { placeholder: "New note title", maxlength: "200" });
  b.before(title);
  b.textContent = "New note";
  b.addEventListener("click", async () => {
    const out = await run("New note", () => act("note_new", { title: title.value }));
    if (out && out.ok && out.result && out.result.ok) title.value = "";
  });
}

async function button() {
  const q = new URLSearchParams(location.search);
  const b = $("#go");
  if (q.get("action") === "note_new") return noteButton(b);
  const kind = q.get("kind"), node = q.get("node");
  b.textContent = q.get("label") || `${kind} on ${node}`;
  b.addEventListener("click", async () => {
    const out = await run(b.textContent, () => act("order", { kind, node, payload: {} }));
    const id = out && out.ok && out.result && out.result.order_id;
    if (!id) return;
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      const o = await signedGet(`/api/order/${id}`).catch(() => null);
      if (o && o.done_at) { say(o.ok ? `done: ${o.note || "ok"}` : `failed: ${o.note}`, o.ok ? "ok" : "error"); return; }
      if (o && o.pulled_at) say(`${node} took it`);
    }
  });
}

// -- enrolment ---------------------------------------------------------------------------

async function enrolPage() {
  const token = location.hash.slice(1);
  history.replaceState(null, "", "/enrol");
  const name = $("#name");
  const go = async (t) => {
    const st = await enrol(t, name.value || "device");
    return `Enrolled as ${st.name}. You can close this tab.`;
  };
  $("#enrol").addEventListener("click", () => {
    if (token) run("Enrol", () => go(token));
    else run("Ask a trusted device", async () => {
      const ask = await post("/api/enrol/ask", { name: name.value || "device" });
      say("Waiting for approval on a trusted device…");
      for (;;) {
        await new Promise((r) => setTimeout(r, 3000));
        const s = await post("/api/enrol/status", { request_id: ask.request_id, poll_secret: ask.poll_secret });
        if (s.token) return go(s.token);
        if (s.status !== "pending") throw new Error(`the request was ${s.status}`);
      }
    });
  });
  $("#enrol").textContent = token ? "Enrol this device" : "Ask a trusted device";
}

// -- portal login (fleet.md R84) -------------------------------------------------------------

async function loginPage() {
  const q = new URLSearchParams(location.search);
  const site = q.get("site") || "", node = q.get("node") || "", ret = q.get("return") || "";
  $("#what").textContent = `Sign in to ${site} on ${node}`;
  $("#login").addEventListener("click", () => run("Sign in", async () => {
    const begin = await post("/api/login/begin", { site, node, return: ret });
    const a = await assertion(begin.options);
    const out = await post("/api/login/finish", { nonce: begin.nonce, assertion: a });
    location.assign(`${out.return}#awm_grant=${encodeURIComponent(JSON.stringify(out.grant))}`);
  }));
}

const pages = { panel, button, enrol: enrolPage, login: loginPage };
pages[document.body.dataset.page]().catch((e) => say(e.message, "error"));
