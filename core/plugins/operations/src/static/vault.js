// The vault pages on alcor's approver: sign-in, the approval modal, the 2fa burst, enrolment
// and device revoke. The device credential is an HttpOnly cookie, so no key lives here.

const data = JSON.parse(document.body.dataset.json || "{}");
const main = document.getElementById("main");
const status = document.getElementById("status");

function say(text, bad = false) {
  status.textContent = text;
  status.className = bad ? "bad" : "";
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "onclick") e.addEventListener("click", v);
    else e.setAttribute(k, v);
  }
  for (const k of kids) e.append(k);
  return e;
}

function b64u(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function unb64u(text) {
  const s = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

class Refused extends Error {
  constructor(status, body) {
    super(body.error || `refused (${status})`);
    this.status = status;
    this.code = body.code;
  }
}

async function api(path, body) {
  const init = body === undefined ? { credentials: "same-origin" }
    : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, init);
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Refused(r.status, out);
  return out;
}

function requestOptions(o) {
  return { ...o, challenge: unb64u(o.challenge),
           allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: unb64u(c.id) })) };
}

function creationOptions(o) {
  return { ...o, challenge: unb64u(o.challenge), user: { ...o.user, id: unb64u(o.user.id) },
           excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: unb64u(c.id) })) };
}

function credentialJSON(c) {
  const r = c.response;
  const response = { clientDataJSON: b64u(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = b64u(r.attestationObject);
    if (r.getTransports) response.transports = r.getTransports();
  } else {
    response.authenticatorData = b64u(r.authenticatorData);
    response.signature = b64u(r.signature);
    if (r.userHandle) response.userHandle = b64u(r.userHandle);
  }
  return { id: c.id, rawId: b64u(c.rawId), type: c.type, response,
           clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {} };
}

async function tap(options) {
  const c = await navigator.credentials.get({ publicKey: requestOptions(options) });
  return credentialJSON(c);
}

// The parent board hears the outcome. A target that is not the parent's origin drops it.
function tell(message) {
  if (window.parent === window) return;
  for (const o of data.board_origins || []) window.parent.postMessage(message, o);
}

async function signedIn() {
  try { await api("/api/session"); return true; } catch { return false; }
}

async function signin() {
  const begin = await api("/api/signin/begin", {});
  const assertion = await tap(begin.options);
  return api("/api/signin/finish", { nonce: begin.nonce, assertion, next: data.next || null });
}

function portalList() {
  const ul = el("ul");
  for (const p of data.portals || []) {
    const next = `https://${p.host}/`;
    ul.append(el("li", {}, el("a", { href: `/signin/go?next=${encodeURIComponent(next)}` }, `${p.host} (${p.node})`)));
  }
  return ul;
}

async function pageSignin() {
  const done = () => {
    if (data.frame) { tell({ type: "awm-signed-in" }); say("Signed in."); return; }
    main.append(el("p", {}, "Signed in. Open a surface:"), portalList(),
                el("button", { class: "plain", onclick: async () => { await api("/api/signout", {}); location.reload(); } }, "Sign out"));
    say("");
  };
  if (data.signed_in) return done();
  const button = el("button", { onclick: async () => {
    button.disabled = true;
    try {
      const out = await signin();
      if (out.redirect && !data.frame) { location.assign(out.redirect); return; }
      button.remove();
      done();
    } catch (e) { say(e.message, true); button.disabled = false; }
  } }, "Sign in with passkey");
  main.append(button);
}

function row(dl, k, v) {
  if (v === null || v === undefined || v === "") return;
  dl.append(el("dt", {}, k), el("dd", {}, typeof v === "string" ? v : JSON.stringify(v, null, 1)));
}

async function needSignin() {
  if (await signedIn()) return true;
  const button = el("button", { onclick: async () => {
    try { await signin(); location.reload(); } catch (e) { say(e.message, true); }
  } }, "Sign in with passkey");
  main.append(button);
  say("Sign in first.");
  return false;
}

async function pageApprove() {
  const id = data.request_id;
  if (!(await needSignin())) return;
  let req;
  try { req = await api(`/api/request/${encodeURIComponent(id)}`); } catch (e) { say(e.message, true); return; }
  const dl = el("dl");
  row(dl, "Kind", req.kind);
  row(dl, "Node", req.node || "alcor");
  row(dl, "Reason", req.reason);
  row(dl, "Window", req.minutes ? `${req.minutes} minutes` : "");
  if (req.payload && req.payload.plugins) row(dl, "Batch", req.payload.plugins.map((p) => `${p.plugin}@${p.commit}`).join("\n"));
  if (req.payload && Object.keys(req.payload).length) dl.append(el("dt", {}, "Payload"), el("dd", {}, el("pre", {}, JSON.stringify(req.payload, null, 1))));
  row(dl, "Status", req.status);
  main.append(dl);
  if (req.status !== "pending") { say(`This request is ${req.status}.`); tell({ type: "awm-approval", request_id: id, ok: req.status === "approved" }); return; }
  const approve = el("button", { onclick: async () => {
    approve.disabled = reject.disabled = true;
    try {
      const options = await api(`/api/request/${encodeURIComponent(id)}/options`, {});
      const assertion = await tap(options);
      await api(`/api/request/${encodeURIComponent(id)}/approve`, { assertion });
      say("Approved.");
      tell({ type: "awm-approval", request_id: id, ok: true });
    } catch (e) { say(e.message, true); approve.disabled = reject.disabled = false; }
  } }, "Approve with passkey");
  const reject = el("button", { class: "plain", onclick: async () => {
    approve.disabled = reject.disabled = true;
    try {
      await api(`/api/request/${encodeURIComponent(id)}/reject`, {});
      say("Rejected.");
      tell({ type: "awm-approval", request_id: id, ok: false });
    } catch (e) { say(e.message, true); approve.disabled = reject.disabled = false; }
  } }, "Reject");
  main.append(approve, reject);
}

async function pageTwofa() {
  if (!(await needSignin())) return;
  let device = data.device;
  if (!device) {
    try {
      const st = await api("/api/twofa");
      const list = Array.isArray(st.devices) ? st.devices : (st.devices && st.devices.devices) || [];
      if (list.length === 1) device = list[0].device || list[0].id || list[0].name;
      else if (list.length > 1) {
        const pick = el("select", { id: "device" });
        for (const d of list) { const v = d.device || d.id || d.name; pick.append(el("option", { value: v }, d.name || v)); }
        main.append(el("label", {}, "Device ", pick));
        device = null;
      }
    } catch (e) { say(e.message, true); return; }
  }
  main.append(el("p", {}, `Approve the next ${data.count} 2fa push${data.count > 1 ? "es" : ""}.`));
  const go = el("button", { onclick: async () => {
    go.disabled = true;
    const d = device || (document.getElementById("device") || {}).value || "";
    try {
      const begin = await api("/api/twofa/options", { count: data.count, device: d });
      const assertion = await tap(begin.options);
      await api("/api/twofa/burst", { nonce: begin.nonce, count: data.count, device: d, assertion });
      say("Armed.");
      tell({ type: "awm-approval", request_id: null, ok: true });
    } catch (e) { say(e.message, true); go.disabled = false; }
  } }, "Arm with passkey");
  main.append(go);
}

async function pageEnrol() {
  const token = location.hash.slice(1);
  const name = el("input", { id: "name", placeholder: "phone" });
  const go = el("button", { onclick: async () => {
    go.disabled = true;
    try {
      const begin = await api("/api/enrol/begin", { token, name: name.value });
      const cred = await navigator.credentials.create({ publicKey: creationOptions(begin.options) });
      const out = await api("/api/enrol/finish", { token, reg_id: begin.reg_id, name: name.value, credential: credentialJSON(cred) });
      say(`Enrolled as ${out.device}. This browser is signed in.`);
    } catch (e) { say(e.message, true); go.disabled = false; }
  } }, "Enrol");
  if (!token) { say("Open the enrolment link alcor printed.", true); return; }
  main.append(el("p", {}, "This registers a passkey with alcor's approver."), el("label", {}, "Device name ", name), go);
}

async function pageDevices() {
  if (!(await needSignin())) return;
  let list;
  try { list = await api("/api/devices"); } catch (e) { say(e.message, true); return; }
  const ul = el("ul");
  for (const d of list) {
    const li = el("li", {}, `${d.name || d.device_id} (${d.device_id})${d.revoked_at ? ", revoked" : ""} `);
    if (!d.revoked_at) {
      const b = el("button", { class: "plain", onclick: async () => {
        b.disabled = true;
        try {
          const begin = await api(`/api/device/${encodeURIComponent(d.device_id)}/revoke/options`, {});
          const assertion = await tap(begin.options);
          await api(`/api/device/${encodeURIComponent(d.device_id)}/revoke`, { nonce: begin.nonce, assertion });
          li.append(" revoked");
          b.remove();
        } catch (e) { say(e.message, true); b.disabled = false; }
      } }, "Revoke");
      li.append(b);
    }
    ul.append(li);
  }
  main.append(ul);
}

const pages = { signin: pageSignin, approve: pageApprove, twofa: pageTwofa, enrol: pageEnrol, devices: pageDevices };
(pages[document.body.dataset.page] || (async () => say("Unknown page.", true)))().catch((e) => say(e.message, true));
