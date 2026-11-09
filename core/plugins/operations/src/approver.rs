//! The approver on alcor: every action Tony takes passes through here (fleet.md R97,
//! queue.md R64, R65, R84).
//!
//! Two proofs meet here. The device credential, a cookie that one passkey tap sets for 12
//! hours, carries Tony's everyday actions. A passkey assertion bound to one request carries
//! each approval. Once an action passes, the approver alone turns it into a store action, a
//! 2fa call, a grant or an order.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_trust::grant::{b64u, canonical};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::ids::token;
use crate::keys::Keys;
use crate::passkeys;
use crate::relay::async_trait_free::BoxFut;
use crate::store::Store;

pub const REQUEST_TTL_S: f64 = 24.0 * 3600.0;
pub const ENROL_LINK_TTL_S: f64 = 15.0 * 60.0;
pub const ORDER_TTL_S: i64 = 10 * 60;
pub const CHALLENGE_TTL_S: f64 = 5.0 * 60.0;
pub const LOGIN_GRANT_S: i64 = 60;
/// One passkey tap opens a 12-hour sign-in (fleet.md R84).
pub const SESSION_TTL_S: f64 = 12.0 * 3600.0;
/// Where a portal's edge takes the grant back (the auth front's callback).
pub const PORTAL_CALLBACK: &str = "/__auth/portal";
pub const MINUTES_MIN: i64 = 15;
pub const MINUTES_MAX: i64 = 1440;
pub const BURST_COUNTS: [i64; 3] = [1, 2, 4];
/// The order kinds alcor signs. `agent` replaces spawn and new_from (CONTRACT.md b11adebc).
pub const ORDER_KINDS: [&str; 2] = ["agent", "service"];
/// How long one passkey approval keeps the Ops service switches open.
pub const SERVICE_WINDOW_S: f64 = 300.0;
/// The kinds whose grant opens a window for its minutes, so the summary names them.
const WINDOW_KINDS: [&str; 4] = ["admin_ssh", "trust_raise", "browser_signin", "vault_hold"];

/// What a relay may ask for, and device_enrol, which only `enrol_ask` opens.
pub fn payload_fields(kind: &str) -> Option<&'static [&'static str]> {
    if kind == "device_enrol" {
        return Some(&["name"]);
    }
    crate::api::PAYLOADS.iter().find(|(k, _)| *k == kind).map(|(_, v)| *v)
}

pub fn request_kinds() -> Vec<&'static str> {
    crate::api::PAYLOADS.iter().map(|(k, _)| *k).chain(["device_enrol"]).collect()
}

#[derive(Debug, Clone, thiserror::Error)]
#[error("{message}")]
pub struct ApproverError {
    pub code: &'static str,
    pub message: String,
    pub extra: Map<String, Value>,
}

impl ApproverError {
    pub fn new(code: &'static str, message: impl Into<String>) -> ApproverError {
        ApproverError { code, message: message.into(), extra: Map::new() }
    }

    pub fn as_json(&self) -> Value {
        let mut m = Map::from_iter([("ok".into(), json!(false)), ("code".into(), json!(self.code)), ("error".into(), json!(self.message))]);
        m.extend(self.extra.clone());
        Value::Object(m)
    }

    /// The HTTP status the vault API answers this refusal with.
    pub fn status(&self) -> u16 {
        match self.code {
            "need_passkey" | "need_signin" => 401,
            "unknown_device" | "unknown_passkey" | "bad_passkey" | "not_allowed" | "not_requester" | "bad_origin" => 403,
            "unknown_request" | "unknown_task" => 404,
            "decided" => 409,
            "rate" => 429,
            _ => 400,
        }
    }
}

impl From<rusqlite::Error> for ApproverError {
    fn from(e: rusqlite::Error) -> Self {
        ApproverError::new("failed", e.to_string())
    }
}

impl From<crate::store::QueueError> for ApproverError {
    fn from(e: crate::store::QueueError) -> Self {
        match e {
            crate::store::QueueError::Refused(m) => ApproverError::new("bad_request", m),
            other => ApproverError::new("failed", other.to_string()),
        }
    }
}

pub type AResult<T> = Result<T, ApproverError>;

const AGENT_MODELS: [&str; 4] = ["opus", "sonnet", "haiku", "fable"];

fn bad(msg: impl Into<String>) -> ApproverError {
    ApproverError::new("bad_request", msg)
}

fn sha_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

/// The 2fa calls the modal makes, through this node's gateway.
pub trait Tools: Send + Sync {
    fn twofa<'a>(&'a self, verb: &'a str, args: Value) -> BoxFut<'a, Result<Value, String>>;
}

#[derive(Debug, Clone, Default)]
pub struct Config {
    pub origin: String,
    /// Order kind -> the nodes it may go to, or `*`.
    pub allowed_orders: HashMap<String, Vec<String>>,
    pub rate_max: i64,
    /// The pages that may ask for a sign-in: host -> the node whose edge it is.
    pub portals: HashMap<String, String>,
}

impl Config {
    pub fn rp_id(&self) -> String {
        url::Url::parse(&self.origin).ok().and_then(|u| u.host_str().map(str::to_string)).unwrap_or_default()
    }
}

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, name TEXT, device_pub TEXT NOT NULL,
    created REAL, revoked_at REAL, locked_nonce TEXT, seq INTEGER NOT NULL, head TEXT NOT NULL,
    last_ack TEXT, last_result TEXT, last_ip TEXT, enrolled_by TEXT);
CREATE TABLE IF NOT EXISTS credentials (cred_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
    public_key TEXT NOT NULL, sign_count INTEGER NOT NULL, created REAL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS enrol_links (token_sha TEXT PRIMARY KEY, expires REAL NOT NULL,
    used_at REAL, request_id TEXT);
CREATE TABLE IF NOT EXISTS reg_challenges (reg_id TEXT PRIMARY KEY, challenge TEXT NOT NULL,
    token_sha TEXT NOT NULL, user_id TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, task_id TEXT, kind TEXT NOT NULL,
    node TEXT, reason TEXT, minutes INTEGER, payload TEXT, requester_session TEXT,
    created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, decided_at REAL,
    device TEXT, grant_env TEXT, cert TEXT, poll_secret_sha TEXT, enrol_token TEXT);
CREATE TABLE IF NOT EXISTS windows (window_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
    kinds TEXT NOT NULL, cards TEXT NOT NULL, opened REAL NOT NULL, not_after REAL NOT NULL,
    ip TEXT, closed_at REAL, closed_reason TEXT);
CREATE TABLE IF NOT EXISTS action_log (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT,
    ts REAL, action TEXT);
CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, kind TEXT NOT NULL, node TEXT NOT NULL,
    payload TEXT, envelope TEXT NOT NULL, created REAL NOT NULL, not_after REAL NOT NULL,
    pulled_at REAL, done_at REAL, ok INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS logins (nonce TEXT PRIMARY KEY, site TEXT, node TEXT, return_url TEXT,
    created REAL NOT NULL, used_at REAL);
CREATE TABLE IF NOT EXISTS device_sessions (id_sha TEXT PRIMARY KEY, device TEXT NOT NULL,
    created REAL NOT NULL, expires REAL NOT NULL, ip TEXT);
";

fn valid_node(n: &str) -> bool {
    let b = n.as_bytes();
    !b.is_empty() && b.len() <= 63 && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

fn valid_request_id(r: &str) -> bool {
    r.len() == 13 && r.starts_with('q') && r[1..].bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn is_commit(s: &str) -> bool {
    s.len() == 40 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn valid_plugin(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty() && b.len() <= 64 && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"._-".contains(c))
}

fn ssh_pub_line(s: &str) -> bool {
    let mut parts = s.trim().splitn(3, ' ');
    let (Some(t), Some(k)) = (parts.next(), parts.next()) else { return false };
    ["ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"].contains(&t)
        && !k.is_empty()
        && k.bytes().all(|b| b.is_ascii_alphanumeric() || b"+/=".contains(&b))
}

/// The challenge an approval's passkey signs: one request, never another (CONTRACT.md).
pub fn approval_challenge(request_id: &str, kind: &str, payload: &Value) -> Vec<u8> {
    Sha256::digest(canonical(&json!({"request_id": request_id, "kind": kind, "payload": payload}))).to_vec()
}

/// The challenge a 2fa burst's passkey signs, bound to one modal's nonce.
pub fn burst_challenge(nonce: &str, count: i64, device: &str) -> Vec<u8> {
    Sha256::digest(canonical(&json!({"kind": "twofa_burst", "count": count, "device": device, "nonce": nonce}))).to_vec()
}

/// The challenge a sign-in's passkey signs.
pub fn signin_challenge(nonce: &str) -> Vec<u8> {
    let mut h = Sha256::new();
    h.update(b"signin\0");
    h.update(nonce.as_bytes());
    h.finalize().to_vec()
}

pub struct Approver {
    pub path: PathBuf,
    db: Mutex<Connection>,
    pub keys: Keys,
    pub config: Config,
    pub store: Arc<Store>,
    tools: Arc<dyn Tools>,
    clock: crate::store::Clock,
    changed: Mutex<HashMap<String, Arc<tokio::sync::Notify>>>,
    /// Open sign-in and burst nonces, with the time each was issued.
    nonces: Mutex<HashMap<String, f64>>,
}

impl Approver {
    /// Open `approver.db`. A missing file is refused unless `create`: a fresh one would drop
    /// every enrolled passkey and lock Tony out.
    pub fn open(path: &Path, keys: Keys, config: Config, store: Arc<Store>, tools: Arc<dyn Tools>, create: bool, clock: crate::store::Clock) -> anyhow::Result<Approver> {
        if !path.exists() && !create {
            anyhow::bail!("{} is missing: refusing to start with no enrolled devices (set AWM_OPERATIONS_INIT=1 to create one)", path.display());
        }
        if let Some(d) = path.parent() {
            std::fs::create_dir_all(d)?;
        }
        let db = Connection::open(path)?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "busy_timeout", 5000)?;
        db.execute_batch(SCHEMA)?;
        Ok(Approver {
            path: path.into(), db: Mutex::new(db), keys, config, store, tools, clock,
            changed: Mutex::new(HashMap::new()), nonces: Mutex::new(HashMap::new()),
        })
    }

    fn now(&self) -> f64 {
        (self.clock)()
    }

    fn conn(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.db.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn notify(&self, key: &str) -> Arc<tokio::sync::Notify> {
        self.changed.lock().expect("notify lock").entry(key.into()).or_insert_with(|| Arc::new(tokio::sync::Notify::new())).clone()
    }

    fn signal(&self, key: &str) {
        self.notify(key).notify_waiters();
    }

    fn active_credentials(&self) -> AResult<Vec<String>> {
        let db = self.conn();
        let mut st = db.prepare("SELECT c.cred_id FROM credentials c JOIN devices d USING(device_id) WHERE c.revoked_at IS NULL AND d.revoked_at IS NULL")?;
        let ids = st.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
        Ok(ids)
    }

    pub fn passkey_options(&self, challenge: &[u8]) -> AResult<Value> {
        Ok(passkeys::assertion_options(&self.config.rp_id(), challenge, &self.active_credentials()?))
    }

    /// The device behind a valid assertion over `challenge`.
    pub fn check_passkey(&self, assertion: &Value, challenge: &[u8]) -> AResult<String> {
        if !assertion.is_object() {
            return Err(ApproverError::new("need_passkey", "this needs a passkey assertion"));
        }
        let id = assertion.get("id").and_then(Value::as_str).unwrap_or_default();
        let cred: Option<(String, String, i64)> = self.conn().query_row(
            "SELECT c.device_id, c.public_key, c.sign_count FROM credentials c JOIN devices d USING(device_id) \
             WHERE c.cred_id=? AND c.revoked_at IS NULL AND d.revoked_at IS NULL",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        ).optional()?;
        let Some((device, public_key, count)) = cred else { return Err(ApproverError::new("unknown_passkey", "that passkey is not enrolled")) };
        let new = passkeys::verify_assertion(assertion, challenge, &self.config.origin, &self.config.rp_id(), &public_key, count as u32)
            .map_err(|e| ApproverError::new("bad_passkey", e.to_string()))?;
        self.conn().execute("UPDATE credentials SET sign_count=? WHERE cred_id=?", params![new as i64, id])?;
        Ok(device)
    }

    // -- nonces for the sign-in and the burst --------------------------------------------

    pub fn nonce(&self) -> String {
        let n = token(16);
        let now = self.now();
        let mut m = self.nonces.lock().expect("nonce lock");
        m.retain(|_, t| now - *t < CHALLENGE_TTL_S);
        m.insert(n.clone(), now);
        n
    }

    /// Spend one nonce: a second use, or one past its five minutes, is refused.
    pub fn spend(&self, n: &str) -> AResult<()> {
        let now = self.now();
        match self.nonces.lock().expect("nonce lock").remove(n) {
            Some(t) if now - t < CHALLENGE_TTL_S => Ok(()),
            _ => Err(ApproverError::new("bad_login", "the sign-in is unknown, used or stale")),
        }
    }

    // -- enrolment (R64 item 1, R85) ----------------------------------------------------------

    /// A one-time enrolment link. Only the operator socket and an approved request call this.
    pub fn enrol_link(&self, minutes: i64, request_id: Option<&str>) -> AResult<Value> {
        let t = token(24);
        let expires = self.now() + ((minutes * 60) as f64).min(ENROL_LINK_TTL_S);
        self.conn().execute("INSERT INTO enrol_links VALUES (?,?,NULL,?)", params![sha_hex(&t), expires, request_id])?;
        Ok(json!({"url": format!("{}/enrol#{t}", self.config.origin), "token": t, "expires": expires}))
    }

    fn link(&self, t: &str) -> AResult<(String, Option<String>)> {
        let row: Option<(String, f64, Option<f64>, Option<String>)> = self.conn().query_row(
            "SELECT token_sha, expires, used_at, request_id FROM enrol_links WHERE token_sha=?",
            [sha_hex(t)],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        ).optional()?;
        match row {
            Some((sha, exp, None, rid)) if exp > self.now() => Ok((sha, rid)),
            _ => Err(ApproverError::new("bad_link", "the enrolment link is used, expired or unknown")),
        }
    }

    pub fn enrol_begin(&self, t: &str, name: &str) -> AResult<Value> {
        self.link(t)?;
        let (reg_id, challenge, user_id) = (token(12), rand_bytes(32), rand_bytes(16));
        self.conn().execute(
            "INSERT INTO reg_challenges VALUES (?,?,?,?,?)",
            params![reg_id, hex::encode(&challenge), sha_hex(t), b64u(&user_id), self.now()],
        )?;
        let label = format!("tony ({})", if name.is_empty() { "device" } else { name });
        Ok(json!({"reg_id": reg_id, "options": passkeys::registration_options(&self.config.rp_id(), &user_id, &label, &challenge)}))
    }

    /// Register the passkey, enrol the device, and answer the device id. The device holds no
    /// key of its own: its sign-in is the cookie the vault API sets.
    pub fn enrol_finish(&self, t: &str, reg_id: &str, credential: &Value, name: &str, ip: Option<&str>) -> AResult<Value> {
        let reg: Option<(String, String, f64)> = self.conn().query_row(
            "SELECT challenge, token_sha, created FROM reg_challenges WHERE reg_id=?",
            [reg_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        ).optional()?;
        let Some((challenge, token_sha, created)) = reg.filter(|r| r.1 == sha_hex(t) && r.2 >= self.now() - ENROL_LINK_TTL_S) else {
            return Err(ApproverError::new("bad_link", "the enrolment session is unknown or stale"));
        };
        let _ = (token_sha, created);
        let (sha, request_id) = self.link(t)?;
        let challenge = hex::decode(challenge).map_err(|e| bad(e.to_string()))?;
        let (cred_id, public_key, count) = passkeys::verify_registration(credential, &challenge, &self.config.origin, &self.config.rp_id())
            .map_err(|e| ApproverError::new("bad_passkey", e.to_string()))?;
        let now = self.now();
        let mut db = self.conn();
        let tx = db.transaction()?;
        if tx.execute("UPDATE enrol_links SET used_at=? WHERE token_sha=? AND used_at IS NULL", params![now, sha])? != 1 {
            return Err(ApproverError::new("bad_link", "the enrolment link is used"));
        }
        tx.execute("DELETE FROM reg_challenges WHERE reg_id=?", [reg_id])?;
        let device_id = format!("d{}", token(9));
        tx.execute(
            "INSERT INTO devices VALUES (?,?,'',?,NULL,NULL,0,?,NULL,NULL,?,?)",
            params![device_id, if name.is_empty() { "device" } else { name }, now, hex::encode(rand_bytes(32)), ip, request_id.unwrap_or_else(|| "link".into())],
        )?;
        tx.execute("INSERT INTO credentials VALUES (?,?,?,?,?,NULL)", params![cred_id, device_id, public_key, count as i64, now])?;
        tx.commit()?;
        Ok(json!({"device": device_id}))
    }

    /// A new device asks to enrol. Tony approves the request in the modal (R64 item 1).
    pub async fn enrol_ask(&self, name: &str) -> AResult<Value> {
        let secret = token(24);
        let mut out = self.approval_request("device_enrol", None, &format!("enrol device {}", if name.is_empty() { "unnamed" } else { name }),
                                            15, json!({"name": name}), None, None).await?;
        self.conn().execute("UPDATE requests SET poll_secret_sha=? WHERE request_id=?", params![sha_hex(&secret), out["request_id"].as_str()])?;
        out["poll_secret"] = json!(secret);
        Ok(out)
    }

    pub fn enrol_ask_status(&self, request_id: &str, poll_secret: &str) -> AResult<Value> {
        let row: Option<(Option<String>, String, Option<String>, f64)> = self.conn().query_row(
            "SELECT poll_secret_sha, status, enrol_token, expires FROM requests WHERE request_id=? AND kind='device_enrol'",
            [request_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        ).optional()?;
        let ok = row.as_ref().and_then(|r| r.0.as_ref()).is_some_and(|s| {
            use subtle::ConstantTimeEq;
            s.as_bytes().ct_eq(sha_hex(poll_secret).as_bytes()).into()
        });
        let Some((_, status, enrol, expires)) = row.filter(|_| ok) else { return Err(ApproverError::new("unknown_request", "no such enrolment request")) };
        let mut t = Value::Null;
        if status == "approved" {
            if let Some(e) = enrol {
                t = json!(e);
                self.conn().execute("UPDATE requests SET enrol_token=NULL WHERE request_id=?", [request_id])?;
            }
        }
        let status = if status == "pending" && expires <= self.now() { "expired".to_string() } else { status };
        Ok(json!({"status": status, "token": t}))
    }

    // -- the device credential (R84 items 1-2) ------------------------------------------------

    /// A new 12-hour sign-in for `device`. Answers the cookie value; only its hash is kept.
    pub fn session_new(&self, device: &str, ip: Option<&str>) -> AResult<String> {
        let raw = rand_bytes(32);
        let value = b64u(&raw);
        let now = self.now();
        let db = self.conn();
        db.execute("DELETE FROM device_sessions WHERE expires<=?", [now])?;
        db.execute("INSERT INTO device_sessions VALUES (?,?,?,?,?)", params![hex::encode(Sha256::digest(&raw)), device, now, now + SESSION_TTL_S, ip])?;
        Ok(value)
    }

    /// The device a live cookie belongs to, or None.
    pub fn session_device(&self, cookie: &str) -> AResult<Option<String>> {
        let Some(raw) = awm_trust::grant::unb64u(cookie).filter(|r| r.len() == 32) else { return Ok(None) };
        let row: Option<String> = self.conn().query_row(
            "SELECT s.device FROM device_sessions s JOIN devices d ON d.device_id=s.device WHERE s.id_sha=? AND s.expires>? AND d.revoked_at IS NULL",
            params![hex::encode(Sha256::digest(&raw)), self.now()],
            |r| r.get(0),
        ).optional()?;
        Ok(row)
    }

    pub fn session_end(&self, cookie: &str) -> AResult<()> {
        if let Some(raw) = awm_trust::grant::unb64u(cookie) {
            self.conn().execute("DELETE FROM device_sessions WHERE id_sha=?", [hex::encode(Sha256::digest(&raw))])?;
        }
        Ok(())
    }

    /// At most `rate_max` everyday actions a minute from one device (a risk signal of R64).
    pub fn note_action(&self, device: &str, action: &str) -> AResult<()> {
        let now = self.now();
        let db = self.conn();
        let recent: i64 = db.query_row("SELECT COUNT(*) FROM action_log WHERE device_id=? AND ts>?", params![device, now - 60.0], |r| r.get(0))?;
        if self.config.rate_max > 0 && recent >= self.config.rate_max {
            return Err(ApproverError::new("rate", "risk: unusual rate, try again in a minute"));
        }
        db.execute("INSERT INTO action_log(device_id, ts, action) VALUES (?,?,?)", params![device, now, action])?;
        Ok(())
    }

    /// The sign-in's passkey options, under a fresh nonce.
    pub fn signin_begin(&self) -> AResult<Value> {
        let n = self.nonce();
        Ok(json!({"nonce": n, "options": self.passkey_options(&signin_challenge(&n))?}))
    }

    /// One tap: the device behind it, and its new sign-in cookie.
    pub fn signin_finish(&self, nonce: &str, assertion: &Value, ip: Option<&str>) -> AResult<(String, String)> {
        self.spend(nonce)?;
        let device = self.check_passkey(assertion, &signin_challenge(nonce))?;
        let cookie = self.session_new(&device, ip)?;
        Ok((device, cookie))
    }

    /// `(host, node, path)` for a page that may ask for a sign-in, from the portals map.
    pub fn portal(&self, next_url: &str) -> AResult<(String, String, String)> {
        let refuse = || bad("next is an https page on a known portal");
        let u = url::Url::parse(next_url).map_err(|_| refuse())?;
        let host = u.host_str().ok_or_else(refuse)?.to_string();
        let node = self.config.portals.get(&host).ok_or_else(refuse)?.clone();
        let path = u.path().to_string();
        let path = if path.is_empty() { "/".into() } else { path };
        if u.scheme() != "https" || u.port().is_some_and(|p| p != 443) || !path.starts_with('/') || path.starts_with("//") {
            return Err(refuse());
        }
        Ok((host, node, path))
    }

    /// A short `portal_login` grant for one page, and the edge callback that spends it.
    pub fn portal_grant(&self, next_url: &str, device: &str) -> AResult<String> {
        let (host, node, ret) = self.portal(next_url)?;
        let now = self.now() as i64;
        let body = json!({
            "grant_id": format!("g{}", token(9)), "request_id": "", "kind": "portal_login", "node": node, "subject": "tony",
            "payload_sha256": hex::encode(Sha256::digest(canonical(&json!({"site": host, "return": ret})))),
            "not_before": now, "not_after": now + LOGIN_GRANT_S, "approved_by_device": device,
        });
        let grant = self.keys.envelope("grant", body);
        let encoded = b64u(serde_json::to_string(&grant).unwrap_or_default().as_bytes());
        let query = url::form_urlencoded::Serializer::new(String::new()).append_pair("grant", &encoded).append_pair("next", &ret).finish();
        Ok(format!("https://{host}{PORTAL_CALLBACK}?{query}"))
    }

    // -- approval requests (fleet.md R97) -----------------------------------------------------

    fn check_payload(kind: &str, node: Option<&str>, payload: &Map<String, Value>) -> AResult<()> {
        let fields = payload_fields(kind).unwrap_or(&[]);
        let missing: Vec<&str> = fields.iter().copied().filter(|k| !payload.contains_key(*k)).collect();
        if !missing.is_empty() {
            return Err(bad(format!("{kind} payload lacks {missing:?}")));
        }
        if kind == "admin_ssh" {
            let ok = payload["nodes"].as_array().is_some_and(|n| !n.is_empty() && n.iter().all(|x| x.as_str().is_some_and(valid_node)));
            if !ok {
                return Err(bad("nodes is a list of node names"));
            }
            if !payload["pubkey"].as_str().is_some_and(ssh_pub_line) {
                return Err(bad("pubkey is one OpenSSH public key line"));
            }
        }
        if kind == "plugin_commits" {
            let plugins = payload["plugins"].as_array().filter(|p| !p.is_empty()).ok_or_else(|| bad("plugins is a list of {plugin, commit}"))?;
            let mut names = std::collections::HashSet::new();
            for p in plugins {
                let Some(o) = p.as_object() else { return Err(bad("plugins is a list of {plugin, commit}")) };
                let keys_ok = o.keys().all(|k| ["plugin", "commit", "source", "from"].contains(&k.as_str()));
                let s = |k: &str| o.get(k).and_then(Value::as_str).unwrap_or_default();
                if !keys_ok || !valid_plugin(s("plugin")) || !is_commit(s("commit")) || (o.contains_key("from") && !is_commit(s("from"))) {
                    return Err(bad("a plugin entry is {plugin, commit, source?, from?} with full 40-hex commits"));
                }
                names.insert(s("plugin").to_string());
            }
            if names.len() != plugins.len() {
                return Err(bad("a change pins each plugin once"));
            }
        }
        if node.is_some_and(|n| !valid_node(n)) {
            return Err(bad("node is a node name"));
        }
        Ok(())
    }

    /// Open a request on the requester's own card (R97 item 1). A relay may pass the id it
    /// minted while alcor was away.
    #[allow(clippy::too_many_arguments)]
    pub async fn approval_request(&self, kind: &str, node: Option<&str>, reason: &str, minutes: i64, payload: Value, requester_session: Option<&str>, request_id: Option<&str>) -> AResult<Value> {
        if payload_fields(kind).is_none() {
            return Err(bad(format!("kind is one of {:?}", request_kinds())));
        }
        if !(MINUTES_MIN..=MINUTES_MAX).contains(&minutes) {
            return Err(bad(format!("minutes is {MINUTES_MIN} to {MINUTES_MAX}")));
        }
        let payload = match payload {
            Value::Object(m) => m,
            Value::Null => Map::new(),
            _ => return Err(bad("payload is an object")),
        };
        Approver::check_payload(kind, node, &payload)?;
        let request_id = match request_id {
            None => format!("q{}", token(9)),
            Some(r) if valid_request_id(r) => r.to_string(),
            Some(_) => return Err(bad("request_id is q plus 12 URL-safe characters")),
        };
        let taken = self.conn().query_row("SELECT 1 FROM requests WHERE request_id=?", [&request_id], |_| Ok(())).optional()?.is_some();
        if taken {
            return Err(bad(format!("request id {request_id} is taken")));
        }
        let where_ = node.map(str::to_string).or_else(|| {
            payload.get("nodes").and_then(Value::as_array).map(|n| n.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(","))
        }).filter(|s| !s.is_empty()).unwrap_or_else(|| "alcor".into());
        let title: String = format!("{kind} on {where_}: {reason}").trim().trim_end_matches(':').chars().take(160).collect();
        let summary = summary_of(kind, reason, minutes);
        let now = self.now();
        let store = self.store.clone();
        let (rs, rid, t, n, s) = (requester_session.map(str::to_string), request_id.clone(), title, node.map(str::to_string), summary);
        let card = tokio::task::spawn_blocking(move || store.approval_attach(rs.as_deref(), &rid, &t, n.as_deref(), &s))
            .await
            .map_err(|e| ApproverError::new("failed", e.to_string()))??;
        let task_id = card["task_id"].as_str().unwrap_or_default().to_string();
        self.conn().execute(
            "INSERT INTO requests(request_id, task_id, kind, node, reason, minutes, payload, requester_session, created, expires, status) \
             VALUES (?,?,?,?,?,?,?,?,?,?,'pending')",
            params![request_id, task_id, kind, node, reason, minutes, Value::Object(payload).to_string(), requester_session, now, now + REQUEST_TTL_S],
        )?;
        self.signal("sweep");
        Ok(json!({"request_id": request_id, "task_id": task_id, "expires": now + REQUEST_TTL_S}))
    }

    fn request_row(&self, request_id: &str) -> AResult<Map<String, Value>> {
        let db = self.conn();
        let mut st = db.prepare(
            "SELECT request_id, task_id, kind, node, reason, minutes, requester_session, created, expires, status, decided_at, cert, payload, grant_env, device \
             FROM requests WHERE request_id=?",
        )?;
        let row = st.query_row([request_id], |r| {
            let mut m = Map::new();
            for (i, k) in ["request_id", "task_id", "kind", "node", "reason", "minutes", "requester_session", "created", "expires", "status", "decided_at", "cert", "payload", "grant_env", "device"].iter().enumerate() {
                let v: rusqlite::types::Value = r.get(i)?;
                m.insert((*k).into(), sql_json(v));
            }
            Ok(m)
        }).optional()?;
        row.ok_or_else(|| ApproverError::new("unknown_request", "no such request"))
    }

    /// A request as its requester sees it. A pending request past its expiry reads expired.
    pub fn approval_get(&self, request_id: &str) -> AResult<Value> {
        let mut r = self.request_row(request_id)?;
        if r["status"] == "pending" && r["expires"].as_f64().unwrap_or(0.0) <= self.now() {
            r.insert("status".into(), json!("expired"));
        }
        let payload = r.remove("payload").and_then(|p| p.as_str().and_then(|s| serde_json::from_str(s).ok())).unwrap_or(json!({}));
        let grant = r.remove("grant_env").and_then(|p| p.as_str().and_then(|s| serde_json::from_str::<Value>(s).ok())).unwrap_or(Value::Null);
        r.remove("device");
        r.insert("payload".into(), payload);
        r.insert("grant".into(), grant);
        r.insert("ssh_cert".into(), r.get("cert").cloned().unwrap_or(Value::Null));
        Ok(Value::Object(r))
    }

    /// The modal's view of a request: exactly what it asks.
    pub fn approval_view(&self, request_id: &str) -> AResult<Value> {
        let mut out = self.approval_get(request_id)?;
        if let Some(m) = out.as_object_mut() {
            m.remove("grant");
            m.remove("ssh_cert");
            m.remove("cert");
        }
        Ok(out)
    }

    pub async fn approval_wait(&self, request_id: &str, wait_s: f64) -> AResult<Value> {
        let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(wait_s.clamp(0.0, 55.0));
        loop {
            let n = self.notify(&format!("req:{request_id}"));
            let wait = n.notified();
            let out = self.approval_get(request_id)?;
            if out["status"] != "pending" || tokio::time::Instant::now() >= deadline {
                return Ok(out);
            }
            let _ = tokio::time::timeout_at(deadline, wait).await;
        }
    }

    /// Withdraw a request. Only the session that made it may (fleet.md R97 item 1).
    pub async fn approval_cancel(&self, request_id: &str, session_id: Option<&str>) -> AResult<Value> {
        let r = self.request_row(request_id)?;
        if session_id.is_none() || r["requester_session"].as_str() != session_id {
            return Err(ApproverError::new("not_requester", "only the session that made a request cancels it"));
        }
        self.finish(request_id, "cancelled", None, &[]).await
    }

    async fn finish(&self, request_id: &str, outcome: &str, device: Option<&str>, cols: &[(&str, Value)]) -> AResult<Value> {
        let r = self.request_row(request_id)?;
        let sets: String = cols.iter().map(|(k, _)| format!(", {k}=?")).collect();
        let mut args: Vec<rusqlite::types::Value> = vec![outcome.to_string().into(), self.now().into(), device.map(str::to_string).into()];
        for (_, v) in cols {
            args.push(match v {
                Value::String(s) => s.clone().into(),
                Value::Null => rusqlite::types::Value::Null,
                other => other.to_string().into(),
            });
        }
        args.push(request_id.to_string().into());
        let n = self.conn().execute(
            &format!("UPDATE requests SET status=?, decided_at=?, device=?{sets} WHERE request_id=? AND status='pending'"),
            rusqlite::params_from_iter(args),
        )?;
        if n != 1 {
            return Err(ApproverError::new("decided", format!("the request is already {}", r["status"].as_str().unwrap_or_default())));
        }
        if let Some(task) = r["task_id"].as_str() {
            let others: i64 = self.conn().query_row(
                "SELECT COUNT(*) FROM requests WHERE task_id=? AND status='pending' AND expires>? AND request_id<>?",
                params![task, self.now(), request_id],
                |x| x.get(0),
            )?;
            let (store, task, rid, out) = (self.store.clone(), task.to_string(), request_id.to_string(), outcome.to_string());
            let moved = tokio::task::spawn_blocking(move || store.approval_resolve(&task, &rid, &out, others > 0)).await;
            if let Ok(Err(e)) | Err(e) = moved.map_err(|e| crate::store::QueueError::Refused(e.to_string())) {
                tracing::warn!("operations: could not move the card of {request_id} to {outcome}: {e}");
            }
        }
        self.signal(&format!("req:{request_id}"));
        self.approval_get(request_id)
    }

    /// Decide one request. An approval carries the passkey assertion over this request alone.
    pub async fn approve(&self, request_id: &str, assertion: &Value) -> AResult<Value> {
        self.sweep().await;
        let r = self.request_row(request_id)?;
        if r["status"] != "pending" {
            return Err(ApproverError::new("decided", format!("the request is already {}", r["status"].as_str().unwrap_or_default())));
        }
        let payload: Value = r["payload"].as_str().and_then(|s| serde_json::from_str(s).ok()).unwrap_or(json!({}));
        let kind = r["kind"].as_str().unwrap_or_default().to_string();
        let device = self.check_passkey(assertion, &approval_challenge(request_id, &kind, &payload))?;
        self.grant(request_id, &r, &payload, &device).await
    }

    /// The passkey options for one request's approval.
    pub fn approve_options(&self, request_id: &str) -> AResult<Value> {
        let r = self.request_row(request_id)?;
        let payload: Value = r["payload"].as_str().and_then(|s| serde_json::from_str(s).ok()).unwrap_or(json!({}));
        self.passkey_options(&approval_challenge(request_id, r["kind"].as_str().unwrap_or_default(), &payload))
    }

    async fn grant(&self, request_id: &str, r: &Map<String, Value>, payload: &Value, device: &str) -> AResult<Value> {
        let now = self.now() as i64;
        let minutes = r["minutes"].as_i64().unwrap_or(MINUTES_MIN);
        let kind = r["kind"].as_str().unwrap_or_default();
        let mut body = json!({
            "grant_id": format!("g{}", token(9)), "request_id": request_id, "kind": kind, "node": r["node"],
            "subject": r["requester_session"].as_str().unwrap_or("tony"),
            "payload_sha256": hex::encode(Sha256::digest(canonical(payload))),
            "not_before": now, "not_after": now + minutes * 60, "approved_by_device": device,
        });
        let mut cols: Vec<(&str, Value)> = vec![];
        if kind == "admin_ssh" {
            let mut nodes: Vec<String> = payload["nodes"].as_array().cloned().unwrap_or_default().iter().filter_map(|n| n.as_str().map(str::to_string)).collect();
            nodes.sort();
            body["nodes"] = json!(nodes);
            let principals: Vec<String> = nodes.iter().map(|n| format!("admin@{n}")).collect();
            let gid = body["grant_id"].as_str().unwrap_or_default().to_string();
            let cert = self.keys.ssh_cert(payload["pubkey"].as_str().unwrap_or_default(), &principals, now, now + minutes * 60, &gid)
                .map_err(|e| ApproverError::new("failed", e.to_string()))?;
            cols.push(("cert", json!(cert)));
        }
        if kind == "plugin_commits" {
            let mut commits: Vec<String> = payload["plugins"].as_array().cloned().unwrap_or_default().iter()
                .map(|p| format!("{}@{}", p["plugin"].as_str().unwrap_or_default(), p["commit"].as_str().unwrap_or_default())).collect();
            commits.sort();
            body["commits"] = json!(commits);
        }
        if kind == "device_enrol" {
            let link = self.enrol_link(15, Some(request_id))?;
            cols.push(("enrol_token", link["token"].clone()));
        }
        cols.push(("grant_env", json!(self.keys.envelope("grant", body).to_string())));
        self.finish(request_id, "approved", Some(device), &cols).await
    }

    /// Reject one pending request with the cookie alone.
    pub async fn reject(&self, request_id: &str, device: &str) -> AResult<Value> {
        self.sweep().await;
        let r = self.request_row(request_id)?;
        if r["status"] != "pending" {
            return Err(ApproverError::new("decided", format!("the request is already {}", r["status"].as_str().unwrap_or_default())));
        }
        self.finish(request_id, "denied", Some(device), &[]).await
    }

    /// The earliest expiry of a pending request.
    pub fn next_expiry(&self) -> Option<f64> {
        self.conn().query_row("SELECT MIN(expires) FROM requests WHERE status='pending'", [], |r| r.get::<_, Option<f64>>(0)).ok().flatten()
    }

    /// Wakes the sweep when a request opens.
    pub fn sweep_signal(&self) -> Arc<tokio::sync::Notify> {
        self.notify("sweep")
    }

    pub async fn sweep(&self) {
        let ids: Vec<String> = {
            let db = self.conn();
            let Ok(mut st) = db.prepare("SELECT request_id FROM requests WHERE status='pending' AND expires<=?") else { return };
            st.query_map([self.now()], |r| r.get(0)).map(|rows| rows.filter_map(Result::ok).collect()).unwrap_or_default()
        };
        for id in ids {
            let _ = self.finish(&id, "expired", None, &[]).await;
        }
    }

    /// A task's approvals, as `board_changes` carries them (CONTRACT.md item 4).
    pub fn approvals_of(&self, task_id: &str) -> Vec<Value> {
        let now = self.now();
        let db = self.conn();
        let Ok(mut st) = db.prepare("SELECT request_id, status, kind, reason, minutes, created, decided_at, expires FROM requests WHERE task_id=? ORDER BY created") else {
            return vec![];
        };
        st.query_map([task_id], |r| {
            let (status, expires): (String, f64) = (r.get(1)?, r.get(7)?);
            let state = match status.as_str() {
                "denied" => "rejected".to_string(),
                "pending" if expires <= now => "expired".to_string(),
                s => s.to_string(),
            };
            let kind: String = r.get(2)?;
            let reason: Option<String> = r.get(3)?;
            let minutes: Option<i64> = r.get(4)?;
            Ok(json!({"request_id": r.get::<_, String>(0)?, "state": state,
                      "summary": summary_of(&kind, reason.as_deref().unwrap_or(""), minutes.unwrap_or(0)),
                      "requested_at": r.get::<_, f64>(5)?, "decided_at": r.get::<_, Option<f64>>(6)?}))
        })
        .map(|rows| rows.filter_map(Result::ok).collect())
        .unwrap_or_default()
    }

    // -- orders (R97: alcor never acts on another node) ------------------------------------------

    fn order_node(&self, kind: &str, node: Option<&str>) -> AResult<String> {
        let allowed = self.config.allowed_orders.get(kind).cloned().unwrap_or_default();
        match node {
            Some(n) if valid_node(n) && (allowed.iter().any(|a| a == "*") || allowed.iter().any(|a| a == n)) => Ok(n.into()),
            _ => Err(ApproverError::new("not_allowed", format!("no {kind} orders to {node:?}"))),
        }
    }

    pub fn issue_order(&self, kind: &str, node: &str, payload: Value) -> AResult<Value> {
        let now = self.now();
        let body = json!({"order_id": format!("o{}", token(9)), "kind": kind, "node": node, "payload": payload, "not_after": now as i64 + ORDER_TTL_S});
        let env = self.keys.envelope("order", body.clone());
        self.conn().execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
            params![body["order_id"].as_str(), kind, node, body["payload"].to_string(), env.to_string(), now, body["not_after"].as_i64()],
        )?;
        self.signal(&format!("orders:{node}"));
        Ok(env)
    }

    /// Switch a service on `node` from Ops. Outside a window this device opened with its passkey,
    /// it opens no order and answers with the request to approve.
    pub async fn service_toggle(&self, device: &str, node: &str, service: &str, on: bool) -> AResult<Value> {
        let node = self.order_node("service", Some(node))?;
        if !valid_plugin(service) {
            return Err(bad("service is a service name"));
        }
        let open = self.conn().query_row(
            "SELECT 1 FROM requests WHERE kind='service_window' AND status='approved' AND requester_session IS NULL AND device=? AND decided_at>?",
            params![device, self.now() - SERVICE_WINDOW_S],
            |_| Ok(()),
        ).optional()?.is_some();
        if !open {
            let pending: Option<String> = self.conn().query_row(
                "SELECT request_id FROM requests WHERE kind='service_window' AND status='pending' AND requester_session IS NULL AND expires>?",
                params![self.now()],
                |r| r.get(0),
            ).optional()?;
            if let Some(request_id) = pending {
                return Ok(json!({"needs_approval": true, "request_id": request_id}));
            }
            let r = self.approval_request("service_window", None, "Allow Ops to switch services for 5 minutes", MINUTES_MIN, json!({}), None, None).await?;
            return Ok(json!({"needs_approval": true, "request_id": r["request_id"]}));
        }
        let env = self.issue_order("service", &node, json!({"service": service, "on": on}))?;
        Ok(json!({"order_id": env["order"]["order_id"]}))
    }

    /// Start an agent on `node` from the vault: a card in Needs you and a signed `agent`
    /// order. No text from the vault reaches the agent, so a model or effort must be a listed one.
    pub async fn agent(&self, node: &str, model: Option<&str>, effort: Option<&str>, note_id: Option<&str>) -> AResult<Value> {
        let node = self.order_node("agent", Some(node))?;
        if model.is_some_and(|m| !AGENT_MODELS.contains(&m)) {
            return Err(bad(format!("model must be one of {}", AGENT_MODELS.join(", "))));
        }
        effort.map(awm_trust::trust::check_effort).transpose().map_err(|e| bad(e.to_string()))?;
        if let Some(n) = note_id {
            if n.is_empty() || n.len() > 64 || !n.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') {
                return Err(bad("note_id is a vault note id"));
            }
        }
        let (store, nd, nt) = (self.store.clone(), node.clone(), note_id.map(str::to_string));
        let card = tokio::task::spawn_blocking(move || store.agent_card(&nd, nt.as_deref())).await.map_err(|e| ApproverError::new("failed", e.to_string()))??;
        let task_id = card["task_id"].as_str().unwrap_or_default().to_string();
        let mut payload = json!({"task_id": task_id, "node": node});
        if let Some(n) = note_id {
            payload["note_id"] = json!(n);
        }
        if let Some(m) = model {
            payload["model"] = json!(m);
        }
        if let Some(e) = effort {
            payload["effort"] = json!(e);
        }
        let env = self.issue_order("agent", &node, payload)?;
        Ok(json!({"task_id": task_id, "order_id": env["order"]["order_id"]}))
    }

    pub async fn order_pull(&self, node: &str, kinds: Option<&[String]>, wait_s: f64) -> AResult<Value> {
        let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(wait_s.clamp(0.0, 55.0));
        loop {
            let n = self.notify(&format!("orders:{node}"));
            let wait = n.notified();
            let rows: Vec<(String, String, String)> = {
                let db = self.conn();
                let mut st = db.prepare("SELECT order_id, kind, envelope FROM orders WHERE node=? AND done_at IS NULL AND not_after>? ORDER BY created")?;
                let rows = st.query_map(params![node, self.now()], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
                rows
            };
            let rows: Vec<_> = rows.into_iter().filter(|(_, k, _)| kinds.is_none_or(|ks| ks.is_empty() || ks.iter().any(|x| x == k))).collect();
            if !rows.is_empty() || tokio::time::Instant::now() >= deadline {
                let now = self.now();
                for (id, _, _) in &rows {
                    self.conn().execute("UPDATE orders SET pulled_at=COALESCE(pulled_at, ?) WHERE order_id=?", params![now, id])?;
                }
                return Ok(Value::Array(rows.into_iter().filter_map(|(_, _, e)| serde_json::from_str(&e).ok()).collect()));
            }
            let _ = tokio::time::timeout_at(deadline, wait).await;
        }
    }

    pub fn order_done(&self, order_id: &str, ok: bool, note: &str) -> AResult<Value> {
        let note: String = note.chars().take(1000).collect();
        let n = self.conn().execute(
            "UPDATE orders SET done_at=?, ok=?, note=? WHERE order_id=? AND done_at IS NULL",
            params![self.now(), ok as i64, note, order_id],
        )?;
        Ok(json!({"order_id": order_id, "recorded": n == 1}))
    }

    pub fn order_get(&self, order_id: &str) -> AResult<Option<Value>> {
        let db = self.conn();
        let row = db.query_row(
            "SELECT order_id, kind, node, created, not_after, pulled_at, done_at, ok, note FROM orders WHERE order_id=?",
            [order_id],
            |r| {
                let mut m = Map::new();
                for (i, k) in ["order_id", "kind", "node", "created", "not_after", "pulled_at", "done_at", "ok", "note"].iter().enumerate() {
                    m.insert((*k).into(), sql_json(r.get(i)?));
                }
                Ok(Value::Object(m))
            },
        ).optional()?;
        Ok(row)
    }

    // -- devices ---------------------------------------------------------------------------------

    pub fn devices(&self) -> AResult<Value> {
        let db = self.conn();
        let mut st = db.prepare("SELECT device_id, name, created, revoked_at, locked_nonce, seq, enrolled_by FROM devices ORDER BY created")?;
        let rows = st.query_map([], |r| {
            Ok(json!({"device_id": r.get::<_, String>(0)?, "name": r.get::<_, Option<String>>(1)?, "created": r.get::<_, Option<f64>>(2)?,
                      "revoked_at": r.get::<_, Option<f64>>(3)?, "locked": r.get::<_, Option<String>>(4)?.is_some_and(|s| !s.is_empty()),
                      "seq": r.get::<_, i64>(5)?, "enrolled_by": r.get::<_, Option<String>>(6)?}))
        })?.collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(Value::Array(rows))
    }

    /// Revoke a device: its passkeys and every sign-in it holds end.
    pub fn device_revoke(&self, device_id: &str) -> AResult<Value> {
        let now = self.now();
        let db = self.conn();
        let n = db.execute("UPDATE devices SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL", params![now, device_id])?;
        db.execute("UPDATE credentials SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL", params![now, device_id])?;
        let ended = db.execute("DELETE FROM device_sessions WHERE device=?", [device_id])?;
        db.execute("UPDATE windows SET closed_at=?, closed_reason='revoked' WHERE closed_at IS NULL AND device_id=?", params![now, device_id])?;
        Ok(json!({"device_id": device_id, "revoked": n == 1, "sessions_ended": ended}))
    }

    // -- the 2fa burst (R137) ---------------------------------------------------------------------

    pub async fn twofa_state(&self) -> AResult<Value> {
        let devices = self.tools.twofa("devices", json!({})).await.map_err(|e| ApproverError::new("failed", e))?;
        let pending = self.tools.twofa("pending", json!({})).await.map_err(|e| ApproverError::new("failed", e))?;
        Ok(json!({"devices": devices, "pending": pending}))
    }

    /// The burst's passkey options, bound to a fresh nonce.
    pub fn burst_options(&self, count: i64, device: &str) -> AResult<Value> {
        if !BURST_COUNTS.contains(&count) {
            return Err(bad(format!("a burst approves {BURST_COUNTS:?} pushes")));
        }
        let n = self.nonce();
        Ok(json!({"nonce": n, "options": self.passkey_options(&burst_challenge(&n, count, device))?}))
    }

    /// Arm one burst of `count` 2fa approvals, with one tap.
    pub async fn burst(&self, nonce: &str, count: i64, device: &str, assertion: &Value) -> AResult<Value> {
        if !BURST_COUNTS.contains(&count) {
            return Err(bad(format!("a burst approves {BURST_COUNTS:?} pushes")));
        }
        self.spend(nonce)?;
        self.check_passkey(assertion, &burst_challenge(nonce, count, device))?;
        self.tools.twofa("burst", json!({"device": device, "count": count})).await.map_err(|e| ApproverError::new("failed", e))
    }

    /// Prove every stored passkey parses, for `check-state`.
    pub fn check_credentials(&self) -> Vec<String> {
        let db = self.conn();
        let Ok(mut st) = db.prepare("SELECT cred_id, public_key FROM credentials WHERE revoked_at IS NULL") else { return vec!["credentials: unreadable".into()] };
        let rows: Vec<(String, String)> = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).map(|r| r.filter_map(Result::ok).collect()).unwrap_or_default();
        rows.into_iter().filter_map(|(id, k)| passkeys::parse_cose(&k).err().map(|e| format!("credential {}…: {e}", id.chars().take(8).collect::<String>()))).collect()
    }
}

fn summary_of(kind: &str, reason: &str, minutes: i64) -> String {
    let mut s = format!("{kind}: {reason}");
    if WINDOW_KINDS.contains(&kind) && minutes > 0 {
        s.push_str(&format!(" ({minutes} min)"));
    }
    s
}

fn rand_bytes(n: usize) -> Vec<u8> {
    use rand::RngCore;
    let mut b = vec![0u8; n];
    rand::thread_rng().fill_bytes(&mut b);
    b
}

fn sql_json(v: rusqlite::types::Value) -> Value {
    use rusqlite::types::Value as V;
    match v {
        V::Null => Value::Null,
        V::Integer(i) => json!(i),
        V::Real(f) => json!(f),
        V::Text(s) => json!(s),
        V::Blob(b) => json!(b64u(&b)),
    }
}
