// hello stripe frontend. Hits its own backend at ./_api/echo — the
// "./_api" prefix is the hub-mandated sub-path that proxies to the
// supervised process. Relative URLs keep the stripe location-agnostic
// (works at /hello, /hello-rework, /dev-stuff/hello, …).
//
// Identity: the hub mints X-Awm-As from the awm_as cookie on every
// proxy hop, so the backend already knows who's calling. The frontend
// just needs to *display* the operator — we fetch /auth/whoami once.
// A real bundled stripe would `import { getOperator } from '@awm/bus'`
// to dedupe across components; hello is hand-written vanilla JS so we
// inline the same one-shot fetch.

const msgEl = document.getElementById("msg");
const sendEl = document.getElementById("send");
const outEl = document.getElementById("out");
const whoEl = document.getElementById("who");

(async () => {
  try {
    const r = await fetch("/auth/whoami", { credentials: "include" });
    if (r.ok) {
      const j = await r.json();
      whoEl.textContent = (j && typeof j.awm_user === "string") ? j.awm_user : "operator";
      return;
    }
  } catch { /* fall through */ }
  whoEl.textContent = "operator";
})();

async function call() {
  const msg = encodeURIComponent(msgEl.value);
  outEl.textContent = "(loading)";
  try {
    const r = await fetch(`./_api/echo?msg=${msg}`, { credentials: "include" });
    const text = await r.text();
    let body;
    try { body = JSON.stringify(JSON.parse(text), null, 2); }
    catch { body = text; }
    outEl.textContent = `HTTP ${r.status}\n${body}`;
  } catch (err) {
    outEl.textContent = `network error: ${err}`;
  }
}

sendEl.addEventListener("click", call);
msgEl.addEventListener("keydown", (e) => { if (e.key === "Enter") call(); });
call();
