//! What the verb table projects onto the gateway: the describe against the Python baseline, the
//! timeouts of the Python verb table, and the manifest's needs.

use std::path::PathBuf;

use crate::surface::{specs, TOOL};
use serde_json::{json, Map, Value};

fn baseline() -> Value {
    serde_json::from_str(include_str!("../../../../../docs/plugin-redux/ports/baseline/reflection.describe.json")).unwrap()
}

fn plugin_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// A param as `describe` shows it: `required` always, its type only when it is not a string.
fn as_described(param: &Value) -> Value {
    let mut p = Map::new();
    p.insert("name".into(), param["name"].clone());
    p.insert("required".into(), json!(param.get("required") == Some(&json!(true))));
    if let Some(t) = param.get("type").filter(|t| *t != "string") {
        p.insert("type".into(), t.clone());
    }
    for key in ["description", "default"] {
        if let Some(v) = param.get(key) {
            p.insert(key.into(), v.clone());
        }
    }
    Value::Object(p)
}

fn param_names(params: &[Value]) -> Vec<String> {
    params.iter().map(|p| p["name"].as_str().unwrap().to_string()).collect()
}

/// Every difference between the served verbs and the baseline, as `<verb>: <what>`.
fn differences() -> Vec<String> {
    let base = baseline();
    let base_verbs = base["verbs"].as_array().unwrap();
    let ours = specs();
    let mut diffs = vec![];
    for b in base_verbs {
        if !ours.iter().any(|s| b["verb"] == s.verb.as_str()) {
            diffs.push(format!("{}: dropped", b["verb"].as_str().unwrap()));
        }
    }
    for spec in &ours {
        let Some(b) = base_verbs.iter().find(|v| v["verb"] == spec.verb.as_str()) else {
            diffs.push(format!("{}: new verb", spec.verb));
            continue;
        };
        if b["description"] != spec.description.as_str() {
            diffs.push(format!("{}: description", spec.verb));
        }
        if b.get("surfaces").cloned().unwrap_or(Value::Null) != spec.surfaces.as_ref().map_or(Value::Null, |s| json!(s)) {
            diffs.push(format!("{}: surfaces", spec.verb));
        }
        let mine: Vec<Value> = spec.params.as_array().unwrap().iter().map(as_described).collect();
        let theirs = b["params"].as_array().unwrap();
        for p in &mine {
            match theirs.iter().find(|q| q["name"] == p["name"]) {
                None => diffs.push(format!("{}: adds param {}", spec.verb, p["name"].as_str().unwrap())),
                Some(q) if q != p => diffs.push(format!("{}: param {} changed", spec.verb, p["name"].as_str().unwrap())),
                Some(_) => {}
            }
        }
        for q in theirs {
            if !mine.iter().any(|p| p["name"] == q["name"]) {
                diffs.push(format!("{}: drops param {}", spec.verb, q["name"].as_str().unwrap()));
            }
        }
        let kept: Vec<String> = param_names(&mine).into_iter().filter(|n| theirs.iter().any(|q| q["name"] == n.as_str())).collect();
        if kept != param_names(theirs) && !diffs.iter().any(|d| d.contains("drops param")) {
            diffs.push(format!("{}: params reordered", spec.verb));
        }
    }
    diffs
}

/// The listed changes: `stop`'s optional `job`, `list_other`'s `include_stopped`, the new `probe`,
/// `to` on `message` and `resume_other`, the lifecycle params and the sender-prefix wording.
#[test]
fn the_describe_differs_from_the_baseline_only_by_the_listed_changes() {
    let want = [
        "adopt_other: adds param to",
        "adopt_other: drops param job",
        "cd: adds param to",
        "cd: drops param job",
        "compact: description",
        "effort: adds param to",
        "effort: drops param job",
        "harness_event: adds param needs",
        "harness_event: adds param rc_url",
        "harness_event: adds param retired",
        "harness_event: adds param session_kind",
        "harness_event: adds param state",
        "harness_event: adds param task_id",
        "harness_event: description",
        "harness_event: param kind changed",
        "interrupt: adds param to",
        "interrupt: drops param job",
        "link: adds param to",
        "link: drops param job",
        "list_other: adds param include_stopped",
        "mcp: adds param to",
        "mcp: drops param job",
        "message: adds param to",
        "message: description",
        "message: drops param job",
        "message_self: description",
        "model: adds param to",
        "model: drops param job",
        "probe: new verb",
        "remote_control: adds param to",
        "remote_control: drops param job",
        "rename: adds param to",
        "rename: drops param job",
        "resume_other: adds param to",
        "resume_other: description",
        "resume_other: drops param job",
        "start_other: description",
        "start_other: param role changed",
        "start_other: param suggest changed",
        "stop: adds param to",
        "stop: description",
        "stop: drops param job",
        "stop_other: adds param to",
        "stop_other: drops param job",
    ];
    let mut got = differences();
    got.sort();
    assert_eq!(got, want);
}

/// `surface.FUNCTIONS` timeouts in the Python reflection, plus `probe`.
#[test]
fn the_timeouts_match_the_python_verb_table() {
    let want: [(&str, Option<u64>); 27] = [
        ("message_self", Some(60)),
        ("compact", Some(60)),
        ("mode", Some(60)),
        ("remote_control", Some(60)),
        ("model", Some(60)),
        ("effort", Some(60)),
        ("rename", Some(60)),
        ("cd", Some(60)),
        ("mcp", Some(60)),
        ("interrupt", Some(60)),
        ("stop", Some(60)),
        ("link", Some(30)),
        ("message", Some(60)),
        ("pending", Some(30)),
        ("whoami", Some(30)),
        ("start_other", Some(1800)),
        ("list_other", None),
        ("probe", Some(30)),
        ("stop_other", Some(30)),
        ("resume_other", Some(90)),
        ("adopt_other", Some(30)),
        ("harness_event", Some(30)),
        ("session_of", Some(30)),
        ("wait", Some(60)),
        ("waits", None),
        ("retired", None),
        ("retired_among", None),
    ];
    let mut got: Vec<(String, Option<u64>)> = specs().into_iter().map(|s| (s.verb, s.timeout)).collect();
    let mut want: Vec<(String, Option<u64>)> = want.iter().map(|(v, t)| (v.to_string(), *t)).collect();
    got.sort();
    want.sort();
    assert_eq!(got, want);
}

#[test]
fn every_verb_folds_into_the_reflection_tool() {
    for s in specs() {
        let flat = format!("{TOOL}_{}", s.verb);
        assert_eq!(flat.split_once('_'), Some((TOOL, s.verb.as_str())), "the gateway folds {flat} on its first underscore");
    }
}

/// D1: a per-verb needs guard refused `scope claims`. The manifest declares one need, the harness
/// contract, and no tool list for a guard to read.
#[test]
fn the_manifest_needs_only_the_harness_contract() {
    let toml = std::fs::read_to_string(plugin_dir().join("awm-plugin.toml")).unwrap();
    let lines: Vec<&str> = toml.lines().map(str::trim).filter(|l| !l.starts_with('#') && !l.is_empty()).collect();
    assert_eq!(toml.matches("[[needs.contract]]").count(), 1, "{toml}");
    assert!(lines.contains(&r#"name = "harness""#) && lines.contains(&"major = 1"), "{toml}");
    for gone in ["[[needs.tool]]", "tools =", "path =", "run ="] {
        assert!(!lines.iter().any(|l| l.starts_with(gone)), "{gone} in {toml}");
    }
    let tool = toml.split("[[tool]]").nth(1).unwrap();
    assert!(tool.contains(r#"name = "reflection""#) && tool.contains(r#"implements = ["reflection@1"]"#), "{toml}");
}
