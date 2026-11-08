//! Check a grant or an order that alcor's approver signed (fleet.md R97 item 3).
//!
//! Twin of `awm/service_components/grantlib/verify_grant.py`: the same checks in the same
//! order, with the same error text. The signature is Ed25519 over the body's canonical
//! JSON: keys sorted at every level, separators `,` and `:`, UTF-8, integer times.
//! Envelope: `{"grant"|"order": {...}, "sig": b64url, "key_id": hex16}`.

use std::path::Path;

use base64::Engine;
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum GrantError {
    #[error("no {0} in the envelope")]
    Missing(&'static str),
    #[error("the envelope names another key")]
    WrongKey,
    #[error("{0} is a float, and signed times are integers")]
    FloatTime(String),
    #[error("the signature does not verify")]
    BadSignature,
    #[error("not valid yet")]
    NotYetValid,
    #[error("expired")]
    Expired,
    /// The grant's kind or subject is not the one asked for. `None` when the grant has none.
    #[error("the grant is for {}, not {}", py_repr(.got.as_deref()), py_repr(Some(.want)))]
    Mismatch { field: &'static str, got: Option<String>, want: String },
    #[error("the grant does not cover node {}", py_repr(Some(.0)))]
    WrongNode(String),
    #[error("the grant does not cover this request")]
    WrongPayload,
    #[error("the order is for {}, not {}", py_repr(.got.as_deref()), py_repr(Some(.want)))]
    WrongOrderNode { got: Option<String>, want: String },
    #[error("the public key is not 32 bytes of base64url")]
    BadKey,
}

/// A grant body, typed. `verify_grant` returns the raw body; read it with [`Grant::of`].
#[derive(Debug, Clone, Deserialize)]
pub struct Grant {
    #[serde(default)]
    pub grant_id: String,
    #[serde(default)]
    pub request_id: String,
    #[serde(default)]
    pub kind: String,
    /// One node, or `None` when `nodes` lists them (an admin_ssh grant for a run).
    #[serde(default)]
    pub node: Option<String>,
    #[serde(default)]
    pub nodes: Vec<String>,
    #[serde(default)]
    pub subject: String,
    #[serde(default)]
    pub payload_sha256: String,
    #[serde(default)]
    pub not_before: i64,
    #[serde(default)]
    pub not_after: i64,
    #[serde(default)]
    pub approved_by_device: String,
}

impl Grant {
    pub fn of(body: &Value) -> Option<Grant> {
        serde_json::from_value(body.clone()).ok()
    }
}

/// What a grant must name to pass.
#[derive(Debug, Default)]
pub struct Expect<'a> {
    pub kind: Option<&'a str>,
    pub node: Option<&'a str>,
    pub subject: Option<&'a str>,
    pub payload: Option<&'a Value>,
}

/// Python's `repr` of a str, or `None`.
fn py_repr(s: Option<&str>) -> String {
    let Some(s) = s else { return "None".into() };
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || (0x7f..0xa0).contains(&(c as u32)) => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

pub fn canonical(v: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    write_canonical(v, &mut out);
    out
}

fn write_canonical(v: &Value, out: &mut Vec<u8>) {
    match v {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push(b'{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                out.extend(serde_json::to_vec(k).expect("string"));
                out.push(b':');
                write_canonical(&map[*k], out);
            }
            out.push(b'}');
        }
        Value::Array(items) => {
            out.push(b'[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                write_canonical(item, out);
            }
            out.push(b']');
        }
        other => out.extend(serde_json::to_vec(other).expect("scalar")),
    }
}

pub fn b64u(data: &[u8]) -> String {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(data)
}

/// Decode base64url with or without padding. Stricter than Python: a character outside
/// the alphabet is an error, never dropped.
pub fn unb64u(text: &str) -> Option<Vec<u8>> {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(text.trim_end_matches('=')).ok()
}

pub fn payload_digest(payload: &Value) -> String {
    hex::encode(Sha256::digest(canonical(payload)))
}

/// The raw Ed25519 key in `b64u`, the approver's `grant_pub` form.
pub fn load_public_key(b64u: &str) -> Result<[u8; 32], GrantError> {
    unb64u(b64u.trim()).and_then(|b| b.try_into().ok()).ok_or(GrantError::BadKey)
}

pub fn key_id(public_key: &[u8; 32]) -> String {
    hex::encode(Sha256::digest(public_key))[..16].to_string()
}

/// The approver's envelope for `body` under `field`, byte for byte `Keys.envelope`.
pub fn sign_envelope(key: &SigningKey, field: &str, body: Value) -> Value {
    let sig = b64u(&key.sign(&canonical(&body)).to_bytes());
    json!({field: body, "sig": sig, "key_id": key_id(&key.verifying_key().to_bytes())})
}

/// The secret or config variable that holds the grant key.
pub const GRANT_KEY_ENV: &str = "AWM_APPROVER_GRANT_PUBKEY";
/// The file in `<AWM_DIR>` where the fleet's config writes the grant key.
pub const GRANT_KEY_FILE: &str = "grant-key.pub";

/// The grant key from the first non-empty value among `values` (secrets first, then config).
pub fn grant_key_env<I: IntoIterator<Item = String>>(values: I) -> Result<[u8; 32], String> {
    let value = values.into_iter().find(|v| !v.trim().is_empty()).ok_or_else(|| {
        format!("{GRANT_KEY_ENV} is not set on this node, so no approval can be checked; enrol the passkey and set the approver's grant_pub as {GRANT_KEY_ENV} in the node's secrets")
    })?;
    load_public_key(&value).map_err(|e| format!("{GRANT_KEY_ENV}: {e}"))
}

/// The grant key in `<awm_dir>/grant-key.pub`. Standard base64 is read as base64url.
pub fn grant_key_file(awm_dir: &Path) -> Result<[u8; 32], String> {
    let path = awm_dir.join(GRANT_KEY_FILE);
    let text = std::fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    load_public_key(&text.trim().replace('+', "-").replace('/', "_"))
        .map_err(|_| format!("{}: expected a 32-byte Ed25519 key", path.display()))
}

fn verify_body<'a>(envelope: &'a Value, field: &'static str, public_key: &[u8; 32], now: i64) -> Result<&'a Map<String, Value>, GrantError> {
    let body = envelope.get(field).and_then(Value::as_object).ok_or(GrantError::Missing(field))?;
    if envelope.get("key_id").and_then(Value::as_str) != Some(key_id(public_key).as_str()) {
        return Err(GrantError::WrongKey);
    }
    if let Some((k, _)) = body.iter().find(|(_, v)| v.is_f64()) {
        return Err(GrantError::FloatTime(k.clone()));
    }
    let key = VerifyingKey::from_bytes(public_key).map_err(|_| GrantError::BadKey)?;
    let sig = envelope
        .get("sig")
        .and_then(Value::as_str)
        .and_then(unb64u)
        .and_then(|b| Signature::from_slice(&b).ok())
        .ok_or(GrantError::BadSignature)?;
    key.verify(&canonical(&envelope[field]), &sig).map_err(|_| GrantError::BadSignature)?;
    let not_before = body.get("not_before").map_or(Some(0), Value::as_i64);
    if not_before.is_none_or(|t| now < t) {
        return Err(GrantError::NotYetValid);
    }
    if body.get("not_after").and_then(Value::as_i64).is_none_or(|t| now >= t) {
        return Err(GrantError::Expired);
    }
    Ok(body)
}

fn str_of<'a>(body: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    body.get(key).and_then(Value::as_str)
}

/// The grant body inside `envelope`, once its signature, window and `expect` check out at `now`.
pub fn verify_grant(envelope: &Value, public_key: &[u8; 32], expect: &Expect, now: i64) -> Result<Value, GrantError> {
    let body = verify_body(envelope, "grant", public_key, now)?;
    let mismatch = |field: &'static str, want: &str| GrantError::Mismatch { field, got: str_of(body, field).map(str::to_string), want: want.into() };
    if let Some(k) = expect.kind.filter(|k| str_of(body, "kind") != Some(k)) {
        return Err(mismatch("kind", k));
    }
    if let Some(n) = expect.node {
        let listed = body.get("nodes").and_then(Value::as_array).is_some_and(|a| a.iter().any(|v| v.as_str() == Some(n)));
        if str_of(body, "node") != Some(n) && !listed {
            return Err(GrantError::WrongNode(n.into()));
        }
    }
    if let Some(s) = expect.subject.filter(|s| str_of(body, "subject") != Some(s)) {
        return Err(mismatch("subject", s));
    }
    if expect.payload.is_some_and(|p| str_of(body, "payload_sha256") != Some(payload_digest(p).as_str())) {
        return Err(GrantError::WrongPayload);
    }
    Ok(Value::Object(body.clone()))
}

/// An order body for `node`. The caller keeps `order_id`, so a replay does nothing.
pub fn verify_order(envelope: &Value, public_key: &[u8; 32], node: &str, now: i64) -> Result<Value, GrantError> {
    let body = verify_body(envelope, "order", public_key, now)?;
    if str_of(body, "node") != Some(node) {
        return Err(GrantError::WrongOrderNode { got: str_of(body, "node").map(str::to_string), want: node.into() });
    }
    Ok(Value::Object(body.clone()))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use serde_json::json;

    pub(crate) fn sign(sk: &SigningKey, grant: Value) -> Value {
        sign_envelope(sk, "grant", grant)
    }

    pub(crate) fn key() -> SigningKey {
        SigningKey::generate(&mut rand::rngs::OsRng)
    }

    #[test]
    fn canonical_sorts_every_level() {
        let v = json!({"b": 1, "a": {"d": [true, null], "c": "é"}});
        assert_eq!(canonical(&v), r#"{"a":{"c":"é","d":[true,null]},"b":1}"#.as_bytes());
    }

    #[test]
    fn repr_matches_python() {
        assert_eq!(py_repr(Some("admin_ssh")), "'admin_ssh'");
        assert_eq!(py_repr(Some("it's")), "\"it's\"");
        assert_eq!(py_repr(Some("a\tb\u{1}é")), "'a\\tb\\x01é'");
        assert_eq!(py_repr(None), "None");
    }

    #[test]
    fn verifies_and_refuses() {
        let sk = key();
        let pk = sk.verifying_key().to_bytes();
        let payload = json!({"session_id": "s1", "from": "cooperative", "to": "trusted"});
        let grant = json!({"grant_id": "g1", "request_id": "r1", "kind": "trust_raise", "node": "home",
            "subject": "s1", "payload_sha256": payload_digest(&payload), "not_before": 100, "not_after": 200,
            "approved_by_device": "d"});
        let env = sign(&sk, grant);
        let ok = Expect { kind: Some("trust_raise"), node: Some("home"), subject: Some("s1"), payload: Some(&payload) };
        assert_eq!(Grant::of(&verify_grant(&env, &pk, &ok, 150).unwrap()).unwrap().grant_id, "g1");
        assert_eq!(verify_grant(&env, &pk, &ok, 200).unwrap_err(), GrantError::Expired);
        assert_eq!(verify_grant(&env, &pk, &ok, 99).unwrap_err(), GrantError::NotYetValid);
        let other = Expect { kind: Some("invite"), ..Default::default() };
        assert_eq!(verify_grant(&env, &pk, &other, 150).unwrap_err().to_string(), "the grant is for 'trust_raise', not 'invite'");
        let mut forged = env.clone();
        forged["grant"]["subject"] = json!("s2");
        assert_eq!(verify_grant(&forged, &pk, &ok, 150).unwrap_err(), GrantError::BadSignature);
        let wrong = Expect { payload: Some(&json!({"to": "trusted"})), ..ok };
        assert_eq!(verify_grant(&env, &pk, &wrong, 150).unwrap_err(), GrantError::WrongPayload);
        let pk2 = key().verifying_key().to_bytes();
        assert_eq!(verify_grant(&env, &pk2, &ok, 150).unwrap_err(), GrantError::WrongKey);
    }

    #[test]
    fn a_float_is_named_before_the_signature() {
        let sk = key();
        let pk = sk.verifying_key().to_bytes();
        let mut env = sign(&sk, json!({"kind": "k", "not_after": 10.5}));
        env["sig"] = json!("AAAA");
        assert_eq!(verify_grant(&env, &pk, &Expect::default(), 1).unwrap_err().to_string(), "not_after is a float, and signed times are integers");
    }

    #[test]
    fn grant_key_sources() {
        let sk = key();
        let pk = sk.verifying_key().to_bytes();
        assert!(grant_key_env(["".to_string()]).unwrap_err().contains(GRANT_KEY_ENV));
        assert_eq!(grant_key_env(["  ".to_string(), b64u(&pk)]).unwrap(), pk);
        let dir = tempfile::tempdir().unwrap();
        assert!(grant_key_file(dir.path()).is_err());
        let std = base64::engine::general_purpose::STANDARD.encode(pk);
        std::fs::write(dir.path().join(GRANT_KEY_FILE), format!("{std}\n")).unwrap();
        assert_eq!(grant_key_file(dir.path()).unwrap(), pk);
    }
}
