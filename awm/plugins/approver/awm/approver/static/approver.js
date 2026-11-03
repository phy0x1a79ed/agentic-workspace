// The device side of the approver (queue.md R64, R65): the device key, the chain and passkeys.
// The private key is a non-extractable WebCrypto key kept in this origin's IndexedDB.

const enc = new TextEncoder();

export function b64u(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function unb64u(text) {
  const s = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

const hex = (bytes) => [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
const unhex = (h) => Uint8Array.from(h.match(/../g) || [], (x) => parseInt(x, 16));

// Canonical JSON, byte for byte what Python's json.dumps(sort_keys, separators=(",", ":"),
// ensure_ascii=False) gives. Every signed number is an integer.
export function canon(v) {
  if (v === null || typeof v !== "object") {
    if (typeof v === "number" && !Number.isInteger(v)) throw new Error("signed numbers are integers");
    return JSON.stringify(v);
  }
  if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
  const keys = Object.keys(v).filter((k) => v[k] !== undefined).sort();
  return "{" + keys.map((k) => JSON.stringify(k) + ":" + canon(v[k])).join(",") + "}";
}

async function sha256(...parts) {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const buf = new Uint8Array(total);
  let off = 0;
  for (const p of parts) { buf.set(p, off); off += p.length; }
  return new Uint8Array(await crypto.subtle.digest("SHA-256", buf));
}

export async function nextHead(prevHex, message, sig) {
  return hex(await sha256(unhex(prevHex), await sha256(enc.encode(canon(message))), sig));
}

async function passkeyChallenge(prevHex, request) {
  return sha256(unhex(prevHex), await sha256(enc.encode(canon(request))));
}

// -- storage ------------------------------------------------------------------

function idb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open("awm-approver", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("state");
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

async function idbDo(mode, fn) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("state", mode);
    const req = fn(tx.objectStore("state"));
    tx.oncomplete = () => resolve(req && req.result);
    tx.onerror = () => reject(tx.error);
  });
}

export const loadState = () => idbDo("readonly", (s) => s.get("device"));
const saveState = (st) => idbDo("readwrite", (s) => s.put(st, "device"));
export const forget = () => idbDo("readwrite", (s) => s.delete("device"));

// -- http ----------------------------------------------------------------------

export class Refused extends Error {
  constructor(status, body) {
    super(body.error || `refused (${status})`);
    this.status = status;
    this.code = body.code;
    this.body = body;
  }
}

export async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(body), credentials: "omit" });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Refused(r.status, out);
  return out;
}

async function signObj(st, obj) {
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, st.key, enc.encode(canon(obj)));
  return new Uint8Array(sig);
}

export async function signedGet(path) {
  const st = await loadState();
  if (!st) throw new Error("this browser is not enrolled");
  const ts = Math.floor(Date.now() / 1000);
  const sig = await signObj(st, { device: st.id, path, ts });
  const r = await fetch(path, { headers: { "X-Awm-Device": st.id, "X-Awm-Ts": String(ts),
                                           "X-Awm-Sig": b64u(sig) }, credentials: "omit" });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Refused(r.status, out);
  return out;
}

let keysCache = null;
export async function serverKeys() {
  if (!keysCache) keysCache = await (await fetch("/api/keys")).json();
  return keysCache;
}

// -- passkeys ---------------------------------------------------------------------

function creationOptions(o) {
  return {
    ...o,
    challenge: unb64u(o.challenge),
    user: { ...o.user, id: unb64u(o.user.id) },
    excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: unb64u(c.id) })),
  };
}

function requestOptions(o) {
  return { ...o, challenge: unb64u(o.challenge),
           allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: unb64u(c.id) })) };
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

export async function assertion(options) {
  const c = await navigator.credentials.get({ publicKey: requestOptions(options) });
  return credentialJSON(c);
}

// -- enrolment (R64 item 1, R85) ------------------------------------------------------

export async function enrol(token, name) {
  const begin = await post("/api/enrol/begin", { token, name });
  const cred = await navigator.credentials.create({ publicKey: creationOptions(begin.options) });
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false,
                                              ["sign", "verify"]);
  const spki = await crypto.subtle.exportKey("spki", pair.publicKey);
  const out = await post("/api/enrol/finish", { token, reg_id: begin.reg_id, name,
                                                credential: credentialJSON(cred), device_pub: b64u(spki) });
  const st = { id: out.device, name, key: pair.privateKey, ackPub: out.ack_pub, head: "", seq: 0,
               pending: null, nonce: null };
  await adoptAck(st, out.ack);
  return st;
}

// -- the chain (R65 item 4) --------------------------------------------------------------

async function checkAck(st, ack) {
  const { sig, ...body } = ack;
  const pub = await crypto.subtle.importKey("spki", unb64u(st.ackPub), { name: "ECDSA", namedCurve: "P-256" },
                                            false, ["verify"]);
  const ok = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, pub, unb64u(sig),
                                        enc.encode(canon(body)));
  if (!ok || body.device !== st.id) throw new Error("alcor's ack does not verify");
}

async function adoptAck(st, ack) {
  await checkAck(st, ack);
  st.head = ack.head;
  st.seq = ack.seq;
  st.pending = null;
  await saveState(st);
}

export class NeedPasskey extends Error {}
export class Locked extends Error {}

async function sendMessage(st, action, args, assertionJSON) {
  const message = { device: st.id, seq: st.seq + 1, prev: st.head, action, args,
                    ts: Math.floor(Date.now() / 1000) };
  const sig = b64u(await signObj(st, message));
  st.pending = { message, sig, assertion: assertionJSON || null };
  await saveState(st);
  let out;
  try {
    out = await post("/api/act", { message, sig, assertion: assertionJSON || null });
  } catch (e) {
    if (e instanceof Refused) {
      st.pending = null;
      if (e.code === "locked") { st.nonce = e.body.nonce; await saveState(st); throw new Locked(e.message); }
      await saveState(st);
      if (e.code === "need_passkey") throw new NeedPasskey(e.message);
      if (e.code === "stale_head") await resume(st);
    }
    throw e;  // a network error keeps the pending message for resume()
  }
  const want = await nextHead(message.prev, message, unb64u(sig));
  if (out.ack.head !== want || out.ack.seq !== message.seq) throw new Error("the ack names another head");
  await adoptAck(st, out.ack);
  return out.result;
}

export async function resume(st) {
  const ts = Math.floor(Date.now() / 1000);
  const sig = b64u(await signObj(st, { device: st.id, acked_head: st.head, ts }));
  const out = await post("/api/resume", { device: st.id, acked_head: st.head, ts, sig, pending: st.pending });
  if (out.state === "locked") {
    st.nonce = out.nonce;
    st.pending = null;
    await saveState(st);
    throw new Locked("this device is locked: unlock it with the passkey");
  }
  if (out.state === "acked" || out.state === "taken") await adoptAck(st, out.ack);
  else { st.pending = null; await saveState(st); }
  return out;
}

export async function unlock() {
  const st = await loadState();
  const options = await post("/api/unlock/options", { device: st.id });
  const a = await assertion(options);
  const sig = b64u(await signObj(st, { device: st.id, nonce: st.nonce }));
  const out = await post("/api/unlock", { device: st.id, assertion: a, sig });
  st.nonce = null;
  await adoptAck(st, out.ack);
}

async function ready() {
  const st = await loadState();
  if (!st) throw new Error("this browser is not enrolled");
  if (st.nonce) throw new Locked("this device is locked: unlock it with the passkey");
  let resumed = null;
  if (st.pending) {
    const lost = st.pending.message;
    const out = await resume(st);
    if (out.state === "acked" || out.state === "taken") resumed = { lost, result: out.result };
  }
  return { st, resumed };
}

// The press that follows a drop may repeat the lost message. alcor took that one already.
const same = (r, action, args) => r && r.lost.action === action && canon(r.lost.args) === canon(args);

// One passkey prompt authorizes a request: a batch of actions, a window, or both (R64).
export async function authorize(request) {
  const { st, resumed } = await ready();
  if (same(resumed, "authorize", request)) return resumed.result;
  const options = await post("/api/passkey/options", { prev: st.head, request });
  const want = b64u(await passkeyChallenge(st.head, request));
  if (options.challenge !== want) throw new Error("alcor offered another challenge");
  return sendMessage(st, "authorize", request, await assertion(options));
}

// Sign one action. Inside an open window it goes with no prompt. Otherwise it goes as a
// one-action request, which asks for the passkey once.
export async function act(action, args) {
  const { st, resumed } = await ready();
  if (same(resumed, action, args)) return resumed.result;
  if (resumed && same(resumed, "authorize", { batch: [{ action, args }] })) return resumed.result.batch[0];
  try {
    return await sendMessage(st, action, args, null);
  } catch (e) {
    if (!(e instanceof NeedPasskey)) throw e;
  }
  const out = await authorize({ batch: [{ action, args }] });
  return out.batch[0];
}
