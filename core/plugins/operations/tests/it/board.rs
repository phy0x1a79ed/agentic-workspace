//! The port's board side, against the store directly: the first-start migration of Python's
//! queue.db, the board feed trilium drains (CONTRACT.md b11adebc items 2 to 5), the one-shot
//! vault import, Stale from telemetry, the event hook and the `agent` order's start.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;
use std::sync::{Arc, Mutex};

use awm_plugin_operations::relay::async_trait_free::BoxFut;
use awm_plugin_operations::store::{BoardEvent, LiveSession, Open, Seat, Store};
use awm_plugin_operations::vault_push::{Pusher, Vault};
use crate::common::{s, Env, TestClock};
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
}

fn migrated() -> Migrated {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("queue.db");
    python_db(&path);
    let clock = TestClock::at(T0);
    let store = Store::open_with_clock(&path, false, clock.clock()).unwrap();
    Migrated { dir, store }
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
    assert_eq!(st.kv_get("writer_mark").unwrap().as_deref(), Some("7"));
    assert_eq!(keys(&path), before, "no row gains or loses a key");
    let old = st.task_get("T0000001").unwrap();
    assert_eq!((s(&old["status"]), old["done_at"].clone()), ("done".into(), json!(1002.0)));
    let live = st.task_get("T0000003").unwrap();
    assert_eq!((s(&live["sessions"][0]["session_id"]), live["sessions"][0]["job"].clone()), (S3.into(), Value::Null));
    assert_eq!(st.task_messages("T0000003", 0).unwrap()[0]["body"], "hello");
    assert_eq!(st.max_event_id().unwrap(), 8);

    drop(st);
    Store::open(&path, false).unwrap();
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
    assert_eq!(m.store.session_gone(Some(S3), None, "mizar", false).unwrap(), ["T0000003"]);
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
    st.task_report("T0000001", Some(S1), "blocked", Some("why"), None).unwrap();
    st.task_open("two", o("T0000002", None), None).unwrap();
    st.task_message("T0000001", "hi", None, None, None, None).unwrap();
    st.task_report("T0000002", None, "working", None, None).unwrap();
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
const SESSION_KEYS: [&str; 11] = ["session_id", "job", "name", "role", "active", "node", "rc_url", "started", "ended", "project", "scope"];

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
        job: Some("job-a".into()), tags: Some(vec!["infra".into()]), ..Open::default()
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
    at(7.0);
    st.task_open("Side quest", Open { task_id: Some("T0000003".into()), session_id: Some(S3.into()), node: Some("mizar".into()), job: Some("job-c".into()), ..Open::default() }, None).unwrap();
    at(8.0);
    st.task_join("T0000001", S3, Seat { node: Some("mizar".into()), job: Some("job-c".into()), ..Seat::default() }, None).unwrap();
    at(9.0);
    st.task_open("Old work", Open {
        task_id: Some("T0000004".into()), session_id: Some(S4.into()), node: Some("lyra".into()), name: Some("old-work".into()), job: Some("job-d".into()), ..Open::default()
    }, None).unwrap();
    at(10.0);
    st.session_gone(Some(S4), None, "lyra", false).unwrap();
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
    st.task_report("T0000002", Some(&sid(2)), "blocked", Some("a key"), None).unwrap();
    assert_eq!(st.session_gone(Some(&sid(1)), None, "mizar", false).unwrap(), ["T0000001"]);
    assert_eq!(at(&st, "T0000001"), ("stale".into(), Value::Null));
    assert_eq!(st.task_sessions("T0000001").unwrap()[0]["active"], false);
    assert!(st.session_gone(None, Some("job-b"), "mizar", false).unwrap().is_empty(), "a job is matched on its own node");
    assert_eq!(st.session_gone(None, Some("job-b"), "lyra", false).unwrap(), ["T0000002"]);
    assert_eq!(at(&st, "T0000002"), ("stale".into(), json!("blocked")), "a Needs-you card goes Stale too");
    assert!(st.session_gone(Some(&sid(9)), None, "mizar", false).unwrap().is_empty());
}

#[test]
fn a_card_with_another_live_session_stays() {
    let (_d, st) = fresh();
    seated(&st, "T0000001", &sid(1), "mizar", "job-a");
    st.task_attach("T0000001", &sid(2), "worker", Seat { node: Some("lyra".into()), ..Seat::default() }, None).unwrap();
    assert!(st.session_gone(Some(&sid(1)), None, "mizar", false).unwrap().is_empty());
    assert_eq!(at(&st, "T0000001").0, "working");
    seated(&st, "T0000002", &sid(3), "mizar", "job-c");
    st.card_close("T0000002").unwrap();
    assert!(st.session_gone(Some(&sid(3)), None, "mizar", false).unwrap().is_empty(), "a History card stays in History");
    assert_eq!(at(&st, "T0000002").0, "history");
}

#[test]
fn a_live_session_brings_its_card_back_and_close_files_a_stale_one() {
    let (_d, st) = fresh();
    seated(&st, "T0000001", &sid(1), "mizar", "job-a");
    seated(&st, "T0000002", &sid(2), "mizar", "job-b");
    st.session_gone(Some(&sid(1)), None, "mizar", false).unwrap();
    st.session_gone(Some(&sid(2)), None, "mizar", false).unwrap();
    assert_eq!(st.session_live(Some(&sid(1)), None, "mizar").unwrap(), ["T0000001"]);
    assert_eq!(at(&st, "T0000001"), ("working".into(), Value::Null));
    assert_eq!(st.task_sessions("T0000001").unwrap()[0]["active"], true);
    assert!(st.session_live(Some(&sid(1)), None, "mizar").unwrap().is_empty(), "a Working card stays");
    assert_eq!(st.card_close("T0000002").unwrap()["section"], "history");
    seated(&st, "T0000003", &sid(3), "lyra", "job-c");
    st.session_gone(None, Some("job-c"), "lyra", false).unwrap();
    assert_eq!(st.session_live(None, Some("job-c"), "lyra").unwrap(), ["T0000003"]);
    seated(&st, "T0000004", &sid(4), "lyra", "job-d");
    st.session_gone(Some(&sid(4)), None, "lyra", false).unwrap();
    assert_eq!(st.task_claim("T0000004", &sid(5), Seat::default(), None).unwrap()["section"], "working", "a claim takes a Stale card");
}

#[test]
fn a_main_sessions_state_moves_its_card_and_a_request_outlives_the_turn() {
    let (_d, st) = fresh();
    seated(&st, "T0000001", &sid(1), "mizar", "job-a");
    let state = |s: &str, needs: Option<&str>| st.session_state(Some(&sid(1)), None, "mizar", s, needs).unwrap();
    assert_eq!(state("blocked", Some("pick one")), ["T0000001"]);
    assert_eq!(at(&st, "T0000001"), ("needs_you".into(), json!("blocked")));
    assert_eq!(st.task_get("T0000001").unwrap()["meta"]["reason"], json!("pick one"));
    assert!(state("blocked", Some("pick one")).is_empty(), "the same block writes nothing");
    assert_eq!(state("done", None), ["T0000001"], "a block's own Needs you clears when the turn ends");
    assert_eq!(at(&st, "T0000001"), ("working".into(), Value::Null));
    assert!(state("done", None).is_empty(), "a turn ending done stays in Working");
    st.task_report("T0000001", Some(&sid(1)), "review", Some("look"), None).unwrap();
    assert!(state("done", None).is_empty(), "a request_human outlives the turn after it");
    assert_eq!(state("working", None), ["T0000001"], "Tony's answer starts the session working");
    st.task_attach("T0000001", &sid(2), "worker", Seat { node: Some("mizar".into()), ..Seat::default() }, None).unwrap();
    assert!(st.session_state(Some(&sid(2)), None, "mizar", "blocked", Some("x")).unwrap().is_empty(), "only the main session moves the card");
    st.card_close("T0000001").unwrap();
    assert!(state("blocked", Some("y")).is_empty(), "a closed card stays closed");
    seated(&st, "T0000002", &sid(3), "mizar", "job-c");
    st.session_gone(Some(&sid(3)), None, "mizar", false).unwrap();
    assert_eq!(st.session_state(Some(&sid(3)), None, "mizar", "working", None).unwrap(), ["T0000002"], "a state takes a Stale card");
    let roster = HashMap::from([("mizar".to_string(), vec![
        LiveSession { session_id: Some(sid(3)), state: Some("blocked".into()), needs: Some("why".into()), ..LiveSession::default() },
    ])]);
    assert_eq!(st.reconcile(&roster).unwrap()["state"], json!(["T0000002"]), "reconcile applies the roster's state");
    assert_eq!(st.session_gone(Some(&sid(3)), None, "mizar", true).unwrap(), ["T0000002"]);
    assert_eq!(at(&st, "T0000002"), ("history".into(), json!("done")), "a retire files the card as done");
    let agent = Open { task_id: Some("T0000003".into()), session_id: Some(sid(4)), node: Some("mizar".into()), section: Some("needs_you".into()), ..Open::default() };
    st.task_open("New agent", agent, None).unwrap();
    assert_eq!(st.session_state(Some(&sid(4)), None, "mizar", "done", None).unwrap(), ["T0000003"], "a New agent card leaves Needs you when its first turn ends");
}

#[test]
fn session_details_copy_the_roster_onto_active_rows() {
    let (_d, st) = fresh();
    seated(&st, "T0000001", &sid(1), "mizar", "job-a");
    let live = |link: Option<&str>| HashMap::from([("mizar".to_string(), vec![LiveSession {
        job: Some("job-a".into()), link: link.map(str::to_string), project: Some("awm".into()), scope: Some("dev".into()), ..LiveSession::default() }])]);
    let row = |st: &Store| st.task_get("T0000001").unwrap()["sessions"][0].clone();
    st.session_details(&live(Some("https://rc.example.test/a"))).unwrap();
    assert_eq!((row(&st)["rc_url"].clone(), row(&st)["project"].clone(), row(&st)["scope"].clone()), (json!("https://rc.example.test/a"), json!("awm"), json!("dev")));
    let mark = st.board_changes(0, Some(100)).unwrap()["mark"].clone();
    st.session_details(&live(Some("https://rc.example.test/a"))).unwrap();
    assert_eq!(st.board_changes(0, Some(100)).unwrap()["mark"], mark, "an unchanged roster writes nothing");
    st.session_details(&live(None)).unwrap();
    assert_eq!(row(&st)["rc_url"], json!(null), "Remote Control went off");
}

#[test]
fn an_agent_card_takes_a_generated_title_until_a_name_arrives() {
    let (_d, st) = fresh();
    let t = s(&st.agent_card("mizar", None).unwrap()["task_id"]);
    let title = |st: &Store| s(&st.task_get(&t).unwrap()["title"]);
    st.task_autotitle(&t, "Plan heading", None).unwrap();
    assert_eq!(title(&st), "Plan heading");
    st.task_autotitle(&t, "Later heading", None).unwrap();
    assert_eq!(title(&st), "Plan heading", "a plan heading is final");
    let u = s(&st.agent_card("mizar", None).unwrap()["task_id"]);
    st.task_attach(&u, &sid(9), "main", Seat { node: Some("mizar".into()), ..Seat::default() }, None).unwrap();
    st.session_renamed(Some(&sid(9)), None, "mizar", "real-name").unwrap();
    st.task_autotitle(&u, "A heading", None).unwrap();
    assert_eq!(s(&st.task_get(&u).unwrap()["title"]), "real-name", "a rename wins over a plan heading");
}

#[test]
fn reconcile_follows_the_roster_on_the_nodes_it_lists() {
    let (_d, st) = fresh();
    seated(&st, "T0000001", &sid(1), "mizar", "job-a");
    seated(&st, "T0000002", &sid(2), "mizar", "job-b");
    seated(&st, "T0000003", &sid(3), "lyra", "job-c");
    seated(&st, "T0000004", &sid(4), "mizar", "job-d");
    st.session_gone(Some(&sid(2)), None, "mizar", false).unwrap();
    let roster = HashMap::from([("mizar".to_string(), vec![
        LiveSession { session_id: Some(sid(2)), job: Some("job-b".into()), name: None, ..LiveSession::default() },
        LiveSession { session_id: None, job: Some("job-d".into()), name: None, ..LiveSession::default() },
    ])]);
    let out = st.reconcile(&roster).unwrap();
    assert_eq!(out, json!({"stale": ["T0000001"], "working": ["T0000002"], "state": []}));
    assert_eq!(at(&st, "T0000003").0, "working", "a node the roster does not list is left alone");
    assert_eq!(at(&st, "T0000004").0, "working", "matched on its job");
    let empty = HashMap::from([("lyra".to_string(), vec![])]);
    assert_eq!(st.reconcile(&empty).unwrap(), json!({"stale": ["T0000003"], "working": [], "state": []}), "a listed node with no sessions ends them all");
}

#[test]
fn a_card_takes_the_name_of_its_main_session() {
    let (_d, st) = fresh();
    st.task_open("Agent on mizar", Open { task_id: Some("T0000001".into()), session_id: Some(sid(1)), node: Some("mizar".into()), job: Some("job-a".into()), name: Some("first".into()), ..Open::default() }, None).unwrap();
    let title = || s(&st.task_get("T0000001").unwrap()["title"]);
    assert_eq!(st.session_renamed(Some(&sid(1)), None, "mizar", "new-agent/section").unwrap(), ["T0000001"]);
    assert_eq!(title(), "new-agent/section");
    assert!(st.session_renamed(Some(&sid(1)), None, "mizar", "new-agent/section").unwrap().is_empty(), "the same name is no change");
    let roster = HashMap::from([("mizar".to_string(), vec![LiveSession { session_id: None, job: Some("job-a".into()), name: Some("while-offline".into()), ..LiveSession::default() }])]);
    st.reconcile(&roster).unwrap();
    assert_eq!(title(), "while-offline", "a rename missed while disconnected lands at reconcile");
    st.task_attach("T0000001", &sid(2), "worker", Seat { node: Some("mizar".into()), job: Some("job-b".into()), ..Seat::default() }, None).unwrap();
    assert!(st.session_renamed(Some(&sid(2)), None, "mizar", "a-worker").unwrap().is_empty(), "a worker's name is not the card's");
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
    assert!(st.task_report("nosuchid", None, "done", None, None).is_err());
    assert!(st.task_report("T0000001", None, "dragged", None, None).is_err());
    assert_eq!(seen.lock().unwrap().len(), 2, "a replayed or refused write announces nothing");
    st.task_report("T0000001", Some(S1), "blocked", None, None).unwrap();
    st.ops_post("vpn", "mizar", "t", "b", None).unwrap();
    let seqs: Vec<i64> = seen.lock().unwrap().iter().map(|g| g.0.seq).collect();
    assert_eq!(seqs, [1, 2, 3], "seq rises, and an Ops notice is no card event");
}

// -- the push to the vault -------------------------------------------------------------------

/// The vault's side of `board_apply` and `ops_render`, with a switch to refuse every push.
#[derive(Default)]
struct FakeVault {
    mark: Mutex<i64>,
    cards: Mutex<Vec<String>>,
    notices: Mutex<Vec<Value>>,
    down: Mutex<bool>,
}

impl Vault for FakeVault {
    fn push<'a>(&'a self, verb: &'a str, args: Value) -> BoxFut<'a, Result<Value, String>> {
        Box::pin(async move {
            if *self.down.lock().unwrap() {
                return Err("deneb is down".to_string());
            }
            if verb == "ops_render" {
                self.notices.lock().unwrap().push(args["notices"].clone());
                return Ok(json!({"ok": true}));
            }
            let mut mark = self.mark.lock().unwrap();
            if args.get("base").and_then(Value::as_i64) == Some(*mark) {
                self.cards.lock().unwrap().extend(args["cards"].as_array().unwrap().iter().map(|c| s(&c["task_id"])));
                *mark = args["mark"].as_i64().unwrap();
            }
            Ok(json!({"mark": *mark}))
        })
    }
}

#[tokio::test]
async fn the_vault_catches_up_from_its_own_mark_after_a_failed_push() {
    let dir = tempfile::tempdir().unwrap();
    let st = Arc::new(Store::open(dir.path().join("q.db"), true).unwrap());
    let vault = Arc::new(FakeVault::default());
    let p = Pusher::attach(st.clone(), vault.clone());
    let open = |id: &str| st.task_open("x", Open { task_id: Some(id.into()), session_id: Some(S1.into()), ..Open::default() }, None).unwrap();
    for i in 0..60 {
        open(&format!("T{i:07}"));
    }
    st.ops_post("disk", "alcor", "Disk", "91%", None).unwrap();
    *vault.down.lock().unwrap() = true;
    assert!(p.pass().await.is_err());
    assert!(vault.cards.lock().unwrap().is_empty());
    *vault.down.lock().unwrap() = false;
    p.pass().await.unwrap();
    assert_eq!(vault.cards.lock().unwrap().len(), 60, "every card crosses, over more than one page");
    assert_eq!(*vault.mark.lock().unwrap(), st.max_event_id().unwrap());
    assert_eq!(vault.notices.lock().unwrap().len(), 1);
    p.pass().await.unwrap();
    assert_eq!(vault.notices.lock().unwrap().len(), 1, "unchanged notices are not sent again");
    assert_eq!(vault.cards.lock().unwrap().len(), 60);
}
