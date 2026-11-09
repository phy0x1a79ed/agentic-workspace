// The Swarm board (queue.md R61-R66): three sections of card headers, a search, a draggable divider
// and an info panel with the task's sessions and approvals. Cards are notes the writer keeps; every
// press is a fetch to alcor's API. The page re-renders when Trilium reports changed entities.

const box = api.$container[0];
const $ = (sel) => box.querySelector(sel);
const SECTIONS = [["needs_you", "Needs you"], ["working", "Working"], ["history", "History"]];
const PAGE = 30;
const NEAR_PX = 200;
const origin = await swarmOrigin();
const open = { needs_you: true, working: true, history: false };
let selected = null;       // task id
let historyShown = [];
let historyMore = false;
let paging = false;
let rendering = false;
let again = false;

const label = (n, name) => n.getLabelValue(name) || "";
const quoted = (q) => `"${q.replace(/["\\]/g, " ").trim()}"`;
const rcLink = (url) => /^https:\/\/claude\.ai\//.test(url || "");

// -- the sections ---------------------------------------------------------------------------------

const parts = {};
for (const [key, name] of SECTIONS) {
  const count = key === "history" ? null : el("span", { class: "n" });
  const list = el("div", { class: "awm-list" });
  const d = el("details", { "data-section": key }, el("summary", {}, `${name} `, count));
  if (open[key]) d.setAttribute("open", "");
  d.addEventListener("toggle", () => { open[key] = d.open; if (key === "history" && d.open) render(); });
  d.append(list);
  if (key === "history") d.append(el("div", { class: "awm-sentinel" }));
  parts[key] = { d, count, list };
}
$(".awm-sections").replaceChildren(...SECTIONS.map(([key]) => parts[key].d));

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

function fill(key, notes) {
  if (parts[key].count) parts[key].count.textContent = `(${notes.length})`;
  parts[key].list.replaceChildren(...(notes.length ? notes.map(header)
    : [el("div", { class: "awm-empty" }, "No cards.")]));
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
      const q = $(".awm-search").value.trim();
      if (q) {
        const v = quoted(q);
        const hits = await api.searchForNotes(`#swarmCard and (note.title *=* ${v} or #taskId = ${v} `
          + `or #peer = ${v} or #status = ${v}) orderBy #updatedAt desc`);
        for (const [key] of SECTIONS) fill(key, hits.filter((n) => label(n, "section") === key));
      } else {
        for (const [key] of SECTIONS) {
          if (key !== "history") { fill(key, await cards(key)); continue; }
          if (!open.history) continue;
          // History keeps the pages Tony scrolled through; a change reloads only the first page.
          const first = await historyPage(null);
          const have = new Set(first.map((n) => n.noteId));
          const older = historyShown.filter((n) => !have.has(n.noteId) && label(n, "historyAt")
            && Number(label(n, "historyAt")) <= Number(label(first[first.length - 1] || n, "historyAt")));
          historyShown = first.concat(older);
          if (!paging) historyMore = first.length === PAGE || older.length > 0;
          fill("history", historyShown);
        }
      }
      await status();
      if (selected) {
        const n = (await api.searchForNotes(`#swarmCard #taskId = ${quoted(selected)}`))[0];
        if (n) await panel(n);
      }
    } while (again);
  } finally { rendering = false; }
  pageBack();
}

function nearEnd() {
  const sentinel = parts.history.d.querySelector(".awm-sentinel").getBoundingClientRect();
  return sentinel.height > 0 && sentinel.top < $(".awm-sections").getBoundingClientRect().bottom + NEAR_PX;
}

async function pageBack() {
  if (paging || $(".awm-search").value.trim()) return;
  paging = true;
  try {
    while (historyMore && historyShown.length && nearEnd()) {
      const last = historyShown[historyShown.length - 1];
      const page = await historyPage(label(last, "historyAt"));
      historyMore = page.length === PAGE;
      historyShown = historyShown.concat(page);
      parts.history.list.append(...page.map(header));
    }
  } finally { paging = false; }
}

// History loads older cards as the sentinel scrolls into view (R61 item 5).
new IntersectionObserver((seen) => { if (seen.some((e) => e.isIntersecting)) pageBack(); },
                         { root: $(".awm-sections"), rootMargin: `${NEAR_PX}px` })
  .observe(parts.history.d.querySelector(".awm-sentinel"));

async function status() {
  const root = await api.getNote(SWARM_BOARD_ID);
  const live = $(".awm-live");
  const at = Number(root && root.getLabelValue("queueChangedAt")) || 0;
  if (at) { live.textContent = `alcor pushed ${new Date(at * 1000).toLocaleTimeString()}`; delete live.dataset.down; }
  else { live.textContent = "alcor has not pushed yet"; live.dataset.down = ""; }
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
        el("td", {}, s.active && rcLink(s.rc_url) ? el("a", { href: s.rc_url, target: "_blank", rel: "noopener" }, "Remote Control") : ""))))
      : el("p", { class: "awm-empty" }, "No sessions yet."),
    el("h4", {}, `Approvals${pending.length ? ` (${pending.length} pending)` : ""}`),
    ...(approvals.length ? pending.map(approvalRow).concat(past.map(approvalRow))
      : [el("p", { class: "awm-empty" }, "No approvals.")]),
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

async function fillNodes() {
  const nodes = await nodeChoices();
  const have = picked("node") || remembered("awm-spawn-node");
  const keep = nodes.some((n) => n.name === have) ? have : nodes[0]?.name;
  $(".awm-spawn-node").replaceChildren(...(nodes.length
    ? nodes.map((n) => el("label", {},
      el("input", { type: "radio", name: "awm-spawn-node", value: n.name, checked: n.name === keep }),
      el("span", { title: n.name }, n.name)))
    : [el("small", { class: "awm-empty" }, "no nodes in the roster")]));
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

$(".awm-search").addEventListener("input", render);

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

entitiesChanged(box, cardsChanged, () => { render(); fillNodes(); });
await fillNodes();
await render();
