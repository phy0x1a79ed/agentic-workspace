// The Swarm board (queue.md R61, R62): four collapsible sections of card headers, a search,
// and a detail panel with the approver frame. Trilium runs this as the board page's script.
// The cards are notes the writer on alcor keeps. Every action goes through the approver frame.

const box = api.$container[0];
const $ = (sel) => box.querySelector(sel);
const SECTIONS = [["proposed", "Proposed"], ["needs_you", "Needs you"], ["working", "Working"], ["done", "Done"]];
const PAGE = 50;
const STALE_S = 180;

const board = (await api.searchForNotes("#swarmBoard"))[0];
const origin = (board && board.getLabelValue("approverOrigin")) || "";
const frame = $(".awm-panel");
let panelReady = false;
let selected = null;   // {noteId, task_id, body_sha256, preview}
let doneCursor = null; // #doneAt of the last Done card shown, once Tony pages back
const open = { proposed: true, needs_you: true, working: true, done: false };

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k);
  return e;
}

const quoted = (q) => `"${q.replace(/["\\]/g, " ").trim()}"`;
const label = (n, name) => n.getLabelValue(name) || "";

// Done pages back on #doneAt as Tony scrolls (R61 item 4). The other sections show every card.
async function cards(section, before = null) {
  if (section !== "done") {
    return api.searchForNotes(`#swarmCard #section=${section} orderBy #updatedAt desc`);
  }
  const cursor = before ? ` #doneAt < ${Number(before)}` : "";
  return api.searchForNotes(`#swarmCard #section=done${cursor} orderBy #doneAt desc limit ${PAGE}`);
}

function header(n) {
  const status = label(n, "status"), peer = label(n, "peer");
  return el("div", { class: "awm-card", role: "option", "data-task": label(n, "taskId"),
                     "aria-selected": selected && selected.noteId === n.noteId ? "true" : "false",
                     onclick: () => select(n) },
    el("span", { class: "t" }, n.title),
    status ? el("span", { class: "awm-chip status" }, status) : null,
    peer ? el("span", { class: "awm-chip peer" }, peer) : null);
}

// The sections are built once, so the New note frame keeps its state and Done keeps its
// scroll. Each refresh replaces only the count and the card list.
const parts = {};
for (const [key, name] of SECTIONS) {
  const count = el("span", { class: "n" });
  const list = el("div", { class: "awm-list" });
  const d = el("details", { "data-section": key }, el("summary", {}, `${name} `, count));
  if (open[key]) d.setAttribute("open", "");
  d.addEventListener("toggle", () => { open[key] = d.open; });
  if (key === "proposed" && origin) {
    // The press is signed on alcor's origin like every other action (R63 item 1, R65).
    d.append(el("iframe", { class: "awm-note", title: "new note",
                            allow: "publickey-credentials-get; publickey-credentials-create",
                            src: `${origin}/panel/button?action=note_new` }));
  }
  d.append(list);
  if (key === "done") d.append(el("div", { class: "awm-sentinel" }));
  parts[key] = { d, count, list };
}
$(".awm-sections").replaceChildren(...SECTIONS.map(([key]) => parts[key].d));

function fill(key, notes, more) {
  parts[key].count.textContent = `(${notes.length}${more ? "+" : ""})`;
  parts[key].list.replaceChildren(...notes.map(header));
}

let doneShown = [];
let doneHasMore = false;
let paging = false;

async function render() {
  const q = $(".awm-search").value.trim();
  if (q) {
    // A bare quoted phrase is a Trilium full-text search, which matches far more than the card.
    const v = quoted(q);
    const hits = await api.searchForNotes(`#swarmCard and (note.title *=* ${v} or #taskId = ${v} `
      + `or #peer = ${v} or #status = ${v}) orderBy #updatedAt desc`);
    for (const [key] of SECTIONS) fill(key, hits.filter((n) => label(n, "section") === key), false);
  } else {
    for (const [key] of SECTIONS) {
      if (key !== "done") { fill(key, await cards(key), false); continue; }
      if (doneCursor === null) {
        doneShown = await cards("done");
        doneHasMore = doneShown.length === PAGE;
      }
      fill("done", doneShown, doneHasMore);
    }
  }
  heartbeat();
}

async function pageBack() {
  const last = doneShown[doneShown.length - 1];
  if (paging || !doneHasMore || !last || $(".awm-search").value.trim()) return;
  paging = true;
  try {
    doneCursor = label(last, "doneAt");
    const page = await cards("done", doneCursor);
    doneHasMore = page.length === PAGE;
    doneShown = doneShown.concat(page);
    fill("done", doneShown, doneHasMore);
  } finally { paging = false; }
}

new IntersectionObserver((seen) => { if (seen.some((e) => e.isIntersecting)) pageBack(); },
                         { root: $(".awm-sections"), rootMargin: "200px" })
  .observe(parts.done.d.querySelector(".awm-sentinel"));

function heartbeat() {
  const beat = Number(board && board.getLabelValue("queueHeartbeat")) || 0;
  const age = Math.floor(Date.now() / 1000) - beat;
  const hb = $(".awm-heartbeat");
  if (!beat) { hb.textContent = "alcor has not written yet"; hb.dataset.stale = ""; return; }
  if (age > STALE_S) { hb.textContent = `alcor unreachable for ${Math.round(age / 60)} min`; hb.dataset.stale = ""; }
  else { hb.textContent = "queue live"; delete hb.dataset.stale; }
}

async function sha256hex(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function select(n) {
  const taskId = label(n, "taskId");
  const kids = await n.getChildNotes();
  const rec = kids.find((k) => k.title === "record");
  let record = { sessions: [] };
  try { if (rec) record = JSON.parse((await rec.getBlob()).content || "{}"); } catch (e) { /* shown empty */ }
  let body = null, sha = null;
  if (label(n, "section") === "proposed") {
    body = (await n.getBlob()).content || "";
    sha = await sha256hex(body);
  }
  selected = { noteId: n.noteId, task_id: taskId, body_sha256: sha,
               preview: body === null ? "" : body.split("\n").slice(0, 8).join("\n").slice(0, 400) };
  for (const c of box.querySelectorAll(".awm-card")) {
    c.setAttribute("aria-selected", c.dataset.task === taskId ? "true" : "false");
  }
  const rows = (record.sessions || []).map((s) => el("tr", {},
    el("td", {}, s.name || s.session_id), el("td", {}, s.role), el("td", {}, s.node || ""),
    el("td", {}, s.active ? "active" : "ended"),
    el("td", {}, /^https:\/\//.test(s.rc_url || "") ? el("a", { href: s.rc_url, target: "_blank", rel: "noopener" }, "Remote Control") : "")));
  $(".awm-info").replaceChildren(
    el("h3", {}, n.title),
    el("p", {}, [taskId, label(n, "section"), label(n, "status"), label(n, "peer")].filter(Boolean).join(" · ")),
    rows.length ? el("table", {}, el("tr", {}, ...["session", "role", "node", "state", ""].map((h) => el("th", {}, h))), ...rows)
                : el("p", {}, "No sessions yet."));
  tell();
}

function tell() {
  if (!panelReady || !selected || !origin) return;
  frame.contentWindow.postMessage({ type: "awm-card", task_id: selected.task_id,
                                    body_sha256: selected.body_sha256, preview: selected.preview }, origin);
}

function onMessage(ev) {
  if (!box.isConnected) { window.removeEventListener("message", onMessage); return; }
  if (ev.origin !== origin || ev.source !== frame.contentWindow) return;
  if (ev.data && ev.data.type === "awm-panel-ready") { panelReady = true; tell(); }
}

window.addEventListener("message", onMessage);
if (origin) frame.src = `${origin}/panel`;
else $(".awm-info").replaceChildren(el("p", {}, "The board has no #approverOrigin label: run board-install."));

let typing = null;
$(".awm-search").addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(render, 300); });
const timer = setInterval(() => { if (!box.isConnected) clearInterval(timer); else render(); }, 15000);
await render();
