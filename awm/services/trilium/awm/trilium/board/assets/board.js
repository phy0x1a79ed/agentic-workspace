// The Control panel (queue.md R61-R66, fleet.md R147): New agent, three sections of card headers,
// then the fleet from the roster note: nodes, seats with their Remote Control switches, a plugin
// grid, a service grid and notices. One search filters the rows of every section but History. A
// draggable divider sets off the info panel with a card's sessions and approvals. Every press is a
// fetch to alcor's API. The page re-renders when Trilium reports changed entities.

const box = api.$container[0];
const $ = (sel) => box.querySelector(sel);
const SECTIONS = [["needs_you", "Needs you"], ["working", "Working"], ["history", "History"]];
const PAGE = 30;
const NEAR_PX = 200;
const origin = await swarmOrigin();
const FLEET = [["nodes", "Nodes"], ["seats", "Seats"], ["plugins", "Plugins"], ["services", "Services"],
               ["notices", "Notices"]];
const OPEN = new Set(["needs_you", "working", "nodes"]);
const SEARCHED = ["needs_you", "working"];
let fleet = { roster: {}, nodes: [] };
let selected = null;       // task id
let historyShown = [];
let historyMore = false;
let paging = false;
let nearEnd = false;       // History's sentinel is within NEAR_PX of the view
let rendering = false;
let again = false;

const label = (n, name) => n.getLabelValue(name) || "";
const rc = (url) => (/^https:\/\/claude\.ai\//.test(url || "")
  ? el("a", { href: url, target: "_blank", rel: "noopener" }, "Remote Control") : null);
const quoted = (q) => `"${q.replace(/["\\]/g, " ").trim()}"`;
const empty = (text, tag = "div") => el(tag, { class: "awm-empty" }, text);

// -- the sections ---------------------------------------------------------------------------------

const parts = {};
for (const [key, name] of SECTIONS) {
  const count = key === "history" ? null : el("span", { class: "n" });
  const list = el("div", { class: "awm-list" });
  const d = el("details", { "data-section": key }, el("summary", {}, `${name} `, count));
  if (OPEN.has(key)) d.setAttribute("open", "");
  d.append(list);
  if (key === "history") {
    d.addEventListener("toggle", () => { if (d.open) render(); });
    d.append(el("div", { class: "awm-sentinel" }));
  }
  parts[key] = { d, count, list, notes: [] };
}
for (const [key, name] of FLEET) {
  const d = el("details", { "data-section": key, class: "awm-fleet" }, el("summary", {}, name),
    el("div", { class: `awm-${key}` }));
  if (OPEN.has(key)) d.setAttribute("open", "");
  parts[key] = { d };
}
$(".awm-sections").replaceChildren(...SECTIONS.concat(FLEET).map(([key]) => parts[key].d));
const query = () => $(".awm-search").value;

function header(n) {
  const status = label(n, "status"), node = label(n, "peer"), waiting = Number(label(n, "pendingApprovals")) || 0;
  return el("div", { class: "awm-card", role: "option", "data-task": label(n, "taskId"),
                     "aria-selected": selected === label(n, "taskId") ? "true" : "false",
                     onclick: () => select(n) },
    el("span", { class: "t" }, n.title),
    waiting ? el("span", { class: "awm-chip pending" }, `${waiting} to approve`) : null,
    status ? el("span", { class: "awm-chip status" }, status) : null,
    node ? el("span", { class: "awm-chip node" }, node) : null);
}

const cardMatches = (q, n) => fuzzy(q, n.title) || ["taskId", "peer", "status"].some((k) => fuzzy(q, label(n, k)));

// Needs you and Working show the cards that match the search. History shows all it has loaded.
function fill(key, notes, q = "") {
  parts[key].notes = notes;
  const rows = notes.filter((n) => cardMatches(q, n));
  if (parts[key].count) parts[key].count.textContent = rows.length === notes.length ? `(${notes.length})` : `(${rows.length}/${notes.length})`;
  parts[key].list.replaceChildren(...(rows.length ? rows.map(header)
    : [empty(notes.length ? "No card matches." : "No cards.")]));
}

async function cards(section) {
  return api.searchForNotes(`#swarmCard #section=${section} orderBy #updatedAt desc`);
}

async function historyPage(before) {
  const cursor = before ? ` #historyAt < ${Number(before)}` : "";
  return api.searchForNotes(`#swarmCard #section=history${cursor} orderBy #historyAt desc limit ${PAGE}`);
}

async function render() {
  if (rendering) { again = true; return; }
  rendering = true;
  try {
    do {
      again = false;
      const history = parts.history.d.open;
      const [needs, working, first] = await Promise.all([cards("needs_you"), cards("working"),
        history ? historyPage(null) : null, status()]);
      fill("needs_you", needs, query());
      fill("working", working, query());
      if (history) {
        // History keeps the pages Tony scrolled through; a change reloads only the first page.
        const have = new Set(first.map((n) => n.noteId));
        const older = historyShown.filter((n) => !have.has(n.noteId) && label(n, "historyAt")
          && Number(label(n, "historyAt")) <= Number(label(first[first.length - 1] || n, "historyAt")));
        historyShown = first.concat(older);
        if (!paging) historyMore = first.length === PAGE || older.length > 0;
        fill("history", historyShown);
      }
      if (selected) {
        const n = (await api.searchForNotes(`#swarmCard #taskId = ${quoted(selected)}`))[0];
        if (n) await panel(n);
      }
    } while (again);
  } finally { rendering = false; }
  pageBack();
}

async function pageBack() {
  if (paging) return;
  paging = true;
  try {
    while (historyMore && historyShown.length && nearEnd) {
      const last = historyShown[historyShown.length - 1];
      const page = await historyPage(label(last, "historyAt"));
      historyMore = page.length === PAGE;
      historyShown = historyShown.concat(page);
      parts.history.list.append(...page.map(header));
    }
  } finally { paging = false; }
}

// History loads older cards as the sentinel scrolls into view (R61 item 5). Scrolled past it, Tony
// reads the fleet and pages nothing.
new IntersectionObserver((seen) => { nearEnd = seen[seen.length - 1].isIntersecting; if (nearEnd) pageBack(); },
                         { root: $(".awm-sections"), rootMargin: `${NEAR_PX}px` })
  .observe(parts.history.d.querySelector(".awm-sentinel"));

async function status() {
  const root = await api.getNote(SWARM_BOARD_ID);
  const live = $(".awm-live");
  const at = Number(root && root.getLabelValue("queueChangedAt")) || 0;
  if (at) { live.textContent = `alcor pushed ${new Date(at * 1000).toLocaleTimeString()}`; delete live.dataset.down; }
  else { live.textContent = "alcor has not pushed yet"; live.dataset.down = ""; }
}

// -- the fleet ------------------------------------------------------------------------------------------

// A minor misspelling still matches (fleet.md R147 item 4): a substring, or a word within a small
// edit distance of the query.
function editDistance(a, b) {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let prev = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const cur = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1));
      prev = cur;
    }
  }
  return row[b.length];
}

function fuzzy(query, text) {
  const q = query.toLowerCase().trim();
  if (!q) return true;
  const t = show(text).toLowerCase();
  if (t.includes(q)) return true;
  const slack = q.length <= 3 ? 0 : q.length <= 6 ? 1 : 2;
  if (!slack) return false;
  return t.split(/[^a-z0-9]+/).some((w) => w && (editDistance(q, w) <= slack
    || (w.length > q.length && editDistance(q, w.slice(0, q.length)) <= slack)));
}

function ago(ts) {
  if (ts === null || ts === undefined || ts === "") return "never";
  const v = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  if (!v) return "never";
  const s = Math.max(0, Math.round((Date.now() - v) / 1000));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}


const sessionMatches = (q, s) => fuzzy(q, s.name) || fuzzy(q, s.scope) || fuzzy(q, s.project);

// A session started for a card keeps the name "task <id>", so it shows the card's title.
const TASK_NAME = /^task (\S+)$/;
async function cardTitles(nodes) {
  const ids = [...new Set(nodes.flatMap((n) => n.sessions || []).map((s) => TASK_NAME.exec(show(s.name))?.[1]).filter(Boolean))];
  const hits = await Promise.all(ids.map((id) => api.searchForNotes(`#swarmCard #taskId = ${JSON.stringify(id)}`)));
  return new Map(ids.flatMap((id, i) => (hits[i][0] ? [[`task ${id}`, hits[i][0].title]] : [])));
}

function nodeSection(n, q) {
  const own = fuzzy(q, n.name);
  const sessions = (n.sessions || []).filter((s) => own || sessionMatches(q, s));
  const services = n.services || [];
  const up = services.filter((s) => s.up).length;
  return el("article", { class: "awm-node", "data-node": n.name },
    el("header", {}, el("b", {}, n.name), n.role ? el("span", { class: "awm-chip" }, n.role) : null,
      n.portal ? el("a", { href: n.portal, target: "_blank", rel: "noopener" }, "portal") : null,
      el("small", {}, `seen ${ago(n.last_seen)}`),
      el("small", {}, `${(n.sessions || []).length} live`),
      el("small", {}, `${n.sessions_7d || 0} sessions in 7d`),
      el("small", {}, `${up}/${services.length} services up`)),
    sessions.length ? el("ul", {}, ...sessions.map((s) => el("li", {}, show(s.name), " ",
      s.project || s.scope ? el("small", {}, [s.project, s.scope].filter(Boolean).join("/")) : null, " ", rc(s.rc_url))))
      : empty("no sessions"));
}

// A flip waits for the seat's answer: a seat event newer than the row it was flipped on.
const flips = new Map();
const FLIP_GIVE_UP_MS = 120000;

function settleFlip(s) {
  const f = flips.get(s.seat);
  if (!f) return;
  if (Date.now() - f.at > FLIP_GIVE_UP_MS) { flips.delete(s.seat); return; }
  if (!s.rc_live || s.ts === f.ts) return;
  flips.delete(s.seat);
  const failed = s.rc_live.startsWith("failed");
  toast(failed ? `${s.label || s.seat}: Remote Control ${f.want ? "on" : "off"} ${s.rc_live}`
    : `${s.label || s.seat}: Remote Control ${s.remote_control ? "on" : "off"}`, failed);
}

// A seat's model and effort as the spawn panel names them. An unsent pick outlives a redraw.
const choices = (name) => [...document.querySelectorAll(`input[name="awm-spawn-${name}"]`)]
  .map((i) => [i.value, i.nextElementSibling.textContent]);
const picks = new Map();
// A Refresh waits for its answer as a flip does.
const refreshes = new Map();

function settleRefresh(s) {
  const r = refreshes.get(s.seat);
  if (!r) return;
  if (Date.now() - r.at > FLIP_GIVE_UP_MS) { refreshes.delete(s.seat); return; }
  if (!s.refresh_live || s.ts === r.ts) return;
  refreshes.delete(s.seat);
  toast(`${s.label || s.seat}: ${s.model} at ${s.effort} effort ${s.refresh_live === "pending"
    ? "once its agent is between turns" : "from its next start"}`);
}

function pickCell(s) {
  settleRefresh(s);
  const pick = picks.get(s.seat) || { model: s.model, effort: s.effort };
  const select = (name) => {
    const opts = choices(name);
    if (pick[name] && !opts.some(([v]) => v === pick[name])) opts.push([pick[name], pick[name]]);
    const sel = el("select", { "aria-label": name }, ...opts.map(([v, label]) =>
      el("option", { value: v, selected: v === pick[name] }, label)));
    sel.addEventListener("change", () => picks.set(s.seat, { ...pick, [name]: sel.value }));
    return sel;
  };
  const waiting = refreshes.has(s.seat) || s.refresh;
  const btn = el("button", { class: "awm-btn", type: "button", disabled: waiting },
    waiting ? "Refresh pending" : "Refresh");
  const [model, effort] = [select("model"), select("effort")];
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    refreshes.set(s.seat, { ts: s.ts, at: Date.now() });
    try {
      await swarmCall(origin, "/api/seat/refresh", { seat: s.seat, model: model.value, effort: effort.value });
      picks.delete(s.seat);
      toast(`${s.label || s.seat}: refresh on ${model.value} at ${effort.value} effort requested`);
    } catch (e) { refreshes.delete(s.seat); btn.disabled = false; toast(e.message, true); }
  });
  return el("td", {}, model, " ", effort, " ", btn);
}

function seatRow(s) {
  settleFlip(s);
  const pending = flips.has(s.seat);
  const sw = el("input", { type: "checkbox", title: "Remote Control" });
  sw.checked = !!s.remote_control;
  sw.disabled = pending;
  sw.addEventListener("change", async () => {
    const want = sw.checked;
    sw.checked = !want;
    sw.disabled = true;
    flips.set(s.seat, { want, ts: s.ts, at: Date.now() });
    try {
      // The seat id holds a slash, so it travels in the body (fleet.md R147 item 7).
      await swarmCall(origin, "/api/seat/remote_control", { seat: s.seat, on: want });
      toast(`${s.label || s.seat}: Remote Control ${want ? "on" : "off"} requested`);
    } catch (e) { flips.delete(s.seat); sw.disabled = false; toast(e.message, true); }
  });
  return el("tr", { "data-seat": s.seat }, el("td", {}, s.label || s.seat), el("td", {}, s.state || ""),
    el("td", {}, el("label", {}, sw, pending ? " Remote Control (requested)" : " Remote Control")),
    el("td", {}, s.remote_control ? rc(s.rc_url) : null), pickCell(s));
}

function table(heads, rows, none) {
  return rows.length ? el("table", {}, el("tr", {}, ...heads.map((h) => el("th", {}, h))), ...rows)
    : empty(none);
}

// Each state has its own mark, so the grid reads without colour or text.
const STATES = {
  enabled: ["\u25CF", "enabled"],
  disabled: ["\u25CB", "disabled"],
  failed: ["\u25B2", "enabled but not running"],
  uninstalled: ["\u2013", "not installed"],
  reachable: ["\u25C6", "singleton, reachable"],
  unreachable: ["\u25C7", "singleton, unreachable"],
  unknown: ["?", "unknown"],
};
// Switching one of these off can take the Control panel, or the way back to it, with it.
const BREAKS_OPS = new Set(["telemetry", "operations", "trilium", "reflection", "auth"]);

function cellState(local, singleton) {
  if (local) {
    if (local.enabled === null || local.enabled === undefined) return "unknown";
    if (!local.enabled) return "disabled";
    return local.up ? "enabled" : "failed";
  }
  if (!singleton) return "uninstalled";
  if (singleton.health === "up") return "reachable";
  return singleton.health === "down" ? "unreachable" : "unknown";
}

const mark = (state, title) => el("span", { class: `awm-state ${state}`, title: title || STATES[state][1] }, STATES[state][0]);

async function toggleService(node, row, local, btn) {
  const on = !local.enabled;
  const core = row.core || BREAKS_OPS.has(row.name);
  if (!on && core && !window.confirm(`${row.name} on ${node} is a core service. Switching it off can break the Control panel or the node's links. Switch it off?`)) return;
  btn.disabled = true;
  const body = { node, service: local.service, on };
  try {
    let out = await swarmCall(origin, "/api/service/toggle", body);
    if (out.needs_approval) {
      if (!await approvalModal(origin, out.request_id)) { btn.disabled = false; return; }
      out = await swarmCall(origin, "/api/service/toggle", body);
    }
    toast(`${row.name} on ${node}: ${on ? "enable" : "disable"} sent`);
  } catch (e) { toast(e.message, true); btn.disabled = false; }
}

function serviceCell(row, node) {
  const local = (node.services || []).find((s) => s.service === row.name);
  const state = cellState(local, row.singleton);
  const canSwitch = local && (state === "enabled" || state === "disabled" || state === "failed")
    && !(row.name === "reflection" && local.enabled);
  if (!canSwitch) return el("td", { class: "cell" }, mark(state));
  const btn = el("button", { class: "awm-switch", title: `${STATES[state][1]}: click to switch ${local.enabled ? "off" : "on"}` }, mark(state));
  btn.addEventListener("click", () => toggleService(node.name, row, local, btn));
  return el("td", { class: "cell" }, btn);
}

function serviceRows(roster, nodes) {
  const rows = new Map();
  const core = new Set(nodes.flatMap((n) => (n.plugins || []).filter((p) => p.core).map((p) => p.name)));
  for (const n of nodes) for (const s of n.services || []) {
    rows.set(s.service, { name: s.service, core: core.has(s.plugin) });
  }
  const owner = new Map(nodes.flatMap((n) => n.services || []).flatMap((x) => [...(x.tools || []), x.service].map((t) => [t, x.service])));
  for (const s of roster.services || []) {
    const name = owner.get(s.service) || s.service;
    rows.set(name, { ...(rows.get(name) || { name, core: false }), singleton: s });
  }
  return [...rows.values()].sort((a, b) => a.name.localeCompare(b.name));
}

const LEGEND = el("div", { class: "awm-legend" }, ...Object.keys(STATES).map((k) => el("span", {}, mark(k), " ", STATES[k][1])));

function grid(nodes, rows, cell, none) {
  if (!rows.length) return empty(none);
  return el("div", { class: "awm-grid" }, el("table", {},
    el("tr", {}, el("th", {}, ""), ...nodes.map((n) => el("th", { class: "cell" }, n.name))),
    ...rows.map((r) => el("tr", { "data-row": r.name }, el("td", {}, r.name,
      r.core ? el("span", { class: "awm-chip" }, "core") : null,
      r.singleton ? el("small", {}, ` ${r.singleton.host}`) : null),
      ...nodes.map((n) => cell(r, n))))));
}

function pluginCell(row, node) {
  const p = (node.plugins || []).find((x) => x.name === row.name);
  return el("td", { class: "cell" }, p ? mark("enabled", p.version || "installed") : mark("uninstalled"));
}

async function renderFleet() {
  const roster = await swarmRoster();
  fillNodes(roster);
  const nodes = roster.nodes || [];
  const titles = await cardTitles(nodes);
  for (const n of nodes) for (const s of n.sessions || []) s.name = titles.get(s.name) || s.name;
  fleet = { roster, nodes };
  drawFleet();
}

function drawFleet() {
  const { roster, nodes: all } = fleet;
  const q = query();
  const nodes = all.filter((n) => fuzzy(q, n.name) || (n.sessions || []).some((s) => sessionMatches(q, s)));
  $(".awm-nodes").replaceChildren(...(nodes.length ? nodes.map((n) => nodeSection(n, q))
    : [empty(all.length ? "No node matches." : "No nodes reported.")]));
  const seats = (roster.seats || []).filter((s) => fuzzy(q, s.label) || fuzzy(q, s.seat));
  $(".awm-seats").replaceChildren(table(["seat", "state", "", "", "agent"], seats.map(seatRow), "No seats."));
  const plugins = new Map();
  for (const n of all) for (const p of n.plugins || []) plugins.set(p.name, { name: p.name, core: p.core });
  $(".awm-plugins").replaceChildren(grid(all, [...plugins.values()].filter((p) => fuzzy(q, p.name)).sort((a, b) => a.name.localeCompare(b.name)),
    pluginCell, "No plugins."));
  $(".awm-services").replaceChildren(LEGEND, grid(all, serviceRows(roster, all).filter((r) => fuzzy(q, r.name)), serviceCell, "No services."));
  const notices = (roster.notices || []).filter((n) => fuzzy(q, `${n.kind} ${n.node} ${n.title}`));
  $(".awm-notices").replaceChildren(...(notices.length ? notices.map((n) => el("details", {},
    el("summary", {}, `${n.title || n.kind} - ${n.node}`), el("pre", {}, show(n.body))))
    : [empty("No notices.")]));
}

// -- the info panel -------------------------------------------------------------------------------------

async function recordOf(n) {
  const kids = await n.getChildNotes();
  const rec = kids.find((k) => k.title === "record");
  try { if (rec) return JSON.parse((await rec.getBlob()).content || "{}"); } catch (e) { /* shown empty */ }
  return {};
}

function when(ts) {
  const v = Number(ts);
  return v ? new Date(v * 1000).toLocaleString() : "";
}

async function act(path, ok, body) {
  try {
    await swarmCall(origin, path, body);
    toast(ok);
  } catch (e) { toast(e.message, true); }
}

function approvalRow(a) {
  const pending = a.state === "pending";
  const cls = a.state === "approved" ? "ok" : (pending ? "pending" : (a.state === "rejected" ? "bad" : ""));
  return el("div", { class: "awm-appr", "data-request": show(a.request_id) },
    el("div", { class: "awm-row" }, el("span", { class: `awm-chip ${cls}` }, show(a.state)),
      el("small", {}, when(a.requested_at || a.created)), a.decided_at ? el("small", {}, `→ ${when(a.decided_at)}`) : null),
    el("div", { class: "sum" }, show(a.summary)),
    pending ? el("div", { class: "awm-row" },
      el("button", { class: "awm-btn primary", type: "button", onclick: async () => {
        if (await approvalModal(origin, a.request_id)) toast("Approved"); } }, "Approve"),
      el("button", { class: "awm-btn danger", type: "button", onclick: () =>
        act(`/api/request/${encodeURIComponent(a.request_id)}/reject`, "Rejected") }, "Reject")) : null);
}

async function panel(n) {
  const rec = await recordOf(n);
  const taskId = label(n, "taskId");
  const section = label(n, "section");
  const sessions = rec.sessions || [];
  const approvals = rec.approvals || [];
  const pending = approvals.filter((a) => a.state === "pending");
  const past = approvals.filter((a) => a.state !== "pending");
  const info = [
    el("div", { class: "awm-row" },
      el("span", { class: "awm-id", title: "Copy the task id", onclick: async () => {
        try { await navigator.clipboard.writeText(taskId); toast(`Copied ${taskId}`); }
        catch (e) { toast("The browser refused the copy.", true); } } }, taskId),
      el("span", { class: "awm-chip" }, section.replace("_", " ")),
      el("button", { class: "awm-btn", type: "button", onclick: () => api.activateNote(n.noteId) }, "Open note"),
      rec.source_note ? el("button", { class: "awm-btn", type: "button",
                                       onclick: () => api.activateNote(rec.source_note) }, "Source note") : null,
      section !== "history" ? el("button", { class: "awm-btn", type: "button", onclick: () =>
        act(`/api/card/${encodeURIComponent(taskId)}/close`, "Closed") }, "Close") : null),
    rec.reason ? el("p", {}, show(rec.reason)) : null,
    el("h4", {}, "Sessions"),
    sessions.length ? el("table", {},
      el("tr", {}, ...["session", "project / scope", "role", "node", "state", ""].map((h) => el("th", {}, h))),
      ...sessions.map((s) => el("tr", {},
        el("td", {}, show(s.name || s.session_id)), el("td", {}, [s.project, s.scope].filter(Boolean).join(" / ")),
        el("td", {}, show(s.role)), el("td", {}, show(s.node)),
        el("td", {}, s.active ? "active" : "ended"),
        el("td", {}, s.active ? rc(s.rc_url) : null))))
      : empty("No sessions yet.", "p"),
    el("h4", {}, `Approvals${pending.length ? ` (${pending.length} pending)` : ""}`),
    ...(approvals.length ? pending.map(approvalRow).concat(past.map(approvalRow))
      : [empty("No approvals.", "p")]),
  ];
  const todos = (rec.todos || []).filter((t) => t && (t.subject || t.content));
  if (todos.length) {
    info.push(el("h4", {}, "Todos"), el("ul", {}, ...todos.map((t) =>
      el("li", {}, `${t.status === "completed" ? "☑" : "☐"} ${show(t.subject || t.content)}`))));
  }
  $(".awm-info").replaceChildren(...info);
}

async function select(n) {
  selected = label(n, "taskId");
  for (const c of box.querySelectorAll(".awm-card")) {
    c.setAttribute("aria-selected", c.dataset.task === selected ? "true" : "false");
  }
  await panel(n);
}

// -- New agent, search, 2fa -------------------------------------------------------------------------------

const picked = (field) => $(`input[name="awm-spawn-${field}"]:checked`)?.value;
const remembered = (name) => { try { return localStorage.getItem(name); } catch (e) { return null; } };

$(".awm-spawn").addEventListener("change", (ev) => {
  try { localStorage.setItem(ev.target.name, ev.target.value); } catch (e) { /* none */ }
});
for (const i of box.querySelectorAll(".awm-spawn input")) if (remembered(i.name) === i.value) i.checked = true;

function fillNodes(roster) {
  const nodes = nodeChoices(roster);
  const have = picked("node") || remembered("awm-spawn-node");
  const keep = nodes.some((n) => n.name === have) ? have : nodes[0]?.name;
  $(".awm-spawn-node").replaceChildren(...(nodes.length
    ? nodes.map((n) => el("label", {},
      el("input", { type: "radio", name: "awm-spawn-node", value: n.name, checked: n.name === keep }),
      el("span", { title: n.name }, n.name)))
    : [empty("no nodes in the roster", "small")]));
  $(".awm-newbtn").disabled = nodes.length === 0;
}

$(".awm-newbtn").addEventListener("click", async () => {
  const node = picked("node");
  if (!node) return;
  try {
    const out = await swarmCall(origin, "/api/agent", { node, model: picked("model"), effort: picked("effort") });
    toast(`Agent starting on ${node}${out.task_id ? ` (${out.task_id})` : ""}`);
  } catch (e) { toast(e.message, true); }
});

for (const b of box.querySelectorAll(".awm-twofa button")) {
  b.addEventListener("click", async () => { if (await twofaModal(origin, b.dataset.count)) toast("Armed"); });
}

// The search refilters what is loaded. It never queries the vault.
$(".awm-search").addEventListener("input", () => {
  for (const key of SEARCHED) fill(key, parts[key].notes, query());
  drawFleet();
});

// -- the divider ------------------------------------------------------------------------------------------------

(function divider() {
  const bar = $(".awm-divider"), main = $(".awm-main");
  try { const w = localStorage.getItem("awm-board-left"); if (w) main.style.setProperty("--awm-left", w); } catch (e) { /* none */ }
  bar.addEventListener("pointerdown", (ev) => {
    bar.setPointerCapture(ev.pointerId);
    bar.dataset.drag = "";
  });
  bar.addEventListener("pointermove", (ev) => {
    if (!("drag" in bar.dataset)) return;
    const rect = main.getBoundingClientRect();
    const px = Math.max(220, Math.min(rect.width - 260, ev.clientX - rect.left));
    main.style.setProperty("--awm-left", `${px}px`);
  });
  const stop = () => {
    delete bar.dataset.drag;
    try { localStorage.setItem("awm-board-left", main.style.getPropertyValue("--awm-left")); } catch (e) { /* none */ }
  };
  bar.addEventListener("pointerup", stop);
  bar.addEventListener("pointercancel", stop);
})();

// Trilium's note pane scrolls a column of auto heights, so the page takes the pane's height itself
// and its left column is the one that scrolls.
(function fitPane() {
  const board = $(".awm-board"), pane = box.closest(".scrolling-container");
  if (!pane) return;
  new ResizeObserver(() => {
    const above = board.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop;
    board.style.height = `${Math.max(320, pane.clientHeight - above)}px`;
  }).observe(pane);
})();

entitiesChanged(box, cardsChanged, render);
entitiesChanged(box, rosterChanged, renderFleet);
await Promise.all([render(), renderFleet()]);
