//! mic's describe equals the baseline captured from the Python at release 49232bf9.

use std::path::PathBuf;
use std::sync::Arc;

use awm_plugin_mic::audio::Audio;
use awm_plugin_mic::bridge::Mic;
use awm_plugin_sdk::{Env, Service};
use serde_json::{json, Value};

#[test]
fn describe_matches_the_baseline() {
    let env = Env { hub_url: "http://127.0.0.1:1".into(), service: "mic".into(), plugin: "mic".into(), node: "here".into(), awm_dir: PathBuf::from("/nonexistent"), token_file: None };
    let mic = Arc::new(Mic::new(Arc::new(Audio::system()), "virtmic"));
    let api = awm_plugin_mic::service(Service::new(env), mic).manifest();
    let mut verbs: Vec<Value> = api["functions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| json!({"description": f["description"], "params": f["params"], "verb": f["name"]}))
        .collect();
    verbs.sort_by(|a, b| a["verb"].as_str().cmp(&b["verb"].as_str()));
    let got = json!({"plugin": "mic", "tool": "mic", "verbs": verbs});
    let want: Value = serde_json::from_str(include_str!("../../../../../docs/plugin-redux/ports/baseline/mic.describe.json")).unwrap();
    assert_eq!(got, want);
    assert_eq!(api["sessions"], json!([{"kind": "stream", "transport": "direct"}]));
    assert_eq!(api["emitters"], json!([]));
    for f in api["functions"].as_array().unwrap() {
        assert_eq!(f["tool"], format!("mic_{}", f["name"].as_str().unwrap()));
    }
}
