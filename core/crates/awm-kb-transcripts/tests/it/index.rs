//! The archive's index follows the archive: a sweep indexes what it archived, a prune forgets
//! what it deleted, and a start reconciles the rest. Synthetic data only.

use std::sync::Arc;

use awm_index::grammar::{parse_source, parse_text};
use awm_index::sql::Veil;
use awm_kb_source::{Ask, Source};
use awm_kb_transcripts::source::Transcripts;
use crate::common::*;
use serde_json::{json, Value};

async fn hits(r: &Rig, words: &str) -> Vec<String> {
    let out = r.app.query(&json!({"query": words}), None).await.unwrap();
    let mut v: Vec<String> = out["hits"].as_array().unwrap().iter().map(|h| h["id"].as_str().unwrap().to_string()).collect();
    v.sort();
    v
}

#[tokio::test]
async fn a_sweep_indexes_what_it_archived() {
    let r = rig();
    r.start().await;
    let p = r.projects().join("proj");
    write(&p.join("sess.jsonl"), &msgs(&["swept words here", "second line"]), 30.0);
    write(&p.join("fresh.jsonl"), &msgs(&["swept words, but live"]), 0.0);
    let out = r.app.sweep(&json!({}), None, "manual").await.unwrap();
    assert_eq!(out["sessions"], 1);
    assert_eq!(hits(&r, "swept words").await, ["fresh:1", "sess:1"], "the live session is caught up by the query");
    let rec = r.app.ix().unwrap().record("proj", "sess").unwrap();
    assert_eq!((rec.rows, rec.lines, rec.error), (2, 2, None));
    let dry = r.app.sweep(&json!({"dry_run": true}), None, "manual").await.unwrap();
    assert_eq!(dry["sessions"], 0);
}

#[tokio::test]
async fn a_prune_forgets_what_it_deleted_and_a_restore_keeps_it() {
    let r = rig();
    write_gz(&r.archive().join("proj/old.jsonl.gz"), &msgs(&["pruned words"]), 400.0);
    write_gz(&r.archive().join("proj/new.jsonl.gz"), &msgs(&["kept words"]), 30.0);
    r.indexed().await;
    assert_eq!(hits(&r, "words").await, ["new:1", "old:1"]);
    r.app.prune(&json!({"days": 365}), None).await.unwrap();
    assert_eq!(hits(&r, "words").await, ["new:1", "old:1"], "a dry run deletes nothing");
    let out = r.app.prune(&json!({"days": 365, "dry_run": false}), None).await.unwrap();
    assert_eq!(out["sessions"], 1);
    assert_eq!(hits(&r, "words").await, ["new:1"]);
    assert!(r.app.ix().unwrap().record("proj", "old").is_none());
    // A restore copies the session out and keeps the archived copy, so its rows stay.
    r.app.restore(&json!({"project": "proj", "session": "new"}), None).await.unwrap();
    assert!(r.archive().join("proj/new.jsonl.gz").exists());
    assert_eq!(hits(&r, "kept").await, ["new:1"]);
}

#[tokio::test]
async fn a_reconcile_indexes_new_and_changed_sessions_and_forgets_gone_ones() {
    let r = rig();
    write_gz(&r.archive().join("p/same.jsonl.gz"), &msgs(&["steady"]), 30.0);
    write_gz(&r.archive().join("p/edit.jsonl.gz"), &msgs(&["before"]), 30.0);
    write_gz(&r.archive().join("p/gone.jsonl.gz"), &msgs(&["vanishing"]), 30.0);
    let first = r.indexed().await;
    assert_eq!((first["sessions"].clone(), first["rows"].clone(), first["forgot"].clone()), (json!(3), json!(3), json!(0)));
    let again = r.reconcile().await;
    assert_eq!((again["sessions"].clone(), again["forgot"].clone()), (json!(0), json!(0)), "nothing changed, nothing done");

    write_gz(&r.archive().join("p/edit.jsonl.gz"), &msgs(&["after", "and more"]), 20.0);
    std::fs::remove_file(r.archive().join("p/gone.jsonl.gz")).unwrap();
    write_gz(&r.archive().join("q/new.jsonl.gz"), &msgs(&["arrived"]), 30.0);
    let out = r.reconcile().await;
    assert_eq!((out["sessions"].clone(), out["rows"].clone(), out["forgot"].clone(), out["rows_forgot"].clone()), (json!(2), json!(3), json!(1), json!(1)));
    assert!(hits(&r, "before").await.is_empty());
    assert_eq!(hits(&r, "after").await, ["edit:1"]);
    assert_eq!(hits(&r, "more").await, ["edit:2"]);
    assert!(hits(&r, "vanishing").await.is_empty());
    assert_eq!(hits(&r, "arrived").await, ["new:1"]);
    assert_eq!(hits(&r, "steady").await, ["same:1"]);

    // A reopened index keeps its records, so the next start does nothing.
    let lance = r.app.lance.clone();
    let reopened = awm_kb_transcripts::index::TxIndex::open(&lance).await.unwrap();
    assert_eq!(reopened.records().len(), 3);
    let st = r.app.ix().unwrap().status().await;
    assert_eq!((st["rows"].clone(), st["sessions"].clone(), st["reconcile"]["indexed"].clone(), st["reconcile"]["total"].clone()), (json!(4), json!(3), json!(2), json!(2)));
}

fn ask(text: Value, source: Value, veil: Veil, max: usize) -> Ask {
    let fields = awm_kb_transcripts::index::spec().fields;
    Ask { text: Some(parse_text(&text).unwrap()), source: parse_source(&source, &fields, "transcripts", "query.sources.transcripts").unwrap(), max, vector: None, veil, session: None }
}

fn exact(out: &Value) -> Vec<String> {
    let mut v: Vec<String> = out["exact"].as_array().unwrap().iter().map(|h| h["id"].as_str().unwrap().to_string()).collect();
    v.sort();
    v
}

/// The source search asks in-process: the full-text index, with no vectors.
#[tokio::test]
async fn the_source_answers_from_the_index() {
    let r = rig();
    let line = |ts: &str, text: &str| format!("{}\n", json!({"timestamp": ts, "message": {"content": text}}));
    write_gz(&r.archive().join("-home-a/s1.jsonl.gz"), &(line("2025-05-01T00:00:00Z", "the orange kitchen sink") + &line("2025-05-02T00:00:00Z", "kitchen tiles")), 30.0);
    write_gz(&r.archive().join("-home-b/s2.jsonl.gz"), &line("2026-01-01T00:00:00Z", "a sink in the kitchen"), 30.0);
    r.indexed().await;
    let src = Transcripts::new(Arc::new(r.app));
    assert_eq!((src.name(), src.default(), src.takes_vectors()), ("transcripts", false, false));
    assert_eq!(src.fields().1, ["title", "ts"]);

    let out = src.query(ask(json!({"$contains": "kitchen sink"}), json!({}), Veil::None, 10)).await.unwrap();
    assert_eq!(exact(&out), ["s1:1"]);
    assert_eq!(out["exact"][0], json!({"id": "s1:1", "title": "the orange kitchen sink", "ts": "2025-05-01T00:00:00Z"}));
    let out = src.query(ask(json!({"$about": "kitchen sink"}), json!({}), Veil::None, 10)).await.unwrap();
    assert_eq!(exact(&out), ["s1:1", "s1:2", "s2:1"]);
    let out = src.query(ask(json!({"$contains": "kitchen"}), json!({"project": "-home-b"}), Veil::None, 10)).await.unwrap();
    assert_eq!(exact(&out), ["s2:1"]);
    let out = src.query(ask(json!({"$contains": "kitchen"}), json!({"ts": {"$lt": "2025-05-02"}}), Veil::None, 10)).await.unwrap();
    assert_eq!(exact(&out), ["s1:1"]);
    let out = src.query(ask(json!("kitchen sink"), json!({}), Veil::None, 10)).await.unwrap();
    assert!(out.get("related").is_some(), "the string form answers in two sections");

    let doc = src.get("s1:2", &Veil::None, None).await.unwrap().unwrap();
    assert_eq!((doc["text"].clone(), doc["title"].clone(), doc["session"].clone(), doc["line"].clone(), doc["project"].clone()), (json!("kitchen tiles"), json!("kitchen tiles"), json!("s1"), json!(2), json!(["-home-a"])));
    assert!(src.get("s9:1", &Veil::None, None).await.unwrap().is_none());
    // Marks unreadable: a peer sees nothing.
    assert!(src.get("s1:2", &Veil::Unreadable, None).await.unwrap().is_none());
    let out = src.query(ask(json!({"$contains": "kitchen"}), json!({}), Veil::Unreadable, 10)).await.unwrap();
    assert_eq!(out["exact"], json!([]));
    let st = src.status().await;
    assert_eq!((st["rows"].clone(), st["sessions"].clone()), (json!(3), json!(2)));
}

#[tokio::test]
async fn a_query_indexes_the_complete_lines_a_live_session_gained() {
    let r = rig();
    r.indexed().await;
    let path = r.projects().join("proj/live.jsonl");
    write(&path, &(msgs(&["first live turn"]) + &msg("half written")), 0.0);
    assert_eq!(hits(&r, "live turn").await, ["live:1"]);
    assert!(hits(&r, "half written").await.is_empty(), "a line still being written waits");
    let mut f = std::fs::OpenOptions::new().append(true).open(&path).unwrap();
    std::io::Write::write_all(&mut f, format!("\n{}", msgs(&["second live turn"])).as_bytes()).unwrap();
    drop(f);
    assert_eq!(hits(&r, "live turn").await, ["live:1", "live:3"]);
    assert_eq!(hits(&r, "half written").await, ["live:2"]);
    let rec = r.app.ix().unwrap().record("proj", "live").unwrap();
    assert_eq!((rec.rows, rec.lines), (3, 3));
    r.reconcile().await;
    assert!(r.app.ix().unwrap().record("proj", "live").is_some(), "a reconcile keeps a live session's record");
}
