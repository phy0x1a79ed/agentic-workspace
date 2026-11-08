//! The two bearer formats the edge reads, byte for byte as the Python wrote them: the
//! `awmut1.<id>.<secret>` device token and the HMAC-signed sliding session cookie.
//!
//! A device token's `id` is 16 lowercase hex digits and its secret 32 random bytes in
//! unpadded base64url. auth keeps only the secret's SHA-256, and the edge compares it in
//! constant time. A token may carry a scope, which narrows it to a few exact request shapes;
//! [`scope_admits`] is the one place those shapes live.
//!
//! A session cookie is `<b64url(payload)>.<b64url(hmac-sha256(secret, payload_b64))>`, where the
//! payload is `{"exp":..,"sat":..,"sub":..}` in `json.dumps(sort_keys=True)` compact form.
//! `sat` is the session start and never moves; `exp` slides forward on each refresh.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use hmac::{Hmac, Mac};
use rand::RngCore;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

pub const PREFIX: &str = "awmut1.";

/// The avatar stage session: one WebSocket, whose first frame is the init.
pub const AVATAR_STAGE: &str = "avatar-stage";
const AVATAR_SESSION: &str = "/hub/session/avatar/stage";

/// Every scope a token may be minted with, sorted, as the edge names them to auth.
pub const SCOPES: [&str; 1] = [AVATAR_STAGE];

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum TokenError {
    #[error("not a device token")]
    NotAToken,
    #[error("malformed device token")]
    Malformed,
}

/// A freshly minted device token. Only `token` is shown to the holder.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceToken {
    pub id: String,
    pub secret: String,
    pub token: String,
}

/// Whether `text` carries the device-token prefix, well-formed or not.
pub fn looks_like_token(text: &str) -> bool {
    text.starts_with(PREFIX)
}

/// A fresh device token from the OS RNG.
pub fn mint() -> DeviceToken {
    let mut id = [0u8; 8];
    let mut secret = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut id);
    rand::rngs::OsRng.fill_bytes(&mut secret);
    mint_with(id, secret)
}

/// The device token for these random bytes.
pub fn mint_with(id: [u8; 8], secret: [u8; 32]) -> DeviceToken {
    let id = hex::encode(id);
    let secret = URL_SAFE_NO_PAD.encode(secret);
    let token = format!("{PREFIX}{id}.{secret}");
    DeviceToken { id, secret, token }
}

/// Python's `re.match(r"^...$")`: `$` also matches before one trailing newline.
fn before_dollar(s: &str) -> &str {
    s.strip_suffix('\n').unwrap_or(s)
}

/// `(id, secret)` of a device token.
pub fn parse(token: &str) -> Result<(&str, &str), TokenError> {
    let rest = token.strip_prefix(PREFIX).ok_or(TokenError::NotAToken)?;
    let (id, secret) = rest.split_once('.').ok_or(TokenError::Malformed)?;
    let id_ok = {
        let core = before_dollar(id);
        core.len() == 16 && core.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    };
    let secret_ok = {
        let core = before_dollar(secret);
        core.len() == 43 && core.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    };
    if !id_ok || !secret_ok {
        return Err(TokenError::Malformed);
    }
    Ok((id, secret))
}

/// The stored form of a secret: its SHA-256, in lowercase hex.
pub fn digest(secret: &str) -> String {
    hex::encode(Sha256::digest(secret.as_bytes()))
}

/// Whether `secret` hashes to `stored_digest`, compared in constant time.
pub fn matches(secret: &str, stored_digest: &str) -> bool {
    digest(secret).as_bytes().ct_eq(stored_digest.as_bytes()).into()
}

/// The parts of a request a scoped device token is checked against. `path` is the decoded
/// path and `raw` the path as it arrived.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Shape<'a> {
    pub method: &'a str,
    pub path: &'a str,
    pub raw: &'a [u8],
    pub query: &'a [u8],
    pub websocket: bool,
}

/// Whether a token minted with `scope` may make this request. Both `path` and `raw` must name
/// the shape, so no percent-encoding and no query string pass. An unknown scope admits nothing.
pub fn scope_admits(scope: &str, shape: &Shape<'_>) -> bool {
    // `path.encode("ascii", "replace")`: each non-ASCII character becomes one `?`.
    let ascii: Vec<u8> = shape.path.chars().map(|c| if c.is_ascii() { c as u8 } else { b'?' }).collect();
    if !shape.query.is_empty() || shape.raw != ascii.as_slice() {
        return false;
    }
    scope == AVATAR_STAGE && shape.websocket && shape.method == "GET" && shape.path == AVATAR_SESSION
}

// -- the session cookie -----------------------------------------------------------------------

fn sign(secret: &str, payload_b64: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("hmac takes any key length");
    mac.update(payload_b64.as_bytes());
    URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
}

/// Python's `int(x)` of a float: truncation toward zero.
fn py_int(x: f64) -> i64 {
    x.trunc() as i64
}

/// Sign a session token valid for `ttl` seconds from `now`. `sat` is the session start: `None`
/// on a first sign-in, the original start on a refresh.
pub fn session_mint(secret: &str, sub: &str, ttl: f64, sat: Option<i64>, now: f64) -> String {
    let sat = sat.unwrap_or_else(|| py_int(now));
    let sub = awm_pycompat::json::dumps(&Value::String(sub.to_string()));
    let payload = format!("{{\"exp\":{},\"sat\":{sat},\"sub\":{sub}}}", py_int(now + ttl));
    let payload_b64 = URL_SAFE_NO_PAD.encode(payload.as_bytes());
    let sig = sign(secret, &payload_b64);
    format!("{payload_b64}.{sig}")
}

/// The payload segment's bytes. Only a segment this secret signed gets here, and every such
/// segment is canonical unpadded base64url.
fn b64u_decode(s: &str) -> Option<Vec<u8>> {
    URL_SAFE_NO_PAD.decode(s.trim_end_matches('=')).ok()
}

/// The claims of a session token whose MAC holds and whose `exp` is still ahead of `now`.
/// A malformed token is simply unauthenticated.
pub fn session_verify(secret: &str, token: &str, now: f64) -> Option<Map<String, Value>> {
    let (payload_b64, sig_b64) = token.split_once('.')?;
    // Python raises on a non-ASCII payload or signature; nothing non-ASCII was ever signed.
    if !payload_b64.is_ascii() || !sig_b64.is_ascii() {
        return None;
    }
    let expected = sign(secret, payload_b64);
    if !bool::from(expected.as_bytes().ct_eq(sig_b64.as_bytes())) {
        return None;
    }
    let raw = b64u_decode(payload_b64)?;
    let Ok(Value::Object(claims)) = serde_json::from_slice::<Value>(&raw) else {
        return None;
    };
    let exp = match claims.get("exp") {
        Some(Value::Number(n)) => n.as_f64()?,
        Some(Value::Bool(b)) => f64::from(u8::from(*b)),
        _ => return None,
    };
    if now >= exp {
        return None;
    }
    Some(claims)
}

/// Python's truthiness of a JSON value.
pub(crate) fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's `str(v)` of a JSON value.
pub(crate) fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => awm_pycompat::repr::repr(other),
    }
}

/// The identity a session's claims act as: `str(claims.get("sub") or "operator")`.
pub fn session_sub(claims: &Map<String, Value>) -> String {
    match claims.get("sub") {
        Some(v) if truthy(v) => py_str(v),
        _ => "operator".into(),
    }
}

/// `int(claims.get("sat", 0))`. `None` where Python's `int` raises.
pub fn session_sat(claims: &Map<String, Value>) -> Option<i64> {
    match claims.get("sat") {
        None => Some(0),
        Some(Value::Bool(b)) => Some(i64::from(*b)),
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(py_int)),
        Some(Value::String(s)) => awm_pycompat::pystr::strip(s).parse::<i64>().ok(),
        Some(_) => None,
    }
}
