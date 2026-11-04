//! Check a grant that alcor's approver signed (fleet.md R97 item 3).
//!
//! Twin of operations' `grantlib/verify_grant.py`. The signature is Ed25519 over the grant's
//! canonical JSON: keys sorted at every level, separators `,` and `:`, UTF-8,
//! integer times.

use base64::Engine;
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum GrantError {
    #[error("no grant")]
    Missing,
    #[error("grant signed by another key")]
    WrongKey,
    #[error("grant signature does not verify")]
    BadSignature,
    #[error("grant is outside its window")]
    Expired,
    #[error("grant is for {field} {got:?}, not {want:?}")]
    Mismatch { field: &'static str, got: String, want: String },
    #[error("grant does not cover this request")]
    WrongPayload,
    #[error("grant does not cover node {0:?}")]
    WrongNode(String),
    #[error("a signed time is a float, and signed times are integers")]
    FloatTime,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Grant {
    pub grant_id: String,
    #[serde(default)]
    pub request_id: String,
    pub kind: String,
    /// One node, or `None` when `nodes` lists them (an admin_ssh grant for a run).
    #[serde(default)]
    pub node: Option<String>,
    #[serde(default)]
    pub nodes: Vec<String>,
    pub subject: String,
    pub payload_sha256: String,
    pub not_before: i64,
    pub not_after: i64,
    #[serde(default)]
    pub approved_by_device: String,
}

/// What a grant must name to pass.
#[derive(Debug, Default)]
pub struct Expect<'a> {
    pub kind: Option<&'a str>,
    pub node: Option<&'a str>,
    pub subject: Option<&'a str>,
    pub payload: Option<&'a Value>,
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

pub fn payload_digest(payload: &Value) -> String {
    hex::encode(Sha256::digest(canonical(payload)))
}

pub fn key_id(public_key: &[u8; 32]) -> String {
    hex::encode(Sha256::digest(public_key))[..16].to_string()
}

/// The grant inside `envelope` when it is genuine and fits `expect` at `now`.
pub fn verify_grant(envelope: &Value, public_key: &[u8; 32], expect: &Expect, now: i64) -> Result<Grant, GrantError> {
    let body = envelope.get("grant").filter(|g| g.is_object()).ok_or(GrantError::Missing)?;
    if envelope.get("key_id").and_then(Value::as_str) != Some(key_id(public_key).as_str()) {
        return Err(GrantError::WrongKey);
    }
    let sig_b64 = envelope.get("sig").and_then(Value::as_str).unwrap_or("");
    let sig = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(sig_b64.trim_end_matches('='))
        .ok()
        .and_then(|b| Signature::from_slice(&b).ok())
        .ok_or(GrantError::BadSignature)?;
    let key = VerifyingKey::from_bytes(public_key).map_err(|_| GrantError::WrongKey)?;
    if body.as_object().is_some_and(|m| m.values().any(Value::is_f64)) {
        return Err(GrantError::FloatTime);
    }
    key.verify(&canonical(body), &sig).map_err(|_| GrantError::BadSignature)?;
    let grant: Grant = serde_json::from_value(body.clone()).map_err(|_| GrantError::Missing)?;
    if !(grant.not_before <= now && now < grant.not_after) {
        return Err(GrantError::Expired);
    }
    let check = |field: &'static str, got: &str, want: Option<&str>| match want {
        Some(w) if w != got => Err(GrantError::Mismatch { field, got: got.into(), want: w.into() }),
        _ => Ok(()),
    };
    check("kind", &grant.kind, expect.kind)?;
    if let Some(n) = expect.node {
        if grant.node.as_deref() != Some(n) && !grant.nodes.iter().any(|g| g == n) {
            return Err(GrantError::WrongNode(n.into()));
        }
    }
    check("subject", &grant.subject, expect.subject)?;
    if let Some(p) = expect.payload {
        if grant.payload_sha256 != payload_digest(p) {
            return Err(GrantError::WrongPayload);
        }
    }
    Ok(grant)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};
    use serde_json::json;

    pub(crate) fn sign(sk: &SigningKey, grant: Value) -> Value {
        let pk = sk.verifying_key().to_bytes();
        let sig = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(sk.sign(&canonical(&grant)).to_bytes());
        json!({"grant": grant, "sig": sig, "key_id": key_id(&pk)})
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
    fn verifies_and_refuses() {
        let sk = key();
        let pk = sk.verifying_key().to_bytes();
        let payload = json!({"session_id": "s1", "from": "cooperative", "to": "trusted"});
        let grant = json!({"grant_id": "g1", "request_id": "r1", "kind": "trust_raise", "node": "home",
            "subject": "s1", "payload_sha256": payload_digest(&payload), "not_before": 100, "not_after": 200,
            "approved_by_device": "d"});
        let env = sign(&sk, grant);
        let ok = Expect { kind: Some("trust_raise"), node: Some("home"), subject: Some("s1"), payload: Some(&payload) };
        assert_eq!(verify_grant(&env, &pk, &ok, 150).unwrap().grant_id, "g1");
        assert_eq!(verify_grant(&env, &pk, &ok, 200).unwrap_err(), GrantError::Expired);
        let other = Expect { kind: Some("invite"), ..Default::default() };
        assert!(matches!(verify_grant(&env, &pk, &other, 150), Err(GrantError::Mismatch { .. })));
        let mut forged = env.clone();
        forged["grant"]["subject"] = json!("s2");
        assert_eq!(verify_grant(&forged, &pk, &ok, 150).unwrap_err(), GrantError::BadSignature);
        let wrong = Expect { payload: Some(&json!({"to": "trusted"})), ..ok };
        assert_eq!(verify_grant(&env, &pk, &wrong, 150).unwrap_err(), GrantError::WrongPayload);
        let pk2 = key().verifying_key().to_bytes();
        assert_eq!(verify_grant(&env, &pk2, &ok, 150).unwrap_err(), GrantError::WrongKey);
    }
}
