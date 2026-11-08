//! Private marks: a peer never sees a marked project's sessions through any transcripts verb.
//! Ported from `test_privacy.py`. The marks live in one AWM_DIR per process, so the cases run
//! in order in one test, the broken-marks case last.

use std::collections::{BTreeSet, HashSet};
use std::path::Path;
use std::sync::Arc;

use awm_index::grammar::{parse_source, parse_text};
use awm_index::sql::{Mark, Veil};
use awm_kb_source::Ask;
use awm_kb_source::Source;
use awm_kb_transcripts::source::Transcripts;

use awm_plugin_sdk::privacy;
use awm_kb_transcripts::{sweep, App};
use crate::common::*;
use serde_json::{json, Value};

const PEER: Option<&str> = Some("peer:altair");
const LOCAL: Option<&str> = None;
const USER: Option<&str> = Some("user:tony");

struct Slugs {
    root: std::path::PathBuf,
    marked: String,
    nested: String,
    sibling: String,
    open: String,
}

impl Slugs {
    fn all(&self) -> BTreeSet<String> {
        [&self.marked, &self.nested, &self.sibling, &self.open].into_iter().cloned().collect()
    }
    fn visible(&self) -> BTreeSet<String> {
        [&self.sibling, &self.open].into_iter().cloned().collect()
    }
}

/// Four project slugs with one aged session each, live and archived and indexed.
async fn tree(s: &Slugs) -> Rig {
    let r = rig();
    for name in s.all() {
        let sid = format!("s-{}", &name[name.len() - 6..]);
        write(&r.projects().join(&name).join(format!("{sid}.jsonl")), &msgs(&["the deep kitchen sink"]), 30.0);
        write_gz(&r.archive().join(&name).join(format!("{sid}.jsonl.gz")), &msgs(&["the deep kitchen sink"]), 30.0);
    }
    r.indexed().await;
    r
}

fn projects(out: &Value) -> BTreeSet<String> {
    out["hits"].as_array().unwrap().iter().map(|h| h["project"].as_str().unwrap().to_string()).collect()
}

async fn call(app: &App, verb: &str, args: Value, as_: Option<&str>) -> Result<Value, awm_kb_transcripts::Fail> {
    match verb {
        "search" => app.search(&args, as_).await,
        "query" => app.query(&args, as_).await,
        "status" => app.status(&args, as_).await,
        "restore" => app.restore(&args, as_).await,
        "sweep" => app.sweep(&args, as_, "manual").await,
        "prune" => app.prune(&args, as_).await,
        _ => unreachable!(),
    }
}

/// Archived sessions filed under the workspace folder: one wandered into the marked project.
fn wandered(r: &Rig, root: &Path) -> String {
    let folder = privacy::slug(&root.to_string_lossy());
    let lines = |v: Vec<Value>| v.iter().map(|l| format!("{l}\n")).collect::<String>();
    let home = root.to_string_lossy().to_string();
    let m = |text: &str| json!({"content": text});
    write_gz(&r.archive().join(&folder).join("a-wander.jsonl.gz"), &lines(vec![
        json!({"cwd": home, "message": m("kitchen sink before")}),
        json!({"message": m("kitchen sink with no cwd")}),
        json!({"cwd": root.join("avatar/Avatar").to_string_lossy(), "message": m("inside")}),
        json!({"cwd": home, "message": m("kitchen sink after")}),
    ]), 30.0);
    write_gz(&r.archive().join(&folder).join("s-stay.jsonl.gz"), &lines(vec![
        json!({"cwd": home, "message": m("kitchen sink at home")}),
        json!({"cwd": root.join("avatar2").to_string_lossy(), "message": m("kitchen sink next door")}),
    ]), 30.0);
    folder
}

#[tokio::test]
async fn a_peer_never_sees_what_a_mark_covers() {
    let awm = tempfile::tempdir().unwrap();
    std::env::set_var("AWM_DIR", awm.path().join(".awm"));
    let root = awm.path().join("ws/projects");
    privacy::write_marks(&[json!({"project": "avatar", "scope": null, "path": root.join("avatar").to_string_lossy()})]).unwrap();
    let slug = |p: &Path| privacy::slug(&p.to_string_lossy());
    let s = Slugs { marked: slug(&root.join("avatar")), nested: slug(&root.join("avatar/Avatar/dev")), sibling: slug(&root.join("avatar2")), open: slug(&root.join("awm")), root };

    // A peer search drops the marked sessions and keeps the sibling whose name only starts alike.
    let r = tree(&s).await;
    for (verb, key, q) in [("search", "pattern", "(?i)kitchen"), ("query", "query", "kitchen")] {
        assert_eq!(projects(&call(&r.app, verb, json!({key: q}), PEER).await.unwrap()), s.visible(), "{verb}");
        for as_ in [LOCAL, USER] {
            assert_eq!(projects(&call(&r.app, verb, json!({key: q}), as_).await.unwrap()), s.all(), "{verb} {as_:?}");
        }
        // Naming a marked project gets what an unknown one gets.
        let named = call(&r.app, verb, json!({key: "kitchen", "project": s.nested}), PEER).await.unwrap();
        let unknown = call(&r.app, verb, json!({key: "kitchen", "project": "-no-such-project"}), PEER).await.unwrap();
        assert_eq!((named["hits"].clone(), named["truncated"].clone()), (json!([]), json!(false)));
        assert_eq!((unknown["hits"].clone(), unknown["truncated"].clone()), (json!([]), json!(false)));
        let prefix = call(&r.app, verb, json!({key: "kitchen", "project": s.marked}), PEER).await.unwrap();
        assert_eq!(projects(&prefix), BTreeSet::from([s.sibling.clone()]));
    }

    // A peer's limit fills with hits it may see.
    let out = r.app.query(&json!({"query": "kitchen", "limit": 2}), PEER).await.unwrap();
    assert_eq!((projects(&out), out["truncated"].clone()), (s.visible(), json!(true)));

    // status counts only what a peer may see.
    let peer = r.app.status(&json!({}), PEER).await.unwrap();
    assert_eq!((peer["live"]["sessions"].clone(), peer["archive"]["sessions"].clone(), peer["live"]["past_retention"].clone()), (json!(2), json!(2), json!(2)));
    assert_eq!(r.app.status(&json!({}), LOCAL).await.unwrap()["live"]["sessions"], 4);

    // restore answers a peer as if a marked session were not archived.
    let sid = format!("s-{}", &s.marked[s.marked.len() - 6..]);
    let hidden = r.app.restore(&json!({"project": s.marked, "session": sid}), PEER).await.unwrap_err();
    let missing = r.app.restore(&json!({"project": "-no-such-project", "session": sid}), PEER).await.unwrap_err();
    assert_eq!(hidden.message, format!("{}/{sid} is not in the archive", s.marked));
    assert_eq!(hidden.class, "FileNotFoundError");
    assert!(missing.message.ends_with("is not in the archive"));

    // A sweep that hides the marked projects leaves them alone.
    let out = sweep::archive(&r.app.tree, 7.0, false, &HashSet::new(), Some(&privacy::hidden_slug));
    assert_eq!(out["sessions"], 2);
    let left: BTreeSet<String> = std::fs::read_dir(r.projects()).unwrap().flatten().filter(|e| std::fs::read_dir(e.path()).unwrap().next().is_some()).map(|e| e.file_name().to_string_lossy().into_owned()).collect();
    assert_eq!(left, BTreeSet::from([s.marked.clone(), s.nested.clone()]));

    // A peer never sees a session that entered a marked path, and its limit counts only the rest.
    let r = tree(&s).await;
    let folder = wandered(&r, &s.root);
    r.reconcile().await;
    for (verb, key) in [("search", "pattern"), ("query", "query")] {
        let sessions = |out: Value| -> BTreeSet<String> {
            out["hits"].as_array().unwrap().iter().filter(|h| h["project"] == json!(folder)).map(|h| if verb == "search" { h["session"].as_str().unwrap().to_string() } else { h["id"].as_str().unwrap().split(':').next().unwrap().to_string() }).collect()
        };
        assert_eq!(sessions(call(&r.app, verb, json!({key: "kitchen", "project": folder}), PEER).await.unwrap()), BTreeSet::from(["s-stay".to_string()]));
        for as_ in [LOCAL, USER] {
            assert_eq!(sessions(call(&r.app, verb, json!({key: "kitchen", "project": folder}), as_).await.unwrap()), BTreeSet::from(["a-wander".to_string(), "s-stay".to_string()]));
        }
    }
    let out = r.app.search(&json!({"pattern": "kitchen", "project": folder, "limit": 1}), PEER).await.unwrap();
    assert_eq!(out["hits"].as_array().unwrap().iter().map(|h| h["session"].clone()).collect::<Vec<_>>(), [json!("s-stay")]);
    assert_eq!(out["truncated"], true);
    let e = r.app.restore(&json!({"project": folder, "session": "a-wander"}), PEER).await.unwrap_err();
    assert_eq!(e.message, format!("{folder}/a-wander is not in the archive"));
    assert_eq!(r.app.restore(&json!({"project": folder, "session": "s-stay"}), PEER).await.unwrap()["files"], 1);
    assert_eq!(r.app.restore(&json!({"project": folder, "session": "a-wander"}), LOCAL).await.unwrap()["files"], 1);

    // A peer cannot run the maintenance verbs.
    for (verb, args) in [("sweep", json!({"dry_run": true})), ("prune", json!({"days": 1}))] {
        for as_ in [Some("peer"), PEER] {
            let e = call(&r.app, verb, args.clone(), as_).await.unwrap_err();
            assert_eq!((e.class, e.message), ("RuntimeError", format!("unknown function '{verb}'")));
        }
    }

    // The search source applies the same veil for a peer, and its max fills with visible hits.
    let r = tree(&s).await;
    let folder = wandered(&r, &s.root);
    r.reconcile().await;
    let Rig { dir: _kept, app } = r;
    let src = Transcripts::new(Arc::new(app));
    let fields = awm_kb_transcripts::index::spec().fields;
    let ask = |veil: Veil, max: usize| Ask {
        text: Some(parse_text(&json!({"$contains": "kitchen"})).unwrap()),
        source: parse_source(&json!({}), &fields, "transcripts", "query").unwrap(),
        max,
        vector: None,
        veil,
        session: None,
    };
    let marks = || Veil::Marks { marks: vec![Mark { project: "avatar".into(), scope: None }], tags: None };
    let sid = |slug: &String| format!("s-{}", &slug[slug.len() - 6..]);
    let ids = |out: Value| -> BTreeSet<String> { out["exact"].as_array().unwrap().iter().map(|h| h["id"].as_str().unwrap().to_string()).collect() };
    let mut want: BTreeSet<String> = s.visible().iter().map(|v| format!("{}:1", sid(v))).collect();
    want.extend(["s-stay:1".to_string(), "s-stay:2".to_string()]);
    assert_eq!(ids(src.query(ask(marks(), 50)).await.unwrap()), want, "peer");
    assert_eq!(ids(src.query(ask(Veil::None, 50)).await.unwrap()).len(), want.len() + 2 + 3, "local: the marked two and a-wander's three");
    assert_eq!(src.query(ask(marks(), 2)).await.unwrap()["exact"].as_array().unwrap().len(), 2);
    assert!(src.get("a-wander:1", &marks(), None).await.unwrap().is_none());
    assert!(src.get(&format!("{}:1", sid(&s.marked)), &marks(), None).await.unwrap().is_none());
    assert!(src.get("s-stay:1", &marks(), None).await.unwrap().is_some());
    assert!(src.get("a-wander:1", &Veil::None, None).await.unwrap().is_some());
    let _ = folder;

    // An unreadable marks file hides every session from a peer.
    let r = tree(&s).await;
    std::fs::write(privacy::marks_path(), "{not json").unwrap();
    assert_eq!(r.app.query(&json!({"query": "kitchen"}), PEER).await.unwrap()["hits"], json!([]));
    assert_eq!(r.app.status(&json!({}), PEER).await.unwrap()["live"]["sessions"], 0);
    assert_eq!(r.app.query(&json!({"query": "kitchen"}), LOCAL).await.unwrap()["hits"].as_array().unwrap().len(), 4);
}
