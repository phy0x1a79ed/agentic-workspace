//! The `list` verb as a peer sees it: the peer refusal and the private marks over the session,
//! pool and isolated rows. The marks live under the process-wide `AWM_DIR`, so these tests share
//! one and take turns.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use awm_plugin_sdk::config::{self, EdgeList};
use awm_plugin_sdk::privacy;
use cch::verbs::testing::Fake;
use cch::verbs::App;
use serde_json::{json, Value};
use tempfile::TempDir;

const PARENT_JOB: &str = "aaaa0001";
const CHILD_JOB: &str = "bbbb0002";
const ISO_SID: &str = "00000000-0000-4000-8000-0000000000aa";

/// The shared `AWM_DIR`, set once, and the turn of the test that holds the guard.
async fn turn() -> tokio::sync::MutexGuard<'static, ()> {
    static DIR: OnceLock<TempDir> = OnceLock::new();
    static TURN: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
    let guard = TURN.lock().await;
    let dir = DIR.get_or_init(|| {
        let dir = TempDir::new().unwrap();
        std::env::set_var("AWM_DIR", dir.path().join(".awm"));
        std::fs::create_dir_all(dir.path().join(".awm")).unwrap();
        dir
    });
    assert!(dir.path().is_dir());
    privacy::write_marks(&[]).unwrap();
    guard
}

fn mark(project: &str, path: &Path) {
    privacy::write_marks(&[json!({"project": project, "scope": null, "path": path.display().to_string()})]).unwrap();
}

/// capella holds an edge to this node; john is in the fleet and holds none.
fn peers() -> EdgeList {
    let here = config::here();
    let yaml = format!(
        "nodes:\n  {here}: {{role: homebase}}\n  capella: {{role: homebase}}\n  john: {{role: homebase}}\nedges:\n  capella: {{{here}: lan}}\n  {here}: {{john: lan}}\n"
    );
    EdgeList::parse(&yaml).unwrap()
}

struct Rig {
    _dir: TempDir,
    projects: PathBuf,
    app: App<Fake>,
}

fn rig(adjust: impl FnOnce(&Path, Fake) -> Fake) -> Rig {
    let dir = TempDir::new().unwrap();
    let projects = dir.path().join("workspace").join("projects");
    let fake = adjust(&projects, Fake { edges: Some(peers()), ..Fake::under(dir.path()) });
    Rig { _dir: dir, projects, app: App::new(fake) }
}

async fn list(app: &App<Fake>, as_: Option<&str>) -> Value {
    app.handle("list", &json!({}), as_).await.unwrap()
}

fn jobs(out: &Value) -> Vec<&str> {
    out["sessions"].as_array().unwrap().iter().filter_map(|r| r["session_id"].as_str()).collect()
}

#[tokio::test]
async fn test_the_list_verb_applies_the_peer_refusal() {
    let _turn = turn().await;
    let r = rig(|_, f| f);
    for who in ["peer", "peer:john", "peer:stranger"] {
        let out = list(&r.app, Some(who)).await;
        assert!(out["ok"] == false && out.get("sessions").is_none(), "{who}: {out}");
    }
    assert!(list(&r.app, Some("peer:capella")).await.get("sessions").is_some());
}

#[tokio::test]
async fn test_the_list_verb_hides_private_sessions_from_a_peer() {
    let _turn = turn().await;
    let r = rig(|projects, f| {
        let demo = projects.join("awm").join("demo").display().to_string();
        Fake {
            rows: vec![
                json!({"session_id": CHILD_JOB, "project": "awm", "scope": "demo", "cwd": demo}),
                json!({"session_id": "cccc0003", "project": null, "scope": null, "cwd": "/elsewhere"}),
            ],
            pool_status: json!({
                "warm": [PARENT_JOB, "dddd0004"], "want": 1, "daemon_pid": null, "loop": "running",
                "sessions": [{"session_id": PARENT_JOB, "cwd": demo}, {"session_id": "dddd0004", "cwd": "/elsewhere"}],
            }),
            ..f
        }
    });
    mark("awm", &r.projects.join("awm"));
    let peer = list(&r.app, Some("peer:capella")).await;
    assert_eq!(jobs(&peer), ["cccc0003"]);
    let pool: Vec<&str> = peer["pool"]["sessions"].as_array().unwrap().iter().map(|s| s["session_id"].as_str().unwrap()).collect();
    assert_eq!(pool, ["dddd0004"]);
    assert_eq!(peer["pool"]["warm"], json!(["dddd0004"]));
    let local = list(&r.app, None).await;
    assert_eq!(jobs(&local), [CHILD_JOB, "cccc0003"]);
    assert_eq!(local["pool"]["warm"], json!([PARENT_JOB, "dddd0004"]));
}

#[tokio::test]
async fn a_peer_list_hides_a_private_isolated_row() {
    let _turn = turn().await;
    let r = rig(|_, f| f);
    let cwd = r.projects.join("secret").join("s");
    let rec = json!({"claude_session_id": ISO_SID, "name": "boxed", "project": "secret", "scope": "s",
                     "cwd": cwd.display().to_string()});
    r.app.backend.isolated_session(rec, true);
    let names = |out: &Value| -> Vec<String> {
        out["sessions"].as_array().unwrap().iter().filter_map(|s| s["name"].as_str().map(str::to_string)).collect()
    };
    assert_eq!(names(&list(&r.app, Some("peer:capella")).await), ["boxed"]);
    mark("secret", &r.projects.join("secret"));
    assert!(names(&list(&r.app, Some("peer:capella")).await).is_empty(), "the isolated row is filtered too");
    assert_eq!(names(&list(&r.app, None).await), ["boxed"]);
}

#[tokio::test]
async fn a_peer_list_leaves_out_the_transcript() {
    let _turn = turn().await;
    let r = rig(|_, f| Fake { rows: vec![json!({"session_id": CHILD_JOB, "transcript": "/t/made-up.jsonl"})], ..f });
    let peer = list(&r.app, Some("peer:capella")).await;
    assert!(peer["sessions"][0].get("transcript").is_none(), "{peer}");
    assert_eq!(list(&r.app, None).await["sessions"][0]["transcript"], "/t/made-up.jsonl");
}
