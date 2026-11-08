//! The verb table and the handlers have to agree, and the write verbs stay off MCP.

use std::collections::BTreeSet;
use std::path::Path;
use std::sync::Arc;

use crate::common;
use awm_plugin_dvc::jobs::JOBS;
use awm_plugin_dvc::service::{self, verbs, Dvc, VerbSpec};
use awm_plugin_dvc::watcher;
use serde_json::{json, Value};

fn verb(name: &str) -> VerbSpec {
    verbs().into_iter().find(|v| v.name == name).unwrap_or_else(|| panic!("no verb {name}"))
}

#[test]
fn the_verb_table_is_the_wire_contract() {
    let names: BTreeSet<&str> = verbs().iter().map(|v| v.name).collect();
    let want: BTreeSet<&str> =
        ["status", "resolve", "pull", "push", "sync", "coverage", "task", "jobs", "runs", "run", "schedule", "scope_status", "mount", "gc"].into();
    assert_eq!(names, want);

    let off: BTreeSet<&str> = verbs().iter().filter(|v| v.cli_http_only).map(|v| v.name).collect();
    assert_eq!(off, ["run", "schedule"].into(), "only the write verbs stay off MCP");

    for v in verbs() {
        assert!(v.description.len() > 40 && v.timeout > 0.0 && v.params.is_array(), "{}", v.name);
        for p in v.params.as_array().unwrap() {
            assert!(p["name"].is_string() && p["type"].is_string() && p["required"].is_boolean(), "{}: {p}", v.name);
        }
    }
    for name in ["sync", "run", "pull", "push", "task"] {
        assert!(verb(name).timeout >= 300.0, "{name}");
    }

    let params = verb("run").params;
    let described = params[0]["description"].as_str().unwrap();
    assert!(JOBS.iter().all(|j| described.contains(j.name)));

    let gc = verb("gc");
    let req: Vec<bool> = gc.params.as_array().unwrap().iter().filter(|p| p["name"] == "projects").map(|p| p["required"].as_bool().unwrap()).collect();
    assert_eq!(req, [true], "gc requires the caller to name its projects");
    assert!(gc.description.contains("DRY RUN BY DEFAULT"));

    assert_eq!(watcher::TOPIC, "job.status");
    assert!(service::EMITTER.contains("succeeded"));
}

#[test]
fn the_plugin_manifest_names_the_tool_and_turns_it_on() {
    let text = std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("awm-plugin.toml")).unwrap();
    let doc: toml::Table = text.parse().unwrap();
    let tools: Vec<(String, String)> =
        doc["tool"].as_array().unwrap().iter().map(|t| (t["name"].as_str().unwrap().to_string(), t["tier"].as_str().unwrap().to_string())).collect();
    assert_eq!(tools, [("dvc".to_string(), "on".to_string())]);
}

#[tokio::test]
async fn every_declared_verb_has_a_handler() {
    let w = common::workspace();
    let dvc = Arc::new(Dvc {
        paths: w.paths.clone(),
        dao: w.dao(),
        api: common::FakeApi::new(|m, p, _| Err(awm_plugin_dvc::globus::GlobusError(format!("offline: {m} {p}")))),
        watcher: None,
        hub: None,
        plugin_dir: w.root.join("plugin"),
        hook_bin: "/bin/true".into(),
    });
    for v in verbs() {
        let out = dvc.handle(v.name, json!({}), false).await;
        if let Err(e) = &out {
            assert!(!e.message.contains("unknown verb"), "{}: {e}", v.name);
        }
    }
    let unknown = dvc.handle("nope", Value::Null, false).await.unwrap_err();
    assert!(unknown.message.contains("unknown verb"));
}
