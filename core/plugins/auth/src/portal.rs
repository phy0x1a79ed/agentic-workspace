//! Sign-in through alcor's approver with the passkey (fleet.md R84, users.md R130).
//!
//! A browser with no session goes to the approver's `/signin` with the visit as `next`. After
//! the passkey the approver sends it back to [`CALLBACK`] with a `portal_login` grant that alcor
//! signed for this node, this host and this path. The edge checks the grant offline against
//! alcor's grant key, spends it once, and starts its own session. It never calls alcor.
//!
//! A grant value is a credential until it expires: nothing here logs one, and no error carries one.

use std::collections::HashMap;
use std::sync::Mutex;

use awm_trust::grant::{load_public_key, unb64u, verify_grant, Expect};
use base64::Engine;
use serde_json::{json, Value};

pub const CALLBACK: &str = "/__auth/portal";
pub const GRANT_KEY_ENV: &str = "AWM_APPROVER_GRANT_PUBKEY";
pub const ORIGIN_ENV: &str = "AWM_APPROVER_ORIGIN";
pub const DEFAULT_ORIGIN: &str = "https://auth.tony-xy-liu.com";

/// A refused grant, with Python's `GrantInvalid` text.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct GrantInvalid(pub String);

/// The approver's origin: `AWM_APPROVER_ORIGIN`, else alcor's, with no trailing slash.
pub fn approver_origin() -> String {
    origin_of(std::env::var(ORIGIN_ENV).ok().as_deref())
}

fn origin_of(value: Option<&str>) -> String {
    value.filter(|v| !v.is_empty()).unwrap_or(DEFAULT_ORIGIN).trim_end_matches('/').to_string()
}

/// The approver's sign-in page, sending the browser back to `public_url` after.
pub fn login_url(public_url: &str) -> String {
    login_url_at(&approver_origin(), public_url)
}

/// [`login_url`] for an explicit approver origin.
pub fn login_url_at(origin: &str, public_url: &str) -> String {
    format!("{}/signin?next={}", origin_of(Some(origin)), crate::pages::quote(public_url))
}

/// `json.dumps(v, separators=(",", ":"))`: compact, every character past `~` escaped.
fn compact_ascii_json(v: &Value) -> String {
    let mut out = String::new();
    for c in serde_json::to_string(v).expect("a JSON value").chars() {
        if (c as u32) < 0x7f {
            out.push(c);
        } else {
            let mut units = [0u16; 2];
            for u in c.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{u:04x}"));
            }
        }
    }
    out
}

/// The URL form of a grant envelope: base64url of its compact JSON, unpadded.
pub fn encode_grant(envelope: &Value) -> String {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(compact_ascii_json(envelope))
}

/// The envelope in a URL-form grant.
///
/// Stricter than Python's `urlsafe_b64decode`, which drops characters outside the alphabet: a
/// grant carrying any is refused here. Either alphabet's `+`/`-` and `/`/`_` are read alike.
pub fn decode_grant(raw: &str) -> Result<Value, GrantInvalid> {
    let not_b64 = || GrantInvalid("the grant is not base64url JSON".into());
    let text = raw.replace('+', "-").replace('/', "_");
    let bytes = unb64u(&text).ok_or_else(not_b64)?;
    let out: Value = serde_json::from_slice(&bytes).map_err(|_| not_b64())?;
    if !out.is_object() {
        return Err(GrantInvalid("the grant is not an envelope".into()));
    }
    Ok(out)
}

/// `raw` when it is a path on this origin with no query or fragment, else `None`.
pub fn local_path(raw: Option<&str>) -> Option<&str> {
    let raw = raw.filter(|r| !r.is_empty())?;
    if !raw.starts_with('/') || raw.starts_with("//") {
        return None;
    }
    if raw.contains(['\\', '\n', '\r', '\t', '\0', '?', '#']) {
        return None;
    }
    Some(raw)
}

/// The host a `Host` header names, lowercased, without port or brackets: Python's
/// `urlsplit("//" + header).hostname or ""`.
///
/// A header `urlsplit` raises on (a bad bracket, or a host that NFKC turns into a delimiter)
/// gives `""`, which no grant names.
pub fn host_of(host_header: Option<&str>) -> String {
    let url: String = format!("//{}", host_header.unwrap_or("")).chars().filter(|c| !matches!(c, '\t' | '\r' | '\n')).collect();
    let rest = &url[2..];
    let netloc = &rest[..rest.find(['/', '?', '#']).unwrap_or(rest.len())];
    if netloc.contains('[') != netloc.contains(']') {
        return String::new();
    }
    if netloc.contains('[') && !bracketed_netloc_ok(netloc) {
        return String::new();
    }
    if !nfkc_netloc_ok(netloc) {
        return String::new();
    }
    let hostinfo = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let hostname = match hostinfo.split_once('[') {
        Some((_, bracketed)) => bracketed.split_once(']').map_or(bracketed, |(h, _)| h),
        None => hostinfo.split_once(':').map_or(hostinfo, |(h, _)| h),
    };
    if hostname.is_empty() {
        return String::new();
    }
    match hostname.split_once('%') {
        Some((h, zone)) => format!("{}%{zone}", awm_pycompat::pystr::lower(h)),
        None => awm_pycompat::pystr::lower(hostname),
    }
}

/// `urllib.parse._check_bracketed_netloc`: nothing before the bracket, only a port after it,
/// and a valid host inside.
fn bracketed_netloc_ok(netloc: &str) -> bool {
    let host_and_port = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let hostname = match host_and_port.split_once('[') {
        Some((before, bracketed)) => {
            if !before.is_empty() {
                return false;
            }
            let (hostname, port) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            if !port.is_empty() && !port.starts_with(':') {
                return false;
            }
            hostname
        }
        None => host_and_port.split_once(':').map_or(host_and_port, |(h, _)| h),
    };
    bracketed_host_ok(hostname)
}

/// `urllib.parse._check_bracketed_host`: an IPvFuture literal, or an IPv6 address.
fn bracketed_host_ok(host: &str) -> bool {
    if let Some(rest) = host.strip_prefix('v') {
        let Some((hex, tail)) = rest.split_once('.') else { return false };
        return !hex.is_empty() && hex.chars().all(|c| c.is_ascii_hexdigit()) && !tail.is_empty();
    }
    let addr = host.split_once('%').map_or(host, |(a, scope)| if scope.is_empty() || scope.contains('%') { "" } else { a });
    addr.parse::<std::net::Ipv6Addr>().is_ok()
}

/// `urllib.parse._checknetloc`: a non-ASCII netloc must not gain a delimiter under NFKC.
fn nfkc_netloc_ok(netloc: &str) -> bool {
    use unicode_normalization::UnicodeNormalization;
    if netloc.is_ascii() {
        return true;
    }
    let n: String = netloc.chars().filter(|c| !matches!(c, '@' | ':' | '#' | '?')).collect();
    let normal: String = n.nfkc().collect();
    normal == n || !normal.contains(['/', '?', '#', '@', ':'])
}

/// A clock in seconds since the epoch.
pub type Clock = Box<dyn Fn() -> f64 + Send + Sync>;

/// Checks portal grants and remembers each spent one until it expires.
pub struct Grants {
    key: Option<String>,
    node: String,
    clock: Clock,
    spent: Mutex<HashMap<String, i64>>,
}

impl Grants {
    /// `key` is alcor's grant key in base64url (the `AWM_APPROVER_GRANT_PUBKEY` secret), and
    /// `node` this node's name as the edge list names it.
    pub fn new(key: Option<String>, node: impl Into<String>) -> Grants {
        Grants::with_clock(key, node, Box::new(crate::now))
    }

    pub fn with_clock(key: Option<String>, node: impl Into<String>, clock: Clock) -> Grants {
        Grants { key, node: node.into(), clock, spent: Mutex::new(HashMap::new()) }
    }

    /// The grant's subject, once it is alcor's, for this node, `host` and `path`, and unspent.
    pub fn accept(&self, raw: &str, host: &str, path: &str) -> Result<String, GrantInvalid> {
        let key = self.key.as_deref().map(str::trim).unwrap_or("");
        if key.is_empty() {
            return Err(GrantInvalid(format!("no {GRANT_KEY_ENV} on this node")));
        }
        let key = load_public_key(key).map_err(|e| GrantInvalid(e.to_string()))?;
        let now = (self.clock)();
        let whole = now.floor() as i64;
        let payload = json!({"site": host, "return": path});
        let expect = Expect { kind: Some("portal_login"), node: Some(&self.node), subject: None, payload: Some(&payload) };
        let body = verify_grant(&decode_grant(raw)?, &key, &expect, whole).map_err(|e| GrantInvalid(e.to_string()))?;
        let grant_id = body.get("grant_id").and_then(Value::as_str).ok_or_else(|| GrantInvalid("the grant has no grant_id".into()))?;
        let not_after = body.get("not_after").and_then(Value::as_i64).unwrap_or(0);
        let mut spent = self.spent.lock().unwrap_or_else(|e| e.into_inner());
        spent.retain(|_, t| (*t as f64) > now);
        if spent.contains_key(grant_id) {
            return Err(GrantInvalid("the grant is spent".into()));
        }
        spent.insert(grant_id.to_string(), not_after);
        Ok(body.get("subject").and_then(Value::as_str).unwrap_or("").to_string())
    }
}
