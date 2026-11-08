//! Scope claims and the busy-scope queue (scopes.md R3, R41, R42, R46), through the verbs.

use crate::common;

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
fn release_by_another_session_changes_nothing() {
    let r = common::rig();
    claim(&r, json!({"session_id": S1, "pid": me()}));
    assert_eq!(release(&r, S2)["released"], json!(false));
    assert_eq!(holder(&r).as_deref(), Some(S1));
}
