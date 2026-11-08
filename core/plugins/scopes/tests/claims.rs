//! Scope claims and the busy-scope queue (scopes.md R3, R41, R42, R46), through the verbs.

mod common;

use serde_json::{json, Value};

const S1: &str = "cc11111111111111111111111111111111";
const S2: &str = "cc22222222222222222222222222222222";
const S3: &str = "cc33333333333333333333333333333333";

fn me() -> i64 {
    std::process::id() as i64
}

fn claim(r: &common::Rig, args: Value) -> Value {
    let mut a = json!({"project": "p", "scope": "s"});
    a.as_object_mut().unwrap().extend(args.as_object().unwrap().clone());
    r.call("claim", a, None).unwrap()
}

fn release(r: &common::Rig, session: &str) -> Value {
    r.call("release", json!({"project": "p", "scope": "s", "session_id": session}), None).unwrap()
}

fn holder(r: &common::Rig) -> Option<String> {
    r.op.live_holder("p", "s").unwrap().map(|h| h["session_id"].as_str().unwrap().to_string())
}

/// A live process that is not this one.
struct Child(std::process::Child);

impl Child {
    fn spawn() -> Self {
        Child(std::process::Command::new("sleep").arg("60").spawn().unwrap())
    }
    fn pid(&self) -> i64 {
        self.0.id() as i64
    }
    fn end(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

impl Drop for Child {
    fn drop(&mut self) {
        self.end();
    }
}

#[test]
fn a_free_scope_is_claimed() {
    let r = common::rig();
    let out = claim(&r, json!({"session_id": S1, "pid": me()}));
    assert_eq!(out, json!({"ok": true, "project": "p", "scope": "s", "claimed": true, "holder": {"session_id": S1, "job": null, "pid": me()}}));
    assert_eq!(holder(&r).as_deref(), Some(S1));
}

#[test]
fn a_second_live_session_is_refused_with_the_holder() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    let out = claim(&r, json!({"session_id": S2, "pid": me()}));
    assert_eq!(out["ok"], json!(false));
    assert_eq!(out["holder"]["session_id"], json!(S1));
    assert!(out["reason"].as_str().unwrap().contains("queue=true"));
    let keys: Vec<&String> = out.as_object().unwrap().keys().collect();
    assert_eq!(keys, ["project", "scope", "claimed", "holder", "ok", "reason"]);
}

#[test]
fn claiming_again_from_the_holder_is_fine() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    assert_eq!(claim(&r, json!({"session_id": S1, "pid": me()}))["claimed"], json!(true));
}

#[test]
fn the_claim_dies_with_the_session() {
    let r = common::rig();
    let mut child = Child::spawn();
    claim(&r, json!({"session_id": S1, "pid": child.pid()}));
    child.end();
    assert_eq!(holder(&r), None);
    assert_eq!(claim(&r, json!({"session_id": S2, "pid": me()}))["claimed"], json!(true));
}

#[test]
fn a_waiting_session_keeps_its_claim() {
    let r = common::rig();
    let mut child = Child::spawn();
    claim(&r, json!({"session_id": S1, "pid": child.pid()}));
    child.end();
    r.out.waiting.lock().unwrap().insert(S1.into());
    assert_eq!(holder(&r).as_deref(), Some(S1));
}

#[test]
fn a_reused_pid_does_not_hold_the_claim() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    r.op.write(|c| {
        c.execute("UPDATE scope_claims SET proc_start='another-boot'", [])?;
        Ok(())
    })
    .unwrap();
    assert_eq!(holder(&r), None);
}

#[test]
fn the_stored_start_time_is_field_22_of_proc_stat() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    let stored: String = r.op.read(|c| Ok(c.query_row("SELECT proc_start FROM scope_claims", [], |row| row.get(0))?)).unwrap();
    let stat = std::fs::read_to_string(format!("/proc/{}/stat", me())).unwrap();
    let after = stat.rsplit_once(')').unwrap().1;
    assert_eq!(stored, after.split_whitespace().nth(19).unwrap());
}

#[test]
fn a_terminal_session_is_warned_not_blocked() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    let out = claim(&r, json!({"session_id": S2, "pid": me(), "terminal": true}));
    assert_eq!(out["ok"], json!(true));
    assert_eq!(out["claimed"], json!(false));
    assert!(out["warning"].as_str().unwrap().contains("not blocked"));
    assert_eq!(holder(&r).as_deref(), Some(S1));
}

#[test]
fn the_queue_is_first_come_first_served() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    let first = claim(&r, json!({"session_id": S2, "pid": me(), "job": "job2", "queue": true}));
    let second = claim(&r, json!({"session_id": S3, "pid": me(), "job": "job3", "queue": true}));
    let again = claim(&r, json!({"session_id": S2, "pid": me(), "job": "job2", "queue": true}));
    assert_eq!((first["queued_position"].clone(), second["queued_position"].clone(), again["queued_position"].clone()), (json!(1), json!(2), json!(1)));

    let out = release(&r, S1);
    assert_eq!(out["released"], json!(true));
    assert_eq!(out["next"]["session_id"], json!(S2));
    assert_eq!(out["notified"], json!(true));
    let notices = r.out.calls_to("message");
    assert_eq!(notices[0]["job"], json!("job2"));
    assert!(notices[0]["text"].as_str().unwrap().contains("p/s"));

    let jump = claim(&r, json!({"session_id": S3, "pid": me()}));
    assert_eq!(jump["claimed"], json!(false));
    assert!(jump["reason"].as_str().unwrap().contains("first"));
    assert_eq!(claim(&r, json!({"session_id": S2, "pid": me()}))["claimed"], json!(true));
    let listing = r.op.listing(Some("p"), Some("s")).unwrap();
    let queue: Vec<&Value> = listing["claims"][0]["queue"].as_array().unwrap().iter().map(|w| &w["session_id"]).collect();
    assert_eq!(queue, [&json!(S3)]);
}

#[test]
fn the_head_is_notified_once() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    claim(&r, json!({"session_id": S2, "pid": me(), "job": "job2", "queue": true}));
    release(&r, S1);
    release(&r, S1);
    claim(&r, json!({"session_id": S3, "pid": me()}));
    assert_eq!(r.out.calls_to("message").len(), 1);
}

#[test]
fn a_dead_waiter_leaves_the_queue() {
    let r = common::rig();
    let mut child = Child::spawn();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    claim(&r, json!({"session_id": S2, "pid": child.pid(), "job": "job2", "queue": true}));
    child.end();
    let out = release(&r, S1);
    assert_eq!(out["next"], Value::Null);
    assert!(r.out.calls_to("message").is_empty());
}

#[test]
fn withdraw_leaves_the_queue() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    claim(&r, json!({"session_id": S2, "pid": me(), "queue": true}));
    let out = r.call("withdraw", json!({"project": "p", "scope": "s", "session_id": S2}), None).unwrap();
    assert_eq!(out["withdrawn"], json!(true));
    assert_eq!(r.op.listing(Some("p"), Some("s")).unwrap()["claims"][0]["queue"], json!([]));
}

#[test]
fn release_by_another_session_changes_nothing() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    assert_eq!(release(&r, S2)["released"], json!(false));
    assert_eq!(holder(&r).as_deref(), Some(S1));
}

#[test]
fn the_caller_pid_stands_in_for_pid() {
    let r = common::rig();
    let out = claim(&r, json!({"session_id": S1, "_caller_pid": me()}));
    assert_eq!(out["holder"]["pid"], json!(me()));
    assert!(holder(&r).is_some());
    let out = claim(&r, json!({"session_id": S1, "pid": me().to_string()}));
    assert_eq!(out["holder"]["pid"], json!(me()), "a digit string counts");
}

#[test]
fn claims_lists_holders_and_queues() {
    let r = common::rig();
    claim(&r, json!({"scope": "a", "session_id": S1, "pid": me()}));
    claim(&r, json!({"scope": "b", "session_id": S2, "pid": me()}));
    let out = r.call("claims", json!({"project": "p"}), None).unwrap();
    assert_eq!(out["count"], json!(2));
    let scopes: Vec<&str> = out["claims"].as_array().unwrap().iter().map(|c| c["scope"].as_str().unwrap()).collect();
    assert_eq!(scopes, ["a", "b"]);
}

#[test]
fn a_missing_or_empty_session_id_is_refused() {
    let r = common::rig();
    let e = r.call("claim", json!({"project": "p", "scope": "s"}), None).unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("ValueError", "claim: missing required argument(s): session_id."));
    let e = r.call("claim", json!({"project": "p", "scope": "s", "session_id": ""}), None).unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("ValueError", "session_id is required"));
}

#[test]
fn bump_counts_up() {
    let r = common::rig();
    let counts: Vec<i64> = (0..3).map(|_| r.op.bump("creates").unwrap()).collect();
    assert_eq!(counts, [1, 2, 3]);
}
