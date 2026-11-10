//! The node side (fleet.md R106 item 6): the outbox while alcor is away, the op_id replay,
//! the read cache and the signed orders. Ported from test_relay.py.

use std::sync::Arc;

use awm_plugin_operations::relay::{ClientError, Relay};
use crate::common::{s, Env, Flaky, ME};
use serde_json::{json, Value};

struct Pair {
    env: Env,
    link: Arc<Flaky>,
    relay: Relay,
}

fn pair() -> Pair {
    let env = Env::new();
    let link = Flaky::new(env.home.clone(), "mizar");
    let relay = Relay::open("mizar", &env.path("r.db"), link.clone()).unwrap();
    Pair { env, link, relay }
}

#[tokio::test]
async fn online_write_and_read() {
    let p = pair();
    let r = p.relay.write("task_open", json!({"title": "x", "session_id": "ccA"})).await.unwrap();
    assert!(r.get("queued").is_none(), "{r}");
    let tid = s(&r["task_id"]);
    assert_eq!(p.env.store.task_get(&tid).unwrap()["peer"], "mizar");
    assert_eq!(p.relay.read("task_for_session", json!({"session_id": "ccA"})).await.unwrap(), json!(tid));
}

#[tokio::test]
async fn outbox_keeps_order_while_alcor_is_away() {
    let p = pair();
    p.link.set_up(false);
    let r = p.relay.write("task_open", json!({"title": "offline work", "session_id": "ccA"})).await.unwrap();
    assert_eq!((r["queued"].clone(), r["outbox"].clone()), (json!(true), json!(1)));
    let tid = s(&r["task_id"]);
    p.relay.write("task_report", json!({"task_id": tid, "session_id": "ccA", "state": "blocked"})).await.unwrap();
    p.relay.write("task_report", json!({"task_id": tid, "session_id": "ccA", "state": "review"})).await.unwrap();
    assert!(p.relay.status()["alcor_down_since"].is_number());
    assert_eq!(p.relay.read("task_for_session", json!({"session_id": "ccA"})).await.unwrap(), json!(tid));
    let stale = p.relay.read("task_get", json!({"task_id": tid})).await.unwrap();
    assert_eq!(stale["stale"], true);
    p.link.set_up(true);
    assert_eq!(p.relay.flush().await, 3);
    let c = p.env.store.task_get(&tid).unwrap();
    assert_eq!((s(&c["section"]), s(&c["status"])), ("needs_you".into(), "review".into()));
    assert_eq!(p.link.verbs(), ["task_open", "task_report", "task_report"]);
    assert_eq!(p.relay.status(), json!({"node": "mizar", "outbox": 0, "refused": 0, "alcor_down_since": null}));
}

#[tokio::test]
async fn a_stale_read_carries_the_last_answer() {
    let p = pair();
    let tid = s(&p.relay.write("task_open", json!({"title": "x", "session_id": "ccA"})).await.unwrap()["task_id"]);
    let fresh = p.relay.read("task_get", json!({"task_id": tid})).await.unwrap();
    p.link.set_up(false);
    let stale = p.relay.read("task_get", json!({"task_id": tid})).await.unwrap();
    assert_eq!((stale["stale"].clone(), stale["result"].clone()), (json!(true), fresh));
    assert!(stale["as_of"].is_number() && stale["alcor_down_since"].is_number());
    let never = p.relay.read("task_get", json!({"task_id": "nosuchid"})).await.unwrap();
    assert_eq!(never["result"], Value::Null);
}

#[tokio::test]
async fn lost_reply_replays_once() {
    let p = pair();
    p.link.lose_reply.store(true, std::sync::atomic::Ordering::SeqCst);
    let r = p.relay.write("task_open", json!({"title": "x", "session_id": "ccA"})).await.unwrap();
    assert_eq!(r["queued"], true);
    p.relay.flush().await;
    assert_eq!(p.env.store.task_list(None, None, None, None).unwrap()["cards"].as_array().unwrap().len(), 1);
    let ops = p.link.ops();
    assert_eq!(ops[0], ops[1], "the replay reuses the op_id");
    assert_eq!(ops[0].as_deref(), Some("mizar:1"));
}

#[tokio::test]
async fn refused_write_surfaces_and_does_not_block() {
    let p = pair();
    let e = p.relay.write("task_report", json!({"task_id": "nosuchid", "state": "done"})).await.unwrap_err();
    assert!(matches!(e, ClientError::Refused(_)), "{e:?}");
    assert_eq!(p.relay.status()["refused"], 1);
    assert_eq!(p.relay.pending(), 0);
    let next = p.relay.write("task_open", json!({"title": "after", "session_id": "ccA"})).await.unwrap();
    assert!(next.get("queued").is_none());
}

#[tokio::test]
async fn approvals_ride_the_outbox() {
    // A request made while alcor is away keeps its id, and only its session cancels it (R97 item 1).
    let p = pair();
    p.link.set_up(false);
    let r = p.relay.write("approval_request", json!({"kind": "vault_hold", "node": "deneb", "reason": "checks tripped", "session_id": ME})).await.unwrap();
    assert_eq!(r["queued"], true);
    let rid = s(&r["request_id"]);
    assert!(rid.starts_with('q') && rid.len() == 13, "{rid}");
    let e = p.relay.poll("approval_wait", json!({"request_id": rid, "wait_s": 0})).await.unwrap_err();
    assert!(matches!(e, ClientError::Unreachable(_)));
    p.relay.write("approval_cancel", json!({"request_id": rid, "session_id": ME})).await.unwrap();
    p.link.set_up(true);
    assert_eq!(p.relay.flush().await, 2);
    let got = p.env.ap.approval_get(&rid).unwrap();
    assert_eq!((s(&got["status"]), s(&got["requester_session"])), ("cancelled".into(), ME.into()));
    let card = p.env.store.task_get(&s(&got["task_id"])).unwrap();
    assert_eq!((s(&card["kind"]), s(&card["section"]), card["status"].clone()), ("task".into(), "working".into(), Value::Null),
               "the requester's own card leaves Needs you once nothing waits");
    assert_eq!(card["sessions"][0]["session_id"], ME);
}

// MIGRATION(session-ids)
#[test]
fn marks_keyed_by_a_job_are_dropped_at_open() {
    let p = pair();
    let session = format!("cc{}", "a".repeat(32));
    for key in ["job:abcd1234", "rc:mizar:abcd1234", &format!("rc:mizar:{session}"), "remind:T1"] {
        p.relay.set_mark(key, "x");
    }
    let reopened = Relay::open("mizar", &p.env.path("r.db"), p.link.clone()).unwrap();
    let mut left: Vec<String> = ["job:", "rc:", "remind:"].iter().flat_map(|prefix| reopened.marks(prefix)).map(|(k, _)| k).collect();
    left.sort();
    assert_eq!(left, [format!("rc:mizar:{session}"), "remind:T1".to_string()]);
}
