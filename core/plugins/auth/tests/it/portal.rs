//! The portal grant check and its helpers, against Python's (`vectors/portal.json`), and the
//! pure parts of httpsfront's `test_portal_login`.

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;

use awm_plugin_auth::portal::{self, decode_grant, encode_grant, host_of, local_path, login_url_at, GrantInvalid, Grants};
use awm_trust::grant::{b64u, payload_digest, sign_envelope};
use ed25519_dalek::SigningKey;
use serde_json::{json, Value};

fn vectors() -> Value {
    serde_json::from_str(include_str!("../vectors/portal.json")).unwrap()
}

fn result(r: Result<String, GrantInvalid>) -> Value {
    match r {
        Ok(sub) => json!({"subject": sub}),
        Err(e) => json!({"error": e.0}),
    }
}

fn at(key: &str, node: &str, now: f64) -> Grants {
    Grants::with_clock(Some(key.to_string()), node, Box::new(move || now))
}

#[test]
fn every_grant_gets_pythons_answer() {
    let v = vectors();
    let (key, node) = (v["key"].as_str().unwrap(), v["node"].as_str().unwrap());
    for case in v["accept"].as_array().unwrap() {
        let grants = at(key, node, case["now"].as_f64().unwrap());
        let got = grants.accept(case["raw"].as_str().unwrap(), case["host"].as_str().unwrap(), case["path"].as_str().unwrap());
        assert_eq!(result(got), case["result"], "{}", case["name"]);
    }
}

#[test]
fn a_spent_grant_is_remembered_until_it_expires_as_python_remembers_it() {
    let v = vectors();
    let clock = Arc::new(AtomicI64::new(0));
    let c = clock.clone();
    let grants = Grants::with_clock(Some(v["key"].as_str().unwrap().into()), v["node"].as_str().unwrap(), Box::new(move || c.load(Ordering::SeqCst) as f64));
    for step in v["sequence"].as_array().unwrap() {
        clock.store(step["now"].as_i64().unwrap(), Ordering::SeqCst);
        let got = grants.accept(step["raw"].as_str().unwrap(), step["host"].as_str().unwrap(), step["path"].as_str().unwrap());
        assert_eq!(result(got), step["result"], "at {}", step["now"]);
    }
}

#[test]
fn no_key_refuses_as_python_refuses() {
    let v = vectors();
    let good = v["accept"][0]["raw"].as_str().unwrap();
    for key in [None, Some("  ".to_string())] {
        let grants = Grants::with_clock(key, "altair", Box::new(|| 1_800_000_000.0));
        assert_eq!(result(grants.accept(good, "node.example.test", "/trilium/")), v["no_key"]);
    }
}

#[test]
fn the_helpers_are_pythons() {
    let v = vectors();
    assert_eq!(portal::CALLBACK, v["callback"]);
    assert_eq!(portal::DEFAULT_ORIGIN, v["default_origin"]);
    for row in v["login_url"].as_array().unwrap() {
        let origin = row["origin"].as_str().unwrap_or("");
        assert_eq!(login_url_at(origin, row["visit"].as_str().unwrap()), row["url"].as_str().unwrap(), "{row}");
    }
    for row in v["encode_grant"].as_array().unwrap() {
        assert_eq!(encode_grant(&row["envelope"]), row["raw"].as_str().unwrap());
        assert_eq!(decode_grant(row["raw"].as_str().unwrap()).unwrap(), row["envelope"]);
    }
    for row in v["decode"].as_array().unwrap() {
        let got = match decode_grant(row["raw"].as_str().unwrap()) {
            Ok(env) => json!({"envelope": env}),
            Err(e) => json!({"error": e.0}),
        };
        let mut want = row.clone();
        want.as_object_mut().unwrap().remove("raw");
        assert_eq!(got, want, "{}", row["raw"]);
    }
    for row in v["local_path"].as_array().unwrap() {
        assert_eq!(local_path(row["raw"].as_str()), row["path"].as_str(), "{}", row["raw"]);
    }
    for row in v["host_of"].as_array().unwrap() {
        assert_eq!(host_of(row["header"].as_str()), row["host"].as_str().unwrap(), "{}", row["header"]);
    }
}

// --- test_portal_login, the parts below the route -------------------------------------------

const HOST: &str = "front.example";

struct Fixture {
    key: SigningKey,
    grants: Grants,
}

fn fixture() -> Fixture {
    let key = SigningKey::from_bytes(&[7u8; 32]);
    let grants = Grants::new(Some(b64u(&key.verifying_key().to_bytes())), "altair");
    Fixture { key, grants }
}

struct G<'a> {
    node: &'a str,
    host: &'a str,
    path: &'a str,
    kind: &'a str,
    ttl: i64,
    grant_id: &'a str,
}

impl Default for G<'_> {
    fn default() -> Self {
        G { node: "altair", host: HOST, path: "/trilium/", kind: "portal_login", ttl: 60, grant_id: "gabc" }
    }
}

fn grant(key: &SigningKey, g: G) -> String {
    let now = awm_plugin_auth::now() as i64;
    let body = json!({"grant_id": g.grant_id, "request_id": "", "kind": g.kind, "node": g.node, "subject": "tony",
        "payload_sha256": payload_digest(&json!({"site": g.host, "return": g.path})),
        "not_before": now, "not_after": now + g.ttl, "approved_by_device": "d1"});
    encode_grant(&sign_envelope(key, "grant", body))
}

#[test]
fn test_a_grant_for_anything_else_is_refused() {
    let f = fixture();
    let other = SigningKey::from_bytes(&[9u8; 32]);
    let bad = [
        grant(&f.key, G { node: "capella", ..G::default() }),
        grant(&f.key, G { host: "other.example", ..G::default() }),
        grant(&f.key, G { path: "/elsewhere", ..G::default() }),
        grant(&f.key, G { kind: "admin_ssh", ..G::default() }),
        grant(&other, G::default()),
        grant(&f.key, G { ttl: -1, ..G::default() }),
    ];
    for g in bad {
        assert!(f.grants.accept(&g, HOST, "/trilium/").is_err());
    }
}

#[test]
fn no_error_carries_the_grant() {
    let f = fixture();
    let g = grant(&f.key, G { node: "capella", ..G::default() });
    let err = f.grants.accept(&g, HOST, "/trilium/").unwrap_err();
    assert!(!err.to_string().contains(&g[..16]));
    let err = f.grants.accept(&format!("{g}!"), HOST, "/trilium/").unwrap_err();
    assert!(!err.to_string().contains(&g[..16]));
}
