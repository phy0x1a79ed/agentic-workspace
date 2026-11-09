//! The task store (fleet.md R106, queue.md R61 to R66), ported from test_store.py with the
//! approved revision: History for done, approvals on the requester's own card, jobs on rows.

use std::collections::HashMap;

use awm_plugin_operations::ids::{from_uuid, is_task_id, mint_task_id};
use awm_plugin_operations::store::{Open, Seat, Store};
use crate::common::{s, TestClock};
use serde_json::{json, Map, Value};

fn fresh() -> (tempfile::TempDir, Store) {
    let d = tempfile::tempdir().unwrap();
    let st = Store::open(d.path().join("q.db"), true).unwrap();
    (d, st)
}

fn clocked(t: f64) -> (tempfile::TempDir, Store, TestClock) {
    let d = tempfile::tempdir().unwrap();
    let c = TestClock::at(t);
    let st = Store::open_with_clock(d.path().join("q.db"), true, c.clock()).unwrap();
    (d, st, c)
}

fn open(st: &Store, title: &str, sid: &str) -> String {
    s(&st.task_open(title, Open { session_id: Some(sid.into()), ..Open::default() }, None).unwrap()["task_id"])
}

fn section(st: &Store, t: &str) -> (String, Value) {
    let c = st.task_get(t).unwrap();
    (s(&c["section"]), c["status"].clone())
}

#[test]
fn task_id_vector_shared_with_sessions() {
    assert_eq!(from_uuid(uuid::Uuid::parse_str("00112233-4455-6677-8899-aabbccddeeff").unwrap()), "ABEiM0RV");
    assert!(is_task_id(&mint_task_id()));
    assert!(!is_task_id("abc/defg"));
}

#[test]
fn open_attach_join_and_sessions() {
    let (_d, st) = fresh();
    let o = Open { session_id: Some("ccA".into()), node: Some("mizar".into()), rc_url: Some("https://rc.example.test/x".into()), ..Open::default() };
    let t = s(&st.task_open("redux queue", o, None).unwrap()["task_id"]);
    st.task_attach(&t, "ccB", "worker", Seat { node: Some("alcor".into()), ..Seat::default() }, None).unwrap();
    let own = open(&st, "side quest", "ccC");
    let j = st.task_join(&t, "ccC", Seat::default(), None).unwrap();
    assert_eq!(j, json!({"task_id": t, "main_session_id": "ccA", "main_node": "mizar", "main_name": null, "folded_task_id": own}));
    assert_eq!(st.task_for_session("ccC").unwrap(), Some(t.clone()));
    assert_eq!(st.task_get(&own).unwrap()["task_id"], json!(t), "a folded card resolves to its target");
    let roles: HashMap<String, String> = st.task_sessions(&t).unwrap().iter().map(|x| (s(&x["session_id"]), s(&x["role"]))).collect();
    assert_eq!(roles, HashMap::from([("ccA".into(), "main".into()), ("ccB".into(), "worker".into()), ("ccC".into(), "joined".into())]));
    assert_eq!(st.task_get(&t).unwrap()["peer"], "mizar");
    st.task_session_end(&t, "ccB", None).unwrap();
    let active: HashMap<String, bool> = st.task_sessions(&t).unwrap().iter().map(|x| (s(&x["session_id"]), x["active"].as_bool().unwrap())).collect();
    assert!(!active["ccB"] && active["ccA"]);
    let listed: Vec<String> = st.task_list(None, None, None, None).unwrap()["cards"].as_array().unwrap().iter().map(|c| s(&c["task_id"])).collect();
    assert!(!listed.contains(&own));
}

#[test]
fn report_moves_sections_and_working_has_no_tag() {
    let (_d, st) = fresh();
    let t = open(&st, "x", "ccA");
    assert_eq!(section(&st, &t), ("working".into(), Value::Null));
    st.task_report(&t, Some("ccA"), "blocked", Some("need a key"), None, None).unwrap();
    assert_eq!(section(&st, &t), ("needs_you".into(), json!("blocked")));
    st.task_report(&t, Some("ccA"), "review", None, None, None).unwrap();
    assert_eq!(st.task_get(&t).unwrap()["status"], "review");
    st.task_report(&t, Some("ccA"), "planned", None, None, None).unwrap();
    assert_eq!(section(&st, &t), ("needs_you".into(), json!("planned")));
    let out = st.task_report(&t, Some("ccA"), "done", None, None, None).unwrap();
    assert_eq!(out, json!({"task_id": t, "section": "history", "status": "done"}));
    let c = st.task_get(&t).unwrap();
    assert_eq!((s(&c["section"]), s(&c["status"])), ("history".into(), "done".into()));
    assert!(c["done_at"].as_f64().is_some());
    assert!(st.task_report(&t, Some("ccA"), "dragged", None, None, None).is_err());
}

#[test]
fn replayed_op_applies_once() {
    let (_d, st) = fresh();
    let o = || Open { session_id: Some("ccA".into()), ..Open::default() };
    let a = st.task_open("x", o(), Some("mizar:1")).unwrap();
    let b = st.task_open("x", o(), Some("mizar:1")).unwrap();
    assert_eq!(a, b);
    assert_eq!(st.task_list(None, None, None, None).unwrap()["cards"].as_array().unwrap().len(), 1);
}

#[test]
fn relay_minted_id_is_kept_and_taken_id_refused() {
    let (_d, st) = fresh();
    let tid = mint_task_id();
    let o = |sid: &str| Open { session_id: Some(sid.into()), task_id: Some(tid.clone()), ..Open::default() };
    assert_eq!(st.task_open("offline", o("ccA"), None).unwrap()["task_id"], json!(tid));
    let e = st.task_open("other", o("ccB"), None).unwrap_err();
    assert!(e.to_string().contains("is taken"), "{e}");
    let bad = Open { task_id: Some("abc/defg".into()), ..Open::default() };
    assert!(st.task_open("bad id", bad, None).is_err());
    assert!(st.task_open("  ", Open::default(), None).is_err(), "a task needs a title");
    let old = Open { section: Some("done".into()), ..Open::default() };
    assert!(st.task_open("old section", old, None).is_err(), "done is History now");
}

#[test]
fn an_old_approval_card_moves_only_through_the_approver() {
    let (_d, st) = fresh();
    let o = Open {
        kind: Some("approval".into()), node: Some("capella".into()), section: Some("needs_you".into()), status: Some("approval".into()),
        meta: Some(Map::from_iter([("kind".to_string(), json!("admin_ssh"))])), ..Open::default()
    };
    let t = s(&st.task_open("admin ssh on capella", o, None).unwrap()["task_id"]);
    let c = st.task_get(&t).unwrap();
    assert_eq!((s(&c["section"]), s(&c["status"]), s(&c["kind"])), ("needs_you".into(), "approval".into(), "approval".into()));
    assert!(st.task_report(&t, None, "done", None, None, None).is_err());
    assert!(st.card_close(&t).is_err());
    assert!(st.task_claim(&t, "ccA", Seat::default(), None).is_err());
    assert!(st.approval_resolve(&t, "q1", "sideways", false).is_err());
    st.approval_resolve(&t, "q1", "expired", false).unwrap();
    assert_eq!(section(&st, &t), ("history".into(), json!("expired")));
}

#[test]
fn an_approval_tags_the_requesters_own_card_until_none_waits() {
    let (_d, st) = fresh();
    let t = open(&st, "fix the tunnel", "ccA");
    let out = st.approval_attach(Some("ccA"), "q1", "vault_hold on alcor: r", None, "vault_hold: checks (15 min)").unwrap();
    assert_eq!(out["task_id"], json!(t), "no card of its own");
    let c = st.task_get(&t).unwrap();
    assert_eq!((s(&c["section"]), s(&c["status"]), s(&c["kind"])), ("needs_you".into(), "approval".into(), "task".into()));
    assert_eq!(c["meta"]["reason"], "vault_hold: checks (15 min)");
    st.approval_attach(Some("ccA"), "q2", "second", None, "second").unwrap();
    st.approval_resolve(&t, "q1", "approved", true).unwrap();
    assert_eq!(section(&st, &t), ("needs_you".into(), json!("approval")), "another request still waits");
    st.approval_resolve(&t, "q2", "denied", false).unwrap();
    let c = st.task_get(&t).unwrap();
    assert_eq!((s(&c["section"]), c["status"].clone()), ("working".into(), Value::Null));
    assert_eq!(c["meta"]["reason"], Value::Null);
    assert_eq!(st.task_list(None, None, None, None).unwrap()["cards"].as_array().unwrap().len(), 1);
}

#[test]
fn history_pages_back_and_search() {
    let (_d, st, clock) = clocked(1000.0);
    for i in 0..7 {
        clock.advance(1.0);
        let t = open(&st, &format!("job {i}"), &format!("cc{i}"));
        st.task_report(&t, Some(&format!("cc{i}")), "done", None, None, None).unwrap();
    }
    let titles = |v: &Value| v["cards"].as_array().unwrap().iter().map(|c| s(&c["title"])).collect::<Vec<_>>();
    let p1 = st.task_list(Some("history"), None, None, Some(3)).unwrap();
    assert_eq!(titles(&p1), ["job 6", "job 5", "job 4"]);
    let p2 = st.task_list(Some("history"), None, p1["next_before"].as_f64(), Some(3)).unwrap();
    assert_eq!(titles(&p2), ["job 3", "job 2", "job 1"]);
    let p3 = st.task_list(Some("history"), None, p2["next_before"].as_f64(), Some(3)).unwrap();
    assert_eq!(titles(&p3), ["job 0"]);
    assert_eq!(p3["next_before"], Value::Null);
    assert_eq!(titles(&st.task_list(None, Some("job 4"), None, None).unwrap()), ["job 4"]);
    assert!(st.task_list(Some("done"), None, None, None).is_err(), "done is History now");
}

#[test]
fn claim_takes_a_needs_you_card_and_refuses_history() {
    let (_d, st) = fresh();
    let o = Open { kind: Some("note".into()), section: Some("needs_you".into()), ..Open::default() };
    let t = s(&st.task_open("idea", o, None).unwrap()["task_id"]);
    let out = st.task_claim(&t, "ccA", Seat::default(), None).unwrap();
    assert_eq!(out, json!({"task_id": t, "title": "idea", "section": "working", "status": null}));
    let c = st.task_get(&t).unwrap();
    assert_eq!((s(&c["kind"]), s(&c["sessions"][0]["role"])), ("task".into(), "main".into()));
    let again = st.task_claim(&t, "ccB", Seat::default(), None).unwrap();
    assert_eq!(again["section"], "working", "a Working card stays where it is");
    st.card_close(&t).unwrap();
    let e = st.task_claim(&t, "ccB", Seat::default(), None).unwrap_err();
    assert!(e.to_string().contains("History"), "{e}");
}

#[test]
fn detail_keeps_the_plan_and_todos_without_moving_the_card() {
    let (_d, st) = fresh();
    let t = open(&st, "x", "ccA");
    let long = "p".repeat(25_000);
    let todos = vec![json!({"id": 1, "subject": "a", "status": "pending", "extra": true}), json!("not an item"), json!({"id": "2", "subject": null, "status": false})];
    st.task_detail(&t, Some(&long), Some(&todos), None).unwrap();
    let c = st.task_get(&t).unwrap();
    assert_eq!(c["meta"]["plan"].as_str().unwrap().chars().count(), 20_000);
    assert_eq!(c["meta"]["todos"], json!([{"id": "1", "subject": "a", "status": "pending"}, {"id": "2", "subject": "", "status": ""}]));
    assert_eq!(c["section"], "working");
    let many: Vec<Value> = (0..250).map(|i| json!({"id": i.to_string(), "subject": "s", "status": "pending"})).collect();
    st.task_detail(&t, None, Some(&many), None).unwrap();
    let c = st.task_get(&t).unwrap();
    assert_eq!(c["meta"]["todos"].as_array().unwrap().len(), 200);
    assert_eq!(c["meta"]["plan"].as_str().unwrap().len(), 20_000, "a todo update keeps the plan");
}

#[test]
fn messages_live_on_the_card() {
    let (_d, st) = fresh();
    let t = open(&st, "x", "ccA");
    assert!(st.task_message(&t, "  ", None, None, None, None).is_err());
    let one = st.task_message(&t, "deploy passed", Some("a service"), None, None, None).unwrap();
    assert_eq!(one, json!({"task_id": t, "seq": 1}));
    st.task_message(&t, &"m".repeat(25_000), Some("other"), Some("lyra"), Some("ccB"), None).unwrap();
    let all = st.task_messages(&t, 0).unwrap();
    assert_eq!(all.len(), 2);
    assert_eq!(all[1]["body"].as_str().unwrap().chars().count(), 20_000);
    assert_eq!((s(&all[1]["node"]), s(&all[1]["session_id"])), ("lyra".into(), "ccB".into()));
    assert_eq!(st.task_messages(&t, 1).unwrap().len(), 1);
    assert_eq!(st.task_get(&t).unwrap()["section"], "working");
    assert!(st.task_message("nosuchid", "x", None, None, None, None).is_err());
}
