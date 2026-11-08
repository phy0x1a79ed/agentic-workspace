//! The verb table and the handlers have to agree, and the write verbs stay off MCP.

mod common;

use std::collections::BTreeSet;
use std::sync::Arc;

use awm_plugin_dvc::jobs::JOBS;
use awm_plugin_dvc::service::{self, verbs, Dvc, VerbSpec};
use awm_plugin_dvc::watcher;
use serde_json::{json, Value};

fn verb(name: &str) -> VerbSpec {
    verbs().into_iter().find(|v| v.name == name).unwrap_or_else(|| panic!("no verb {name}"))
}

#[test]
fn the_verb_names_are_the_python_manifests() {
    let names: BTreeSet<&str> = verbs().iter().map(|v| v.name).collect();
    let want: BTreeSet<&str> =
        ["status", "resolve", "pull", "push", "sync", "coverage", "task", "jobs", "runs", "run", "schedule", "scope_status", "mount", "gc"].into();
    assert_eq!(names, want);
    assert_eq!(verbs().len(), want.len());
}

#[test]
fn every_verb_has_a_description_and_a_timeout() {
    for v in verbs() {
        assert!(v.description.len() > 40, "{}", v.name);
        assert!(v.timeout > 0.0, "{}", v.name);
        assert!(v.params.is_array(), "{}", v.name);
        for p in v.params.as_array().unwrap() {
            assert!(p["name"].is_string() && p["type"].is_string() && p["required"].is_boolean(), "{}: {p}", v.name);
        }
    }
}

#[test]
fn the_write_verbs_are_kept_off_the_mcp_surface() {
    assert!(verb("run").cli_http_only);
    assert!(verb("schedule").cli_http_only);
    let off: BTreeSet<&str> = verbs().iter().filter(|v| v.cli_http_only).map(|v| v.name).collect();
    assert_eq!(off, ["run", "schedule"].into());
}

#[test]
fn the_read_verbs_are_on_every_surface() {
    for name in ["jobs", "runs"] {
        assert!(!verb(name).cli_http_only, "{name}");
    }
}

#[test]
fn the_job_status_emitter_is_declared() {
    assert_eq!(watcher::TOPIC, "job.status");
    assert!(service::EMITTER.contains("succeeded"));
}

#[test]
fn every_verb_that_moves_bytes_declares_a_timeout() {
    for name in ["sync", "run", "pull", "push", "task"] {
        assert!(verb(name).timeout >= 300.0, "{name}");
    }
}

#[test]
fn the_run_verb_names_the_jobs_that_actually_exist() {
    let params = verb("run").params;
    assert_eq!(params[0]["name"], "job");
    let described = params[0]["description"].as_str().unwrap();
    for j in &JOBS {
        assert!(described.contains(j.name), "{}", j.name);
    }
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
