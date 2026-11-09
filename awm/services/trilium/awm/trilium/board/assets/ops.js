// Ops (fleet.md R147): the swarm status page, top to bottom: a section per node, the seats with
// their Remote Control switches, a plugin grid and a service grid with a column per node.
// Everything comes from the roster note the trilium plugin renders on each telemetry event; the
// page re-renders when that note changes.

const box = api.$container[0];
const $ = (sel) => box.querySelector(sel);
const origin = await swarmOrigin();

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

const rc = (url) => (/^https:\/\/claude\.ai\//.test(url || "")
  ? el("a", { href: url, target: "_blank", rel: "noopener" }, "Remote Control") : null);

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
      : el("div", { class: "none" }, "no sessions"));
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
    el("td", {}, s.remote_control ? rc(s.rc_url) : null));
}

function table(heads, rows, empty) {
  return rows.length ? el("table", {}, el("tr", {}, ...heads.map((h) => el("th", {}, h))), ...rows)
    : el("div", { class: "none" }, empty);
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
// Switching one of these off can take this page, or the way back to it, with it.
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
  if (!on && core && !window.confirm(`${row.name} on ${node} is a core service. Switching it off can break this page or the node's links. Switch it off?`)) return;
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
  const local = (node.services || []).find((s) => s.service === row.name || (row.singleton && (s.tools || []).includes(row.singleton.service)));
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
  for (const s of roster.services || []) {
    const owner = nodes.flatMap((n) => n.services || []).find((x) => x.service === s.service || (x.tools || []).includes(s.service));
    const name = owner ? owner.service : s.service;
    rows.set(name, { ...(rows.get(name) || { name, core: false }), singleton: s });
  }
  return [...rows.values()].sort((a, b) => a.name.localeCompare(b.name));
}

function legend() {
  return el("div", { class: "awm-legend" }, ...Object.keys(STATES).map((k) => el("span", {}, mark(k), " ", STATES[k][1])));
}

function grid(nodes, rows, cell, empty) {
  if (!rows.length) return el("div", { class: "none" }, empty);
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

async function render() {
  const roster = await swarmRoster();
  const q = $(".search").value;
  const all = roster.nodes || [];
  const titles = await cardTitles(all);
  for (const n of all) for (const s of n.sessions || []) s.name = titles.get(s.name) || s.name;
  const nodes = all.filter((n) => fuzzy(q, n.name) || (n.sessions || []).some((s) => sessionMatches(q, s)));
  $(".awm-nodes").replaceChildren(...(nodes.length ? nodes.map((n) => nodeSection(n, q))
    : [el("div", { class: "none" }, "No node matches.")]));
  const seats = (roster.seats || []).filter((s) => fuzzy(q, s.label) || fuzzy(q, s.seat));
  $(".awm-seats").replaceChildren(table(["seat", "state", "", ""], seats.map(seatRow), "No seats."));
  const plugins = new Map();
  for (const n of all) for (const p of n.plugins || []) plugins.set(p.name, { name: p.name, core: p.core });
  $(".awm-plugins").replaceChildren(grid(all, [...plugins.values()].filter((p) => fuzzy(q, p.name)).sort((a, b) => a.name.localeCompare(b.name)),
    pluginCell, "No plugins reported."));
  $(".awm-services").replaceChildren(legend(), grid(all, serviceRows(roster, all).filter((r) => fuzzy(q, r.name)), serviceCell, "No services."));
  const notices = (roster.notices || []).filter((n) => fuzzy(q, `${n.kind} ${n.node} ${n.title}`));
  $(".awm-notices").replaceChildren(...(notices.length ? [el("h3", {}, "Notices"),
    ...notices.map((n) => el("details", {}, el("summary", {}, `${n.title || n.kind} - ${n.node}`),
      el("pre", {}, show(n.body))))] : []));
}

$(".search").addEventListener("input", render);
entitiesChanged(box, rosterChanged, render);
await render();
