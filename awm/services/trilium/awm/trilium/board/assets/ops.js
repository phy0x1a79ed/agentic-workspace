// Ops (fleet.md R147): the swarm status page. One large card per node, always expanded, then the
// seats with their Remote Control switches and the services. Everything comes from the roster note
// the trilium plugin renders on each telemetry event; the page re-renders when that note changes.

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

function nodeCard(n, q) {
  const own = fuzzy(q, n.name);
  const sessions = (n.sessions || []).filter((s) => own || fuzzy(q, s.name));
  return el("article", { class: "awm-node", "data-node": n.name },
    el("header", {}, el("b", {}, n.name), n.role ? el("span", { class: "awm-chip" }, n.role) : null,
      n.portal ? el("a", { href: n.portal, target: "_blank", rel: "noopener" }, "portal") : null,
      el("small", {}, `seen ${ago(n.last_seen)}`), el("small", {}, `${n.sessions_7d || 0} sessions in 7d`)),
    el("h5", {}, "Sessions"),
    sessions.length ? el("ul", {}, ...sessions.map((s) => el("li", {}, show(s.name), " ",
      s.scope ? el("small", {}, s.scope) : null, " ", rc(s.rc_url)))) : el("div", { class: "none" }, "none"),
    el("h5", {}, "Services it can use"),
    (n.services || []).length ? el("ul", {}, ...n.services.map((s) => el("li", {}, show(s.service), " ",
      el("small", {}, `checked ${ago(s.last_checked)}, used ${ago(s.last_used)}`)))) : el("div", { class: "none" }, "none"));
}

function seatRow(s) {
  const sw = el("input", { type: "checkbox", title: "Remote Control" });
  sw.checked = !!s.remote_control;
  sw.addEventListener("change", async () => {
    const want = sw.checked;
    sw.disabled = true;
    try {
      // The seat id holds a slash, so it travels in the body (fleet.md R147 item 7).
      const out = await swarmCall(origin, "/api/seat/remote_control", { seat: s.seat, on: want });
      const live = show(out.live);
      toast(`${s.label || s.seat}: Remote Control ${want ? "on" : "off"}${live.startsWith("failed") ? ` (${live})` : ""}`,
            live.startsWith("failed"));
    } catch (e) { sw.checked = !want; toast(e.message, true); }
    sw.disabled = false;
  });
  return el("tr", { "data-seat": s.seat }, el("td", {}, s.label || s.seat), el("td", {}, s.state || ""),
    el("td", {}, el("label", {}, sw, " Remote Control")), el("td", {}, s.remote_control ? rc(s.rc_url) : null));
}

function table(heads, rows, empty) {
  return rows.length ? el("table", {}, el("tr", {}, ...heads.map((h) => el("th", {}, h))), ...rows)
    : el("div", { class: "none" }, empty);
}

async function render() {
  const roster = await swarmRoster();
  const q = $(".search").value;
  const nodes = (roster.nodes || []).filter((n) => fuzzy(q, n.name) || (n.sessions || []).some((s) => fuzzy(q, s.name)));
  $(".awm-nodes").replaceChildren(...(nodes.length ? nodes.map((n) => nodeCard(n, q))
    : [el("div", { class: "none" }, "No node matches.")]));
  const seats = (roster.seats || []).filter((s) => fuzzy(q, s.label) || fuzzy(q, s.seat));
  $(".awm-seats").replaceChildren(table(["seat", "state", "", ""], seats.map(seatRow), "No seats."));
  const services = (roster.services || []).filter((s) => fuzzy(q, s.service) || fuzzy(q, s.host));
  $(".awm-services").replaceChildren(table(["service", "host", "health", "last response", "last used"],
    services.map((s) => el("tr", {}, el("td", {}, s.service), el("td", {}, s.host),
      el("td", {}, el("span", { class: `awm-chip ${s.health === "up" ? "ok" : (s.health === "down" ? "bad" : "")}` }, s.health)),
      el("td", {}, ago(s.last_response)), el("td", {}, ago(s.last_used)))), "No services."));
  const notices = (roster.notices || []).filter((n) => fuzzy(q, `${n.kind} ${n.node} ${n.title}`));
  $(".awm-notices").replaceChildren(...(notices.length ? [el("h3", {}, "Notices"),
    ...notices.map((n) => el("details", {}, el("summary", {}, `${n.title || n.kind} - ${n.node}`),
      el("pre", {}, show(n.body))))] : []));
}

$(".search").addEventListener("input", render);
entitiesChanged(box, rosterChanged, render);
await render();
