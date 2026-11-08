//! The port's board side, against the store directly: the first-start migration of Python's
//! queue.db, the board feed trilium drains (CONTRACT.md b11adebc items 2 to 5), the one-shot
//! vault import, Stale from telemetry, the event hook and the `agent` order's start.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;
use std::sync::{Arc, Mutex};

use awm_plugin_operations::store::{BoardEvent, LiveSession, Open, Seat, Store};
use crate::common::{count, s, Env, TestClock};
use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

/// Python's store.py SCHEMA, as alcor's queue.db holds it before the port's first start.
const PY_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    section TEXT NOT NULL,
    status TEXT,
    peer TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    body_ref TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    parent_task TEXT,
    folded_into TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    done_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_section ON tasks(section, updated);
CREATE INDEX IF NOT EXISTS tasks_done ON tasks(done_at);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    node TEXT,
    name TEXT,
    rc_url TEXT,
    started REAL NOT NULL,
    ended REAL
);
CREATE INDEX IF NOT EXISTS sessions_task ON sessions(task_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS applied_ops (
    op_id TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    author TEXT,
    node TEXT,
    session_id TEXT,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_task ON messages(task_id, seq);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
";

const T0: f64 = 1_800_000_000.0;
const S1: &str = "cc0123456789abcdef0123456789abcdef";
const S3: &str = "cc3333333333333333333333333333333a";
const S4: &str = "cc4444444444444444444444444444444b";

fn sid(n: u32) -> String {
    format!("cc{n:032x}")
}

/// A queue.db as Python left it: a done card, two proposed cards (one a note), a working
/// card with a session, and a card opened after the writer's mark of 7.
fn python_db(path: &Path) {
    let db = Connection::open(path).unwrap();
    db.execute_batch(PY_SCHEMA).unwrap();
    for (id, title, kind, section, status, done_at) in [
        ("T0000001", "Old work", "task", "done", Some("done"), Some(1002.0)),
        ("T0000002", "An idea", "note", "proposed", None, None),
        ("T0000003", "Live work", "task", "working", None, None),
        ("T0000004", "Proposed task", "task", "proposed", None, None),
        ("T0000005", "After the mark", "task", "working", None, None),
    ] {
        db.execute(
            "INSERT INTO tasks(task_id, title, kind, section, status, tags, meta, created, updated, done_at) VALUES (?,?,?,?,?,'[]','{}',1000.0,1001.0,?)",
            params![id, title, kind, section, status, done_at],
        ).unwrap();
    }
    db.execute("INSERT INTO sessions VALUES (?, 'T0000003', 'main', 1, 'mizar', 'live-work', NULL, 1000.0, NULL)", [S3]).unwrap();
    for (task, kind) in [("T0000001", "open"), ("T0000001", "report"), ("T0000002", "open"), ("T0000003", "open"), ("T0000003", "session"),
                         ("T0000004", "open"), ("T0000003", "report"), ("T0000005", "open")] {
        db.execute("INSERT INTO events(task_id, ts, kind, data) VALUES (?, 1000.0, ?, '{}')", params![task, kind]).unwrap();
    }
    db.execute("INSERT INTO messages(task_id, ts, author, body) VALUES ('T0000003', 1000.0, 'tony', 'hello')", []).unwrap();
    db.execute("INSERT INTO applied_ops VALUES ('mizar:1', '{}', 1000.0)", []).unwrap();
    db.execute("INSERT INTO kv VALUES ('writer_mark', '7')", []).unwrap();
}

fn keys(path: &Path) -> Vec<Vec<String>> {
    let db = Connection::open(path).unwrap();
    ["SELECT task_id FROM tasks", "SELECT session_id FROM sessions", "SELECT CAST(id AS TEXT) FROM events",
     "SELECT CAST(seq AS TEXT) FROM messages", "SELECT op_id FROM applied_ops"]
        .iter()
        .map(|q| {
            let mut st = db.prepare(&format!("{q} ORDER BY 1")).unwrap();
            let rows = st.query_map([], |r| r.get(0)).unwrap().map(Result::unwrap).collect();
            rows
        })
        .collect()
}

fn columns(path: &Path, table: &str) -> Vec<String> {
    let db = Connection::open(path).unwrap();
    let mut st = db.prepare(&format!("PRAGMA table_info({table})")).unwrap();
    let cols = st.query_map([], |r| r.get(1)).unwrap().map(Result::unwrap).collect();
    cols
}

fn raw_sections(path: &Path) -> HashMap<String, String> {
    let db = Connection::open(path).unwrap();
    let mut st = db.prepare("SELECT task_id, section FROM tasks").unwrap();
    let rows = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).unwrap().map(Result::unwrap).collect();
    rows
}

fn sections(pairs: &[(&str, &str)]) -> HashMap<String, String> {
    pairs.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect()
}

struct Migrated {
    dir: tempfile::TempDir,
    store: Store,
    clock: TestClock,
}

fn migrated() -> Migrated {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("queue.db");
    python_db(&path);
    let clock = TestClock::at(T0);
    let store = Store::open_with_clock(&path, false, clock.clock()).unwrap();
    Migrated { dir, store, clock }
}

// -- the first-start migration ---------------------------------------------------------------

#[test]
fn the_first_start_migrates_a_python_queue_in_place() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("queue.db");
    python_db(&path);
    let before = keys(&path);
    let st = Store::open(&path, false).unwrap();
    assert_eq!(raw_sections(&path), sections(&[("T0000001", "history"), ("T0000002", "proposed"), ("T0000003", "working"),
                                                ("T0000004", "proposed"), ("T0000005", "working")]));
    let tasks = columns(&path, "tasks");
    assert!(tasks.contains(&"body".into()) && tasks.contains(&"source_note".into()), "{tasks:?}");
    assert!(columns(&path, "sessions").contains(&"job".into()));
    assert_eq!(columns(&path, "ops_notes"), ["kind", "node", "title", "body", "ts"]);
    assert_eq!(st.kv_get("vault_mark").unwrap().as_deref(), Some("7"));
    assert_eq!(st.kv_get("writer_mark").unwrap().as_deref(), Some("7"));
    assert_eq!(keys(&path), before, "no row gains or loses a key");
    let old = st.task_get("T0000001").unwrap();
    assert_eq!((s(&old["status"]), old["done_at"].clone()), ("done".into(), json!(1002.0)));
    let live = st.task_get("T0000003").unwrap();
    assert_eq!((s(&live["sessions"][0]["session_id"]), live["sessions"][0]["job"].clone()), (S3.into(), Value::Null));
    assert_eq!(st.task_messages("T0000003", 0).unwrap()[0]["body"], "hello");
    assert_eq!(st.max_event_id().unwrap(), 8);

    st.kv_set("vault_mark", "9").unwrap();
    drop(st);
    let again = Store::open(&path, false).unwrap();
    assert_eq!(again.kv_get("vault_mark").unwrap().as_deref(), Some("9"), "a second start keeps the mark it has");
    assert_eq!(keys(&path), before);
}

#[test]
fn a_missing_queue_is_refused_unless_created() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("sub/queue.db");
    let e = Store::open(&path, false).err().expect("a missing queue is refused");
    assert!(e.to_string().contains("refusing to start with an empty queue"), "{e}");
    assert!(!path.exists(), "the refusal creates nothing");
    Store::open(&path, true).unwrap();
    assert!(path.exists());
}

#[test]
fn downgrade_sections_renames_back_for_python() {
    let m = migrated();
    assert_eq!(m.store.session_gone(Some(S3), None, "mizar").unwrap(), ["T0000003"]);
    assert_eq!(m.store.task_get("T0000003").unwrap()["section"], "stale");
    assert_eq!(m.store.downgrade_sections().unwrap(), json!({"history_to_done": 1, "stale_to_working": 1}));
    let path = m.dir.path().join("queue.db");
    assert_eq!(raw_sections(&path), sections(&[("T0000001", "done"), ("T0000002", "proposed"), ("T0000003", "working"),
                                                ("T0000004", "proposed"), ("T0000005", "working")]));
}

// -- board_changes ---------------------------------------------------------------------------

#[test]
fn board_changes_pages_by_event_with_one_card_per_task() {
    let (dir, clock) = (tempfile::tempdir().unwrap(), TestClock::at(T0));
    let st = Store::open_with_clock(dir.path().join("q.db"), true, clock.clock()).unwrap();
    let o = |id: &str, sid: Option<&str>| Open { task_id: Some(id.into()), session_id: sid.map(str::to_string), ..Open::default() };
    st.task_open("one", o("T0000001", Some(S1)), None).unwrap();
    st.task_report("T0000001", Some(S1), "blocked", Some("why"), None, None).unwrap();
    st.task_open("two", o("T0000002", None), None).unwrap();
    st.task_message("T0000001", "hi", None, None, None, None).unwrap();
    st.task_report("T0000002", None, "working", None, None, None).unwrap();
    let ids = |v: &Value| v["cards"].as_array().unwrap().iter().map(|c| s(&c["task_id"])).collect::<Vec<_>>();

    let p1 = st.board_changes(0, Some(3)).unwrap();
    assert_eq!((ids(&p1), p1["mark"].clone(), p1["more"].clone()), (vec!["T0000001".to_string()], json!(3), json!(true)));
    assert_eq!(p1["cards"][0]["section"], "needs_you", "a card shows as it is now");
    let p2 = st.board_changes(3, Some(3)).unwrap();
    assert_eq!((ids(&p2), p2["mark"].clone(), p2["more"].clone()), (vec!["T0000002".to_string(), "T0000001".to_string()], json!(6), json!(false)));
    let p3 = st.board_changes(6, None).unwrap();
    assert_eq!(p3, json!({"cards": [], "mark": 6, "more": false}));
    let one = st.board_changes(0, Some(0)).unwrap();
    assert_eq!((one["mark"].clone(), one["more"].clone()), (json!(1), json!(true)), "a limit under 1 reads 1");
    let all = st.board_changes(0, Some(100_000)).unwrap();
    assert_eq!((ids(&all), all["mark"].clone(), all["more"].clone()), (vec!["T0000001".to_string(), "T0000002".to_string()], json!(6), json!(false)));
}

const CARD_KEYS: [&str; 15] = ["task_id", "title", "kind", "section", "status", "peer", "tags", "meta", "created", "updated", "done_at",
                               "parent_task", "sessions", "source_note", "approvals"];
const SESSION_KEYS: [&str; 9] = ["session_id", "job", "name", "role", "active", "node", "rc_url", "started", "ended"];

fn key_set(v: &Value) -> BTreeSet<String> {
    v.as_object().unwrap().keys().cloned().collect()
}

fn names(k: &[&str]) -> BTreeSet<String> {
    k.iter().map(|x| x.to_string()).collect()
}

/// A fixed board: every shape the feed carries, at fixed times, with synthetic ids.
async fn fixture_board() -> Value {
    let clock = TestClock::at(T0);
    let env = Env::with_clock(clock.clock());
    let (st, ap) = (env.store.clone(), env.ap.clone());
    let a2 = ap.clone();
    st.set_approvals(Arc::new(move |t: &str| a2.approvals_of(t)));
    let at = |n: f64| clock.set(T0 + n);

    at(1.0);
    st.task_open("Fix the tunnel", Open {
        task_id: Some("T0000001".into()), session_id: Some(S1.into()), node: Some("mizar".into()), name: Some("fix-tunnel".into()),
        rc_url: Some("https://rc.example.test/s1".into()), job: Some("job-a".into()), tags: Some(vec!["infra".into()]), ..Open::default()
    }, None).unwrap();
    at(2.0);
    st.task_detail("T0000001", Some("# Fix the tunnel\n\nsteps"), Some(&[json!({"id": "1", "subject": "port hooks", "status": "in_progress"})]), None).unwrap();
    at(3.0);
    st.task_message("T0000001", "deploy passed", Some("fix-tunnel"), Some("mizar"), Some(S1), None).unwrap();
    at(4.0);
    ap.approval_request("vault_hold", Some("mizar"), "checks tripped", 15, Value::Null, Some(S1), Some("q000000000001")).await.unwrap();
    at(5.0);
    st.task_open("An idea", Open { task_id: Some("T0000002".into()), kind: Some("note".into()), section: Some("needs_you".into()), ..Open::default() }, None).unwrap();
    at(6.0);
    st.kv_set("vault_mark", &st.max_event_id().unwrap().to_string()).unwrap();
    let cards = [json!({"task_id": "T0000001", "section": "needs_you", "kind": "task", "title": "Fix the tunnel"}),
                 json!({"task_id": "T0000002", "section": "needs_you", "kind": "note", "title": "An idea", "body": "The idea, in full."})];
    assert_eq!(st.board_import(&cards, &[], &json!(false)).unwrap()["ok"], true);
    at(7.0);
    st.task_open("Side quest", Open { task_id: Some("T0000003".into()), session_id: Some(S3.into()), node: Some("mizar".into()), job: Some("job-c".into()), ..Open::default() }, None).unwrap();
    at(8.0);
    st.task_join("T0000001", S3, Seat { node: Some("mizar".into()), job: Some("job-c".into()), ..Seat::default() }, None).unwrap();
    at(9.0);
    st.task_open("Old work", Open {
        task_id: Some("T0000004".into()), session_id: Some(S4.into()), node: Some("lyra".into()), name: Some("old-work".into()), job: Some("job-d".into()), ..Open::default()
    }, None).unwrap();
    at(10.0);
    st.session_gone(Some(S4), None, "lyra").unwrap();
    at(11.0);
    st.card_close("T0000004").unwrap();
    at(12.0);
    st.task_open("Agent on mizar from note n0000005", Open {
        task_id: Some("T0000005".into()), peer: Some("mizar".into()), section: Some("needs_you".into()), source_note: Some("n0000005".into()),
        meta: Some(Map::from_iter([("from".to_string(), json!("vault"))])), ..Open::default()
    }, None).unwrap();
    st.board_changes(0, None).unwrap()
}

#[tokio::test]
async fn board_changes_matches_the_shared_fixture() {
    let got = fixture_board().await;
    for card in got["cards"].as_array().unwrap() {
        let mut want = names(&CARD_KEYS);
        let id = s(&card["task_id"]);
        if id == "T0000002" {
            want.insert("body".into());
        }
        if id == "T0000001" {
            want.insert("messages".into());
        }
        if id == "T0000003" {
            want.insert("folded_into".into());
        }
        assert_eq!(key_set(card), want, "{id}");
        assert_eq!(key_set(&card["meta"]), names(&["reason", "plan", "todos"]), "{id}");
        for ses in card["sessions"].as_array().unwrap() {
            assert_eq!(key_set(ses), names(&SESSION_KEYS), "{id}");
        }
    }
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/board_changes.json");
    let want: Option<Value> = std::fs::read_to_string(&path).ok().map(|t| serde_json::from_str(&t).unwrap());
    if want.as_ref() != Some(&got) {
        let out = Path::new(env!("CARGO_TARGET_TMPDIR")).join("board_changes.json");
        std::fs::write(&out, serde_json::to_string_pretty(&got).unwrap() + "\n").unwrap();
        panic!("board_changes differs from {}: the actual feed is in {}", path.display(), out.display());
    }
}

// -- board_import ----------------------------------------------------------------------------

fn card(id: &str, section: &str, kind: &str, body: Option<&str>) -> Value {
    let mut c = json!({"task_id": id, "section": section, "kind": kind, "title": id});
    if let Some(b) = body {
        c["body"] = json!(b);
    }
    c
}

fn vault() -> Vec<Value> {
    vec![card("T0000001", "history", "task", None), card("T0000002", "proposed", "note", Some("The idea, in full.")),
         card("T0000003", "working", "task", None), card("T0000004", "proposed", "task", None)]
}

/// What an import may change, to show a refused one changed nothing.
fn written(m: &Migrated) -> (i64, Option<String>, HashMap<String, String>, Value, i64) {
    let path = m.dir.path().join("queue.db");
    (count(&m.store, "events"), m.store.kv_get("board_import").unwrap(), raw_sections(&path),
     m.store.task_get("T0000002").unwrap()["body"].clone(), count(&m.store, "ops_notes"))
}

#[test]
fn an_exact_import_moves_proposed_cards_to_history_and_lands_note_bodies() {
    let m = migrated();
    m.clock.set(T0 + 10.0);
    m.store.ops_post("vpn", "mizar", "VPN newer", "the tunnel is back", None).unwrap();
    let notes = [json!({"kind": "vpn", "node": "mizar", "title": "VPN older", "body": "down"}),
                 json!({"kind": "disk", "node": "alcor", "title": "Disk", "body": "91%"}),
                 json!({"kind": "", "node": "alcor", "title": "no kind", "body": "x"})];
    m.clock.set(T0 + 20.0);
    let out = m.store.board_import(&vault(), &notes, &json!(false)).unwrap();
    assert_eq!(out, json!({"ok": true, "moved": 2, "notes": 1}));
    let path = m.dir.path().join("queue.db");
    assert_eq!(raw_sections(&path), sections(&[("T0000001", "history"), ("T0000002", "history"), ("T0000003", "working"),
                                                ("T0000004", "history"), ("T0000005", "working")]));
    let idea = m.store.task_get("T0000002").unwrap();
    assert_eq!((idea["body"].clone(), idea["done_at"].clone(), idea["kind"].clone()), (json!("The idea, in full."), json!(T0 + 20.0), json!("note")));
    assert_eq!(m.store.task_get("T0000004").unwrap()["body"], Value::Null, "only a note's body lands");
    let listed = m.store.ops_list().unwrap();
    let titles: Vec<(String, String)> = listed["notes"].as_array().unwrap().iter().map(|n| (s(&n["kind"]), s(&n["title"]))).collect();
    assert_eq!(titles, [("disk".to_string(), "Disk".to_string()), ("vpn".to_string(), "VPN newer".to_string())], "an import never overwrites a newer notice");
    let record: Value = serde_json::from_str(&m.store.kv_get("board_import").unwrap().unwrap()).unwrap();
    assert_eq!(record, json!({"ts": T0 + 20.0, "cards": 4, "moved": 2, "notes": 1, "gap": []}));
}

#[test]
fn a_replay_after_success_answers_ok_and_writes_nothing() {
    let m = migrated();
    m.store.board_import(&vault(), &[], &json!(false)).unwrap();
    let before = written(&m);
    let again = m.store.board_import(&[card("T0000009", "proposed", "note", None)], &[json!({"kind": "k", "node": "n", "title": "t", "body": "b"})], &json!(false)).unwrap();
    assert_eq!(again, json!({"ok": true, "replay": true}));
    assert_eq!(written(&m), before);
}

fn refused(m: &Migrated, cards: &[Value], gap: Value) -> Value {
    let before = written(m);
    let out = m.store.board_import(cards, &[json!({"kind": "disk", "node": "alcor", "title": "Disk", "body": "91%"})], &gap).unwrap();
    assert_eq!(out["ok"], false, "{out}");
    assert_eq!(written(m), before, "a refused import writes nothing");
    out
}

#[test]
fn an_import_that_does_not_match_the_queue_is_refused() {
    let m = migrated();
    let out = refused(&m, &vault()[..3], json!(false));
    assert_eq!(out, json!({"ok": false, "missing": ["T0000004"], "extra": [], "no_body": [], "duplicates": false, "expected": 4, "given": 3}));
    let mut cards = vault();
    cards.push(card("T0000005", "working", "task", None));
    cards.push(card("T0000009", "proposed", "task", None));
    let out = refused(&m, &cards, json!(false));
    assert_eq!((out["missing"].clone(), out["extra"].clone()), (json!([]), json!(["T0000005", "T0000009"])), "a card after the mark is extra");
    let mut cards = vault();
    cards.push(card("T0000001", "history", "task", None));
    let out = refused(&m, &cards, json!(true));
    assert_eq!((out["duplicates"].clone(), out["given"].clone()), (json!(true), json!(5)), "accept_gap does not excuse duplicates");
    let mut cards = vault();
    cards[1] = card("T0000002", "proposed", "note", None);
    assert_eq!(refused(&m, &cards, json!(true))["no_body"], json!(["T0000002"]));
}

#[test]
fn accept_gap_must_name_the_gap_exactly() {
    let m = migrated();
    let mut cards = vault()[..3].to_vec();
    cards.push(card("T0000005", "working", "task", None));
    refused(&m, &cards, json!(["T0000004"]));
    refused(&m, &cards, json!(["T0000004", "T0000005", "T0000006"]));
    refused(&m, &cards, json!("yes"));
    let out = m.store.board_import(&cards, &[], &json!(["T0000005", "T0000004"])).unwrap();
    assert_eq!(out, json!({"ok": true, "moved": 1, "notes": 0}));
    assert_eq!(m.store.task_get("T0000004").unwrap()["section"], "proposed", "a card left out stays where it is");
    let record: Value = serde_json::from_str(&m.store.kv_get("board_import").unwrap().unwrap()).unwrap();
    assert_eq!(record["gap"], json!(["T0000004", "T0000005"]));
}

// -- Stale (R61 item 4) ----------------------------------------------------------------------

fn seated(st: &Store, id: &str, sid: &str, node: &str, job: &str) {
    st.task_open(id, Open { task_id: Some(id.into()), session_id: Some(sid.into()), node: Some(node.into()), job: Some(job.into()), ..Open::default() }, None).unwrap();
}

fn at(st: &Store, id: &str) -> (String, Value) {
    let c = st.task_get(id).unwrap();
    (s(&c["section"]), c["status"].clone())
}

fn fresh() -> (tempfile::TempDir, Store) {
    let dir = tempfile::tempdir().unwrap();
    let st = Store::open(dir.path().join("q.db"), true).unwrap();
    (dir, st)
}

#[test]
fn a_gone_session_leaves_its_lone_card_stale() {
    let (_d, st) = fresh();
    seated(&st, "T0000001", &sid(1), "mizar", "job-a");
    seated(&st, "T0000002", &sid(2), "lyra", "job-b");
    st.task_report("T0000002", Some(&sid(2)), "blocked", Some("a key"), None, None).unwrap();
    assert_eq!(st.session_gone(Some(&sid(1)), None, "mizar").unwrap(), ["T0000001"]);
    assert_eq!(at(&st, "T0000001"), ("stale".into(), Value::Null));
    assert_eq!(st.task_sessions("T0000001").unwrap()[0]["active"], false);
    assert!(st.session_gone(None, Some("job-b"), "mizar").unwrap().is_empty(), "a job is matched on its own node");
    assert_eq!(st.session_gone(None, Some("job-b"), "lyra").unwrap(), ["T0000002"]);
    assert_eq!(at(&st, "T0000002"), ("stale".into(), json!("blocked")), "a Needs-you card goes Stale too");
    assert!(st.session_gone(Some(&sid(9)), None, "mizar").unwrap().is_empty());
}

#[test]
fn a_card_with_another_live_session_stays() {
    let (_d, st) = fresh();
    seated(&st, "T0000001", &sid(1), "mizar", "job-a");
    st.task_attach("T0000001", &sid(2), "worker", Seat { node: Some("lyra".into()), ..Seat::default() }, None).unwrap();
    assert!(st.session_gone(Some(&sid(1)), None, "mizar").unwrap().is_empty());
    assert_eq!(at(&st, "T0000001").0, "working");
    seated(&st, "T0000002", &sid(3), "mizar", "job-c");
    st.card_close("T0000002").unwrap();
    assert!(st.session_gone(Some(&sid(3)), None, "mizar").unwrap().is_empty(), "a History card stays in History");
    assert_eq!(at(&st, "T0000002").0, "history");
}

#[test]
fn a_live_session_brings_its_card_back_and_close_files_a_stale_one() {
    let (_d, st) = fresh();
    seated(&st, "T0000001", &sid(1), "mizar", "job-a");
    seated(&st, "T0000002", &sid(2), "mizar", "job-b");
    st.session_gone(Some(&sid(1)), None, "mizar").unwrap();
    st.session_gone(Some(&sid(2)), None, "mizar").unwrap();
    assert_eq!(st.session_live(Some(&sid(1)), None, "mizar").unwrap(), ["T0000001"]);
    assert_eq!(at(&st, "T0000001"), ("working".into(), Value::Null));
    assert_eq!(st.task_sessions("T0000001").unwrap()[0]["active"], true);
    assert!(st.session_live(Some(&sid(1)), None, "mizar").unwrap().is_empty(), "a Working card stays");
    assert_eq!(st.card_close("T0000002").unwrap()["section"], "history");
    seated(&st, "T0000003", &sid(3), "lyra", "job-c");
    st.session_gone(None, Some("job-c"), "lyra").unwrap();
    assert_eq!(st.session_live(None, Some("job-c"), "lyra").unwrap(), ["T0000003"]);
    seated(&st, "T0000004", &sid(4), "lyra", "job-d");
    st.session_gone(Some(&sid(4)), None, "lyra").unwrap();
    assert_eq!(st.task_claim("T0000004", &sid(5), Seat::default(), None).unwrap()["section"], "working", "a claim takes a Stale card");
}

#[test]
fn reconcile_follows_the_roster_on_the_nodes_it_lists() {
    let (_d, st) = fresh();
    seated(&st, "T0000001", &sid(1), "mizar", "job-a");
    seated(&st, "T0000002", &sid(2), "mizar", "job-b");
    seated(&st, "T0000003", &sid(3), "lyra", "job-c");
    seated(&st, "T0000004", &sid(4), "mizar", "job-d");
    st.session_gone(Some(&sid(2)), None, "mizar").unwrap();
    let roster = HashMap::from([("mizar".to_string(), vec![
        LiveSession { session_id: Some(sid(2)), job: Some("job-b".into()) },
        LiveSession { session_id: None, job: Some("job-d".into()) },
    ])]);
    let out = st.reconcile(&roster).unwrap();
    assert_eq!(out, json!({"stale": ["T0000001"], "working": ["T0000002"]}));
    assert_eq!(at(&st, "T0000003").0, "working", "a node the roster does not list is left alone");
    assert_eq!(at(&st, "T0000004").0, "working", "matched on its job");
    let empty = HashMap::from([("lyra".to_string(), vec![])]);
    assert_eq!(st.reconcile(&empty).unwrap(), json!({"stale": ["T0000003"], "working": []}), "a listed node with no sessions ends them all");
}

// -- the event hook --------------------------------------------------------------------------

#[test]
fn the_event_hook_fires_once_per_event_after_commit() {
    let dir = tempfile::tempdir().unwrap();
    let st = Arc::new(Store::open(dir.path().join("q.db"), true).unwrap());
    let seen: Arc<Mutex<Vec<(BoardEvent, i64)>>> = Arc::new(Mutex::new(vec![]));
    let (weak, rec) = (Arc::downgrade(&st), seen.clone());
    st.set_on_event(Arc::new(move |e: &BoardEvent| {
        let committed = weak.upgrade().unwrap().max_event_id().unwrap();
        rec.lock().unwrap().push((e.clone(), committed));
    }));
    let o = || Open { task_id: Some("T0000001".into()), session_id: Some(S1.into()), ..Open::default() };
    st.task_open("x", o(), Some("mizar:1")).unwrap();
    let got = seen.lock().unwrap().clone();
    assert_eq!(got.iter().map(|g| (g.0.task_id.as_str(), g.0.seq)).collect::<Vec<_>>(), [("T0000001", 1), ("T0000001", 2)]);
    assert!(got.iter().all(|g| g.1 == 2), "each announcement follows the commit of its whole write");
    st.task_open("x", o(), Some("mizar:1")).unwrap();
    assert!(st.task_open("x", o(), None).is_err());
    assert!(st.task_report("nosuchid", None, "done", None, None, None).is_err());
    assert!(st.task_report("T0000001", None, "dragged", None, None, None).is_err());
    assert_eq!(seen.lock().unwrap().len(), 2, "a replayed or refused write announces nothing");
    st.task_report("T0000001", Some(S1), "blocked", None, None, None).unwrap();
    st.ops_post("vpn", "mizar", "t", "b", None).unwrap();
    let seqs: Vec<i64> = seen.lock().unwrap().iter().map(|g| g.0.seq).collect();
    assert_eq!(seqs, [1, 2, 3], "seq rises, and an Ops notice is no card event");
}
