//! scope's describe equals the baseline captured from the Python at release 49232bf9, with
//! the plan's two changes: `create` gains one sentence on the create hook, `reindex` goes.

mod common;

use serde_json::{json, Value};

#[test]
fn describe_matches_the_baseline() {
    let r = common::rig();
    let api = awm_plugin_scopes::service(awm_plugin_sdk::Service::new(common::env(&r.awm_dir)), r.op.app.clone()).manifest();
    let functions = api["functions"].as_array().unwrap();
    let mut verbs: Vec<Value> = functions
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

    let mut want: Value = serde_json::from_str(include_str!("../../../../docs/plugin-redux/ports/baseline/scope.describe.json")).unwrap();
    let list = want["verbs"].as_array_mut().unwrap();
    list.retain(|v| v["verb"] != "reindex");
    for v in list.iter_mut() {
        if v["verb"] == "create" {
            let d = format!("{} {}", v["description"].as_str().unwrap(), awm_plugin_scopes::service::CREATE_HOOK_SENTENCE);
            v["description"] = json!(d);
        }
    }
    assert_eq!(json!({"plugin": "scopes", "tool": "scope", "verbs": verbs}), want);

    for f in functions {
        assert_eq!(f["tool"], json!(format!("scope_{}", f["name"].as_str().unwrap())), "every verb publishes under the scope tool");
        assert!(f.get("category").is_none());
    }
    let timeouts: Vec<(String, f64)> = functions.iter().filter_map(|f| Some((f["name"].as_str()?.to_string(), f.get("timeout")?.as_f64()?))).collect();
    let mut timeouts = timeouts;
    timeouts.sort_by(|a, b| a.0.cmp(&b.0));
    let want_timeouts = [("awm_sweep", 600.0), ("create", 1800.0), ("delete", 600.0), ("git_gather", 900.0), ("git_scatter", 900.0), ("heal", 1800.0)];
    assert_eq!(timeouts, want_timeouts.map(|(n, t)| (n.to_string(), t)));
}
