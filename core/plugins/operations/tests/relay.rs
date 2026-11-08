//! The node side (fleet.md R106 item 6): the outbox while alcor is away, the op_id replay,
//! the read cache and the signed orders. Ported from test_relay.py.

mod common;

use std::sync::Arc;

use awm_plugin_operations::relay::{ClientError, Relay};
use awm_trust::grant::load_public_key;
use common::{s, Env, Flaky, ME};
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
async fn a_write_waits_behind_the_outbox_and_then_gets_its_answer() {
    let p = pair();
    p.link.set_up(false);
    let first = p.relay.write("task_open", json!({"title": "first", "session_id": "ccA"})).await.unwrap();
    p.link.set_up(true);
    let second = p.relay.write("task_open", json!({"title": "second", "session_id": "ccB"})).await.unwrap();
    assert!(second.get("queued").is_none(), "the outbox drains before the new write: {second}");
    assert_eq!(p.link.verbs(), ["task_open", "task_open"]);
    let titles: Vec<String> = [&first, &second].iter().map(|r| s(&p.env.store.task_get(&s(&r["task_id"])).unwrap()["title"])).collect();
    assert_eq!(titles, ["first", "second"]);
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
async fn the_relay_refuses_a_verb_of_the_wrong_class() {
    let p = pair();
    assert!(matches!(p.relay.write("task_get", json!({})).await, Err(ClientError::Refused(_))));
    assert!(matches!(p.relay.read("task_open", json!({})).await, Err(ClientError::Refused(_))));
    assert!(matches!(p.relay.poll("task_get", json!({})).await, Err(ClientError::Refused(_))));
    assert!(p.link.verbs().is_empty());
}

#[tokio::test]
async fn orders_run_once_and_only_for_this_node() {
    let p = pair();
    let grant_pub = load_public_key(&p.env.ap.keys.grant_pub_b64u()).unwrap();
    let good = p.env.ap.issue_order("agent", "mizar", json!({"task_id": "ABEiM0RV", "node": "mizar"})).unwrap();
    let other = p.env.ap.issue_order("agent", "alcor", json!({"task_id": "ABEiM0RV", "node": "alcor"})).unwrap();
    let mut forged = good.clone();
    forged["order"]["order_id"] = json!("o-forged");
    let body = p.relay.take_order(&good, &grant_pub).unwrap().expect("a fresh order");
    assert_eq!((body["kind"].clone(), body["payload"]["task_id"].clone()), (json!("agent"), json!("ABEiM0RV")));
    assert_eq!(p.relay.take_order(&good, &grant_pub).unwrap(), None, "a replay is dropped");
    assert!(p.relay.take_order(&other, &grant_pub).is_err(), "another node's order");
    assert!(p.relay.take_order(&forged, &grant_pub).is_err(), "a forged order");
    let late = p.env.ap.keys.envelope("order", json!({"order_id": "o-late", "kind": "agent", "node": "mizar", "payload": {}, "not_after": 1}));
    assert!(p.relay.take_order(&late, &grant_pub).is_err(), "an expired order");
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

#[tokio::test]
async fn a_lost_approval_reply_replays_once() {
    let p = pair();
    p.link.lose_reply.store(true, std::sync::atomic::Ordering::SeqCst);
    let r = p.relay.write("approval_request", json!({"kind": "vault_hold", "reason": "r", "session_id": ME})).await.unwrap();
    assert_eq!(r["queued"], true);
    p.relay.flush().await;
    assert_eq!(p.relay.status()["refused"], 0);
    assert_eq!(p.env.ap.approval_get(&s(&r["request_id"])).unwrap()["status"], "pending");
}

#[tokio::test]
async fn cancel_by_another_session_is_refused() {
    let p = pair();
    let r = p.relay.write("approval_request", json!({"kind": "vault_hold", "reason": "r", "session_id": ME})).await.unwrap();
    let other = format!("cc{}", "2".repeat(32));
    let e = p.relay.write("approval_cancel", json!({"request_id": r["request_id"], "session_id": other})).await.unwrap_err();
    assert!(matches!(&e, ClientError::Refused(m) if m.contains("not_requester")), "{e:?}");
}

#[tokio::test]
async fn marks_are_local_notes() {
    let p = pair();
    assert_eq!(p.relay.mark("needs:x"), None);
    p.relay.set_mark("remind:T1", "a");
    p.relay.set_mark("remind:T2", "b");
    p.relay.set_mark("needs:x", "T1");
    let mut marks = p.relay.marks("remind:");
    marks.sort();
    assert_eq!(marks, [("remind:T1".to_string(), "a".to_string()), ("remind:T2".to_string(), "b".to_string())]);
    p.relay.clear_mark("needs:x");
    assert_eq!(p.relay.mark("needs:x"), None);
}
