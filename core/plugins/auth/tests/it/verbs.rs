//! The `auth` verbs and their caller gate (awm/services/auth/tests/test_device_tokens.py and
//! test_no_login.py), with the manifest checked against the Python's (vectors/auth_verbs.json).

use std::collections::BTreeMap;

use awm_plugin_auth::now;
use awm_plugin_auth::store::AuthStore;
use awm_plugin_auth::tokens::{self, AVATAR_STAGE};
use awm_plugin_auth::verbs::{self, Caller};
use awm_plugin_sdk::VerbError;
use serde_json::{json, Value};

/// A fresh auth.db with a one-hour session, as the Python fixture set it.
fn auth() -> (tempfile::TempDir, AuthStore) {
    let dir = tempfile::tempdir().unwrap();
    let store = AuthStore::open(AuthStore::path_in(dir.path()), true).unwrap();
    verbs::config_set(&store, &json!({"values": {"session_ttl_hours": 1.0, "max_session_days": 30.0}})).unwrap();
    (dir, store)
}

fn edge(as_: &str) -> Caller<'_> {
    Caller { as_: Some(as_), plugin: None }
}

fn plugin(p: &str) -> Caller<'_> {
    Caller { as_: None, plugin: Some(p) }
}

fn mint(store: &AuthStore, label: &str, caller: Caller<'_>) -> Result<Value, VerbError> {
    verbs::device_mint(store, &json!({"label": label, "scope": AVATAR_STAGE}), caller, now())
}

fn minted(store: &AuthStore, label: &str) -> Value {
    mint(store, label, Caller::OPERATOR).unwrap()
}

fn live(store: &AuthStore) -> BTreeMap<String, Value> {
    let m = verbs::edge_material(store, &json!({"token_scopes": [AVATAR_STAGE]}), Caller::OPERATOR).unwrap();
    m["device_tokens"].as_array().unwrap().iter().map(|t| (t["id"].as_str().unwrap().to_string(), t.clone())).collect()
}

fn listed(store: &AuthStore) -> Vec<Value> {
    verbs::device_list(store, Caller::OPERATOR).unwrap()["tokens"].as_array().unwrap().clone()
}

fn id(v: &Value) -> String {
    v["id"].as_str().unwrap().to_string()
}

fn assert_err(r: Result<Value, VerbError>, class: &str, needle: &str) {
    let e = r.expect_err("an error");
    assert_eq!(e.error_class, class, "{}", e.message);
    assert!(e.message.contains(needle), "{:?} lacks {needle:?}", e.message);
}

// -- mint --------------------------------------------------------------------------------------

#[test]
fn test_mint_returns_the_token_once_and_stores_only_its_digest() {
    let (_d, store) = auth();
    let out = minted(&store, "kiosk");
    let (token_id, secret) = tokens::parse(out["token"].as_str().unwrap()).unwrap();
    assert_eq!(out["id"], token_id);
    assert_eq!(token_id.len(), 16);
    assert_eq!(out["username"], format!("device:{token_id}"));
    assert_eq!(out["label"], "kiosk");
    assert!(out["last_used_at"].is_null() && out["revoked_at"].is_null());
    let keys: Vec<&str> = out.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["id", "username", "label", "created_at", "last_used_at", "revoked_at", "scope", "token"]);
    let row = store.device_get(token_id).unwrap().unwrap();
    assert_eq!(row.secret_sha256, tokens::digest(secret));
    assert!(!format!("{row:?}").contains(secret));
    let l = listed(&store);
    let keys: Vec<&str> = l[0].as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["id", "username", "label", "created_at", "last_used_at", "revoked_at", "scope"]);
    assert_eq!(l[0]["scope"], AVATAR_STAGE);
}

#[test]
fn test_mint_refuses_bad_arguments() {
    let (_d, store) = auth();
    for (args, needle) in [
        (json!({"scope": AVATAR_STAGE}), "label"),
        (json!({"label": "   ", "scope": AVATAR_STAGE}), "label"),
        (json!({"label": "x".repeat(65), "scope": AVATAR_STAGE}), "longer"),
        (json!({"label": "phone"}), "needs a scope"),
        (json!({"label": "phone", "scope": "everything"}), "a device token needs a scope; known: avatar-stage"),
    ] {
        assert_err(verbs::device_mint(&store, &args, Caller::OPERATOR, now()), "ValueError", needle);
    }
    assert!(verbs::device_mint(&store, &json!({"label": "é".repeat(64), "scope": AVATAR_STAGE}), Caller::OPERATOR, now()).is_ok());
}

#[test]
fn test_a_device_token_never_speaks_for_the_operator() {
    let (_d, store) = auth();
    let out = verbs::device_mint(&store, &json!({"username": "operator", "label": "kiosk", "scope": AVATAR_STAGE}), Caller::OPERATOR, now()).unwrap();
    assert_eq!(out["username"], format!("device:{}", id(&out)));
}

// -- edge material -----------------------------------------------------------------------------

#[test]
fn test_edge_material_carries_live_tokens_as_id_sub_digest() {
    let (_d, store) = auth();
    let out = minted(&store, "kiosk");
    let (_, secret) = tokens::parse(out["token"].as_str().unwrap()).unwrap();
    let want = BTreeMap::from([(
        id(&out),
        json!({"id": out["id"], "sub": format!("device:{}", id(&out)), "sha256": tokens::digest(secret), "scope": AVATAR_STAGE}),
    )]);
    assert_eq!(live(&store), want);
}

#[test]
fn test_a_scoped_token_goes_only_to_an_edge_that_enforces_its_scope() {
    let (_d, store) = auth();
    let scoped = minted(&store, "phone");
    for scopes in [json!([]), json!(["other"]), json!("avatar-stage"), Value::Null] {
        let got = verbs::edge_material(&store, &json!({"token_scopes": scopes}), Caller::OPERATOR).unwrap();
        assert_eq!(got["device_tokens"], json!([]), "{scopes}");
    }
    assert_eq!(live(&store)[&id(&scoped)]["scope"], AVATAR_STAGE);
}

#[test]
fn an_unscoped_token_goes_to_every_edge() {
    let (_d, store) = auth();
    store.device_insert("0123456789abcdef", "device:0123456789abcdef", "old", "d", None, 1.0).unwrap();
    store.device_insert("fedcba9876543210", "device:fedcba9876543210", "blank", "e", Some(""), 2.0).unwrap();
    let got = verbs::edge_material(&store, &json!({}), Caller::OPERATOR).unwrap();
    assert_eq!(
        got["device_tokens"],
        json!([{"id": "0123456789abcdef", "sub": "device:0123456789abcdef", "sha256": "d"},
               {"id": "fedcba9876543210", "sub": "device:fedcba9876543210", "sha256": "e"}])
    );
}

#[test]
fn test_revoke_drops_the_token_from_the_material_and_keeps_the_row() {
    let (_d, store) = auth();
    let (keep, gone) = (minted(&store, "a"), minted(&store, "b"));
    let revoked = verbs::device_revoke(&store, &json!({"id": gone["id"]}), Caller::OPERATOR, now()).unwrap();
    assert!(!revoked["revoked_at"].is_null());
    assert_eq!(live(&store).into_keys().collect::<Vec<_>>(), [id(&keep)]);
    let rows: BTreeMap<String, Value> = listed(&store).into_iter().map(|t| (id(&t), t)).collect();
    assert_eq!(rows[&id(&gone)]["revoked_at"], revoked["revoked_at"]);
    let again = verbs::device_revoke(&store, &json!({"id": gone["id"]}), Caller::OPERATOR, now() + 5.0).unwrap();
    assert_eq!(again["revoked_at"], revoked["revoked_at"]);
}

#[test]
fn test_revoke_of_an_unknown_id_is_an_error() {
    let (_d, store) = auth();
    assert_err(
        verbs::device_revoke(&store, &json!({"id": "0123456789abcdef"}), Caller::OPERATOR, now()),
        "ValueError",
        "no such device token '0123456789abcdef'",
    );
    assert_err(verbs::device_revoke(&store, &json!({}), Caller::OPERATOR, now()), "ValueError", "id is required");
}

#[test]
fn test_reported_use_moves_last_used_forward_only() {
    let (_d, store) = auth();
    let out = minted(&store, "kiosk");
    verbs::edge_material(&store, &json!({"device_used": {id(&out): 2000.0}}), Caller::OPERATOR).unwrap();
    assert_eq!(listed(&store)[0]["last_used_at"], 2000.0);
    verbs::edge_material(&store, &json!({"device_used": {id(&out): 1000.0, "junk": "x", "0123456789abcdef": 5.0}}), Caller::OPERATOR)
        .unwrap();
    assert_eq!(listed(&store)[0]["last_used_at"], 2000.0);
    verbs::edge_material(&store, &json!({"device_used": {id(&out): 3000}}), Caller::OPERATOR).unwrap();
    assert_eq!(listed(&store)[0]["last_used_at"], 3000.0);
}

#[test]
fn edge_material_fails_closed_without_a_secret() {
    let (_d, store) = auth();
    store.db().connection().unwrap().execute_batch("DELETE FROM awm_secret").unwrap();
    assert_err(verbs::edge_material(&store, &json!({}), Caller::OPERATOR), "RuntimeError", "refusing to mint");
    assert!(store.secret().is_err(), "nothing minted one");
}

// -- who may manage tokens ---------------------------------------------------------------------

#[test]
fn test_every_caller_across_an_edge_manages_nothing() {
    for as_ in ["user:tony", "peer", "peer:mira", "user:operator", "operator", "agent-slug", "user:Not A Name", ""] {
        let (_d, store) = auth();
        let out = minted(&store, "kiosk");
        assert_err(mint(&store, "kiosk", edge(as_)), "PermissionError", "device_mint is an operator verb");
        assert_err(verbs::device_list(&store, edge(as_)), "PermissionError", "device_list is an operator verb");
        assert_err(
            verbs::device_revoke(&store, &json!({"id": out["id"]}), edge(as_), now()),
            "PermissionError",
            "device_revoke is an operator verb: run it on the host, not through an edge",
        );
        assert!(live(&store).contains_key(&id(&out)), "{as_:?}");
    }
}

#[test]
fn test_the_verbs_stay_off_the_agent_surface() {
    let table = verbs::verbs();
    for verb in ["device_mint", "device_list", "device_revoke"] {
        let v = table.iter().find(|v| v.name == verb).unwrap();
        assert!(v.hidden, "{verb}");
        assert!(!v.params.as_array().unwrap().iter().any(|p| p["name"] == "username"), "{verb}");
    }
}

#[test]
fn the_manifest_is_pythons() {
    let want: Value = serde_json::from_str(include_str!("../vectors/auth_verbs.json")).unwrap();
    let got: Vec<Value> = verbs::verbs()
        .into_iter()
        .map(|v| {
            let surfaces = if v.hidden { json!(["cli", "http"]) } else { json!(["cli", "http", "mcp"]) };
            json!({"name": v.name, "tool": format!("{}_{}", verbs::TOOL, v.name), "surfaces": surfaces,
                   "description": v.description, "params": v.params})
        })
        .collect();
    assert_eq!(Value::Array(got), want["functions"]);
    let (_d, store) = auth();
    assert_eq!(verbs::config_fragment(&store), want["config"]);
    for name in want["handlers"].as_array().unwrap() {
        let r = verbs::call(&store, name.as_str().unwrap(), &json!({"label": "x", "scope": AVATAR_STAGE, "id": "0"}), Caller::OPERATOR, now());
        assert!(r.as_ref().map_or_else(|e| e.message.contains("no such device token"), |_| true), "{name}: {:?}", r.err());
    }
}

// -- plugin callers ----------------------------------------------------------------------------

#[test]
fn test_no_plugin_reads_edge_material() {
    let (_d, store) = auth();
    for p in ["dsh", "claude-science", "search"] {
        assert_err(verbs::edge_material(&store, &json!({}), plugin(p)), "PermissionError", &format!("edge_material is not open to plugin '{p}'"));
    }
    assert!(verbs::edge_material(&store, &json!({}), Caller::OPERATOR).unwrap().get("secret").is_some());
    let edge = Caller { as_: Some("user:tony"), plugin: None };
    assert_err(verbs::edge_material(&store, &json!({}), edge), "PermissionError", "operator verb");
}

#[test]
fn test_no_plugin_manages_device_tokens() {
    let (_d, store) = auth();
    let token_id = id(&minted(&store, "kiosk"));
    for p in ["httpsfront", "dsh", "claude-science", "auth"] {
        assert_err(mint(&store, "x", plugin(p)), "PermissionError", p);
        assert_err(verbs::device_list(&store, plugin(p)), "PermissionError", p);
        assert_err(verbs::device_revoke(&store, &json!({"id": token_id}), plugin(p), now()), "PermissionError", p);
    }
    assert!(live(&store).contains_key(&token_id), "the refused revoke changed nothing");
}

// -- test_no_login.py --------------------------------------------------------------------------

#[test]
fn test_the_login_and_rotation_verbs_are_gone() {
    let (_d, store) = auth();
    let names: Vec<&str> = verbs::verbs().iter().map(|v| v.name).collect();
    for verb in ["password", "verify", "rotate", "peer_credential", "user_add", "user_passwd", "user_disable", "user_list", "login"] {
        assert!(!names.contains(&verb), "{verb}");
        assert!(verbs::call(&store, verb, &json!({}), Caller::OPERATOR, now()).is_err(), "{verb}");
    }
}

#[test]
fn test_edge_material_is_the_secret_the_tokens_and_the_knobs() {
    let (_d, store) = auth();
    let out = verbs::edge_material(&store, &json!({}), Caller::OPERATOR).unwrap();
    let keys: Vec<&str> = out.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["secret", "device_tokens", "session_ttl_seconds", "max_session_seconds"]);
    assert_eq!(out["session_ttl_seconds"], 3600.0);
    assert_eq!(out["max_session_seconds"], 30.0 * 86400.0);
    assert_eq!(out["secret"], store.secret().unwrap());
}

#[test]
fn config_get_answers_the_contract_and_its_values() {
    let (_d, store) = auth();
    let got = verbs::config_get(&store).unwrap();
    assert_eq!(got["title"], "Authentication");
    assert_eq!(got["values"], json!({"session_ttl_hours": 1.0, "max_session_days": 30.0}));
    assert_err(verbs::config_set(&store, &json!({"values": {"session_ttl_hours": "soon"}})), "ValueError", "invalid settings");
    assert_eq!(verbs::config_get(&store).unwrap()["values"]["session_ttl_hours"], 1.0, "a refused value never lands");
}
