// hello stripe frontend. Hits its own backend at ./_api/echo — the
// "./_api" prefix is the hub-mandated sub-path that proxies to the
// supervised process. Relative URLs keep the stripe location-agnostic
// (works at /hello, /hello-rework, /dev-stuff/hello, …).

const msgEl = document.getElementById("msg");
const sendEl = document.getElementById("send");
const outEl = document.getElementById("out");

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
