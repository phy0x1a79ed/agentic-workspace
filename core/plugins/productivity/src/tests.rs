//! The gate every verb passes, and the surface it gates.

use std::sync::Arc;

use awm_plugin_sdk::{Caller, TestService};
use serde_json::{json, Value};

use crate::config::Loaded;
use crate::verbs::Productivity;

fn test_env() -> awm_plugin_sdk::Env {
    awm_plugin_sdk::Env { hub_url: "http://127.0.0.1:1".into(), service: "productivity".into(), plugin: "productivity".into(), node: "alcor".into(), awm_dir: std::env::temp_dir(), token_file: None }
}

async fn svc() -> TestService {
    let loaded = Loaded { callers: vec!["mizar".into(), "capella".into(), "lyra".into()], ..Default::default() };
    let app = Arc::new(Productivity::new("alcor".into(), loaded));
    crate::service(awm_plugin_sdk::Service::new(test_env()), app).into_test().await.unwrap()
}

fn verbs() -> Vec<String> {
    manifest().iter().map(|f| f["name"].as_str().unwrap().to_string()).collect()
}

fn manifest() -> Vec<Value> {
    let m: Value = serde_json::from_str(crate::MANIFEST).unwrap();
    m["functions"].as_array().unwrap().clone()
}

/// `extra` with every required argument of `verb` filled, so the gate is what answers.
fn full(verb: &str, extra: &Value) -> Value {
    let mut args = extra.clone();
    let f = manifest().into_iter().find(|f| f["name"] == verb).unwrap();
    for p in f["params"].as_array().into_iter().flatten().filter(|p| p["required"] == true) {
        args[p["name"].as_str().unwrap()] = json!("x");
    }
    args
}

fn from(node: &str) -> Caller {
    Caller { node: node.into(), ..Default::default() }
}

#[test]
fn no_verb_sends() {
    for v in verbs() {
        assert!(!["send", "post", "reply", "forward", "react"].iter().any(|w| v.contains(w)), "{v} reads like a send");
    }
}

#[tokio::test]
async fn every_verb_refuses_every_node_but_the_callers_and_every_seat() {
    let s = svc().await;
    let refused = [
        ("altair", from("altair"), json!({})),
        ("alcor itself", from("alcor"), json!({})),
        ("no node", from(""), json!({})),
        ("altair relayed by mizar", Caller { origin: Some("altair".into()), ..from("mizar") }, json!({})),
        ("a seat by name", Caller { name: Some("seat:slack/dm/x".into()), ..from("mizar") }, json!({})),
        ("a seat by scope", Caller { scope: Some("seats/x".into()), ..from("mizar") }, json!({})),
        ("a seat by its claim", from("mizar"), json!({"_caller_session": {"kind": "bg", "name": "seat:discord/x"}})),
    ];
    for v in verbs() {
        for (who, caller, args) in &refused {
            let e = s.call_as(&v, full(&v, args), caller.clone(), None).await.unwrap_err();
            assert_eq!(e.error_class, "PermissionError", "{v} from {who}: {}", e.message);
        }
        let past = s.call_as(&v, full(&v, &json!({})), from("lyra"), Some("peer:lyra")).await;
        if let Err(e) = past {
            assert_ne!(e.error_class, "PermissionError", "{v} from lyra: {}", e.message);
            assert!(!e.message.contains("no handler"), "{v} is in the manifest but not dispatched");
        }
    }
}
