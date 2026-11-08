//! transcripts' describe equals the baseline captured from the Python at release 49232bf9.

mod common;

use std::sync::Arc;

use awm_plugin_sdk::{Env, Service};
use serde_json::{json, Value};

#[test]
fn describe_matches_the_baseline() {
    let r = common::rig();
    let env = Env {
        hub_url: "http://127.0.0.1:1".into(),
        service: "transcripts".into(),
        plugin: "transcripts".into(),
        node: "here".into(),
        awm_dir: r.dir.path().into(),
        token_file: None,
    };
    let api = awm_plugin_transcripts::service(Service::new(env), Arc::new(r.app)).manifest();
    let mut verbs: Vec<Value> = api["functions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| {
            let mut v = json!({"description": f["description"], "params": f["params"], "verb": f["name"]});
            if let Some(s) = f.get("surfaces") {
                v["surfaces"] = s.clone();
            }
            v
        })
        .collect();
    verbs.sort_by(|a, b| a["verb"].as_str().cmp(&b["verb"].as_str()));
    let want: Value = serde_json::from_str(include_str!("../../../../docs/plugin-redux/ports/baseline/transcripts.describe.json")).unwrap();
    assert_eq!(json!({"plugin": "transcripts", "tool": "transcripts", "verbs": verbs}), want);
    let timeouts: Vec<(String, f64)> =
        api["functions"].as_array().unwrap().iter().map(|f| (f["name"].as_str().unwrap().to_string(), f["timeout"].as_f64().unwrap())).collect();
    let want_timeouts = [("status", 120.0), ("sweep", 900.0), ("search", 300.0), ("restore", 300.0), ("runs", 60.0), ("prune", 900.0), ("query", 300.0)];
    assert_eq!(timeouts, want_timeouts.map(|(n, t)| (n.to_string(), t)));
}
