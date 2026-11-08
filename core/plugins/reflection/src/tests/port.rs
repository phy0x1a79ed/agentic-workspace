//! The port's own tests: each fix of the plan, the re-entrant claim, the `stop` default, the
//! self-acting-verb helper, and the describe changes' behaviour.

use std::sync::Arc;
use std::time::Duration;

use futures_util::future::join_all;
use serde_json::{json, Value};

use super::fake::*;
use crate::gateway::Node as _;
use crate::ledger::{Ticket, CLOSED_KEPT_MS};
use crate::surface::specs;
use crate::verbs::{ROW_KEYS, SELF_ACTING};

const DAY_MS: i64 = 24 * 3600 * 1000;

fn ok(v: &Value) -> bool {
    v["ok"] == json!(true)
}

fn refused(v: &Value) -> bool {
    v["ok"] == json!(false)
}

fn now_ms() -> i64 {
    crate::gateway::LiveNode.now_ms()
}

/// The args each self-acting verb needs besides `job`.
fn needs(verb: &str) -> Value {
    match verb {
        "message_self" => json!({"text": "hello"}),
        "wait" => json!({"until": [other()]}),
        "model" => json!({"model": "opus"}),
        "effort" => json!({"level": "high"}),
        "rename" => json!({"name": "renamed"}),
        "cd" => json!({"path": "/w/projects/awm/new"}),
        "mcp" => json!({"action": "reconnect", "server": "awm"}),
        "stop" => json!({"confirm": true}),
        _ => json!({}),
    }
}

/// Every self-acting verb either takes no `job` or defaults it to the caller, and each one finds
/// the caller through the one helper: the provider sees the caller's own pid.
#[tokio::test]
async fn every_self_acting_verb_defaults_job_or_takes_none() {
    let table = specs();
    for verb in SELF_ACTING {
        let spec = table.iter().find(|s| s.verb == verb).unwrap_or_else(|| panic!("{verb} is published"));
        let job = spec.params.as_array().unwrap().iter().find(|p| p["name"] == "job");
        assert!(job.is_none_or(|j| j.get("required") != Some(&json!(true))), "{verb} requires job");

        let fx = Fx::new();
        let args = crate::app::merged(&json!({"_caller_pid": ME}), &needs(verb));
        let out = fx.run(verb, args).await;
        assert!(!out.to_string().contains("job is required"), "{verb}: {out}");
        assert!(fx.cc().calls_of("resolve").contains(&json!({"pid": ME})), "{verb} did not resolve the caller");
        let acted: Vec<(String, Value)> =
            fx.cc().calls.lock().unwrap().iter().filter(|(v, _)| !["resolve", "find", "status"].contains(&v.as_str())).cloned().collect();
        for (sent, a) in &acted {
            let mine = a["pid"] == json!(ME) || a["ref"] == "j1" || a["session_id"] == json!(my_id());
            assert!(mine, "{verb} sent {sent} for someone else: {a}");
        }
        fx.app.settle().await;
    }
}

#[tokio::test]
async fn stop_with_no_job_is_the_callers_own_session_and_needs_confirm() {
    let fx = Fx::new();
    let out = fx.run("stop", json!({"_caller_pid": ME})).await;
    assert_eq!(out["kind"], "self", "{out}");
    assert_eq!(out["reason"], "that is your own session; stopping it ends this conversation. Pass confirm=true to proceed.");
    assert!(fx.cc().calls_of("stop").is_empty());

    fx.cc().answer("stop", json!({"ok": true, "session_id": my_id()}));
    let out = fx.run("stop", json!({"_caller_pid": ME, "confirm": true})).await;
    assert!(ok(&out), "{out}");
    assert_eq!(fx.cc().calls_of("stop"), [json!({"session_id": my_id(), "pid": ME, "keep": false, "confirm": true})]);
    assert!(fx.asked().is_empty(), "a self-stop asks no steer policy");
    fx.app.settle().await;
}

#[tokio::test]
async fn an_isolated_caller_stops_itself_by_its_session_id() {
    let fx = Fx::new();
    assert_eq!(fx.run("stop", json!({"_caller_session_id": my_id()})).await["kind"], "self");
    fx.run("stop", json!({"_caller_session_id": my_id(), "confirm": true})).await;
    assert_eq!(fx.cc().calls_of("stop"), [json!({"session_id": my_id(), "keep": false, "confirm": true})]);
    fx.app.settle().await;
}

/// scopes calls back into reflection while it handles a claim: a lock held across the claim would
/// hang the start (brief R1 item 5).
#[tokio::test]
async fn a_claim_that_calls_back_into_reflection_does_not_hang() {
    let fx = Fx::new();
    fx.gw().use_claims();
    let weak = fx.weak();
    let reentered: Hook = Arc::new(move |claim: Value| {
        let weak = weak.clone();
        Box::pin(async move {
            let app = weak.upgrade().expect("the app is alive");
            let pid = claim["pid"].clone();
            let who = app.handle("session_of", &crate::caller::Frame::default(), &json!({"pid": pid})).await;
            assert_eq!(who["session_id"], json!(new_id()), "{who}");
            app.handle("waits", &crate::caller::Frame::default(), &json!({})).await;
            app.record_trust(&third(), "cooperative", "test", None).await;
            app.open_tickets().await;
            who
        })
    });
    *fx.gw().on_claim.lock().unwrap() = Some(reentered);
    let start = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"}));
    let out = tokio::time::timeout(Duration::from_secs(10), start).await.expect("the start hung on its own claim");
    assert!(ok(&out) && out["claim"]["claimed"] == true, "{out}");
    assert_eq!(fx.trust_file().await[third()]["level"], "cooperative");
}

#[tokio::test]
async fn concurrent_trust_records_lose_none() {
    let fx = Fx::new();
    let ids: Vec<String> = (0..16).map(|i| format!("cc{i:032x}")).collect();
    join_all(ids.iter().map(|sid| fx.app.record_trust(sid, "cooperative", "test", None))).await;
    let data = fx.trust_file().await;
    assert!(ids.iter().all(|sid| data.get(sid).is_some()), "{data}");
}

/// D1: the scope verbs answer reflection again, so a held scope is seen and a stop releases.
#[tokio::test]
async fn scope_claims_reach_the_scope_tool() {
    let fx = Fx::new();
    fx.gw().use_claims();
    let holder = json!({"session_id": other(), "pid": 77, "job": "j2"});
    fx.gw().with_claims(|c| c.hold("awm", "x", holder.clone()));
    assert_eq!(fx.app.held_by("awm", "x").await, Some(holder));
    assert_eq!(fx.app.held_by("awm", "free").await, None);
    let asked: Vec<(String, Value)> = fx.gw().calls_to("scope");
    assert_eq!(asked[0], ("claims".to_string(), json!({"project": "awm", "scope": "x"})));
    fx.app.release_claims(&other()).await;
    assert!(fx.gw().with_claims(|c| c.held.is_empty()));
}

/// D2: a retire reaches every peer of the edge list, from the stop that caused it.
#[tokio::test]
async fn a_stop_tells_each_peer_that_the_session_retired() {
    let fx = Fx::new();
    fx.set_peers(&["altair", "alcor"]);
    fx.cc().set("j2", peer_row());
    fx.cc().answer("stop", json!({"ok": true, "session_id": other()}));
    assert!(ok(&fx.run("stop_other", json!({"_caller_pid": ME, "job": "j2"})).await));
    fx.app.settle().await;
    let told: Vec<(String, String)> = fx.gw().peer_calls().into_iter().filter(|(_, v, _)| v == "retired").map(|(p, _, a)| (p, a["session_id"].as_str().unwrap().into())).collect();
    assert_eq!(told, [("altair".to_string(), other()), ("alcor".to_string(), other())]);
}

/// D4: a `.closed` marker goes once it is older than seven days, and an event starts the sweep.
#[tokio::test]
async fn a_closed_marker_is_pruned_after_seven_days() {
    let fx = Fx::new();
    fx.run("harness_event", json!({"session_id": my_id(), "ticket": "t-7", "kind": "done"})).await;
    fx.app.settle().await;
    let marker = fx.app.store.tickets().join("t-7.closed");
    assert!(marker.exists());
    assert_eq!(fx.app.prune_closed(now_ms() + CLOSED_KEPT_MS - DAY_MS).await, 0);
    assert!(marker.exists());

    *fx.node.now.lock().unwrap() = Some(now_ms() + CLOSED_KEPT_MS + DAY_MS);
    fx.run("harness_event", json!({"session_id": my_id(), "kind": "done"})).await;
    fx.app.settle().await;
    assert!(!marker.exists());
    assert_eq!(CLOSED_KEPT_MS, 7 * DAY_MS);
}

/// The 300 s timer is gone: an event sweeps the tickets a provider dropped.
#[tokio::test]
async fn an_event_sweeps_the_tickets_a_provider_dropped() {
    let fx = Fx::new();
    let ticket = Ticket { ticket: "dropped".into(), session_id: my_id(), tool: "harness-cc".into(), command: "/compact".into(), ..Ticket::default() };
    fx.app.open_ticket(&ticket).await;
    *fx.node.now.lock().unwrap() = Some(10_i64.pow(13));
    fx.run("harness_event", json!({"session_id": my_id(), "kind": "done"})).await;
    fx.app.settle().await;
    assert!(fx.app.open_tickets().await.is_empty());
    assert_eq!(fx.app.recent_lost().await[0]["ticket"], "dropped");
}

#[tokio::test]
async fn a_lost_follow_up_is_logged_without_its_text() {
    let logs = Captured::default();
    let _guard = logs.install();
    let fx = Fx::new();
    fx.run("message_self", json!({"_caller_pid": ME, "text": "secret words", "followup": "secret follow"})).await;
    fx.run("harness_event", json!({"session_id": my_id(), "ticket": "t-1", "kind": "lost", "reason": "worker swapped"})).await;
    fx.app.settle().await;
    let text = logs.text();
    assert!(text.contains("t-1"), "{text}");
    assert!(!text.contains("secret"), "{text}");
}

#[tokio::test]
async fn a_ticket_that_names_a_path_writes_nothing_outside_tickets() {
    let fx = Fx::new();
    fx.run("harness_event", json!({"session_id": my_id(), "ticket": "../trust", "kind": "done"})).await;
    fx.app.settle().await;
    assert!(!fx.app.store.dir().join("trust.closed").exists());
}

#[tokio::test]
async fn session_of_asks_each_provider_once() {
    let fx = Fx::new();
    assert!(ok(&fx.run("session_of", json!({"pid": ME})).await));
    assert_eq!(fx.cc().calls_of("resolve").len(), 1);
    assert_eq!(fx.ds().calls_of("resolve").len(), 1);
}

#[tokio::test]
async fn list_other_names_each_failing_provider_and_fills_every_row_key() {
    let fx = Fx::new();
    fx.ds().down.store(true, std::sync::atomic::Ordering::SeqCst);
    let out = fx.run("list_other", json!({})).await;
    assert!(ok(&out), "{out}");
    assert_eq!(out["errors"][0]["harness"], "harness-ds");
    for row in out["sessions"].as_array().unwrap() {
        for key in ROW_KEYS {
            assert!(row.get(key).is_some(), "{key} missing from {row}");
        }
    }
    let again = fx.run("list_other", json!({})).await;
    assert!(again["errors"][0]["error"].as_str().unwrap().contains("skipped"), "{again}");
    assert_eq!(fx.ds().calls_of("list").len(), 1, "a provider marked down is not asked again");
}

#[tokio::test]
async fn list_other_forwards_include_stopped() {
    let fx = Fx::new();
    fx.run("list_other", json!({"include_stopped": true})).await;
    assert_eq!(fx.cc().calls_of("list"), [json!({"include_stopped": true})]);
}

#[tokio::test]
async fn a_message_to_a_gone_session_says_so_in_its_code() {
    let fx = Fx::new();
    let out = fx.run("message", json!({"_caller_pid": ME, "job": "nobody", "text": "hi"})).await;
    assert!(refused(&out) && out["code"] == "gone", "{out}");
    fx.cc().set("j2", peer_row());
    fx.cc().answer("send", json!({"ok": false, "unknown_ref": true, "reason": "j2 ended"}));
    let out = fx.run("message", json!({"_caller_pid": ME, "job": "j2", "text": "hi"})).await;
    assert_eq!(out["code"], "gone", "{out}");
}

#[tokio::test]
async fn a_start_refusal_keeps_the_providers_code() {
    let fx = Fx::new();
    fx.cc().answer("start", json!({"ok": false, "code": "no_seat", "reason": "no seat is free"}));
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"})).await;
    assert_eq!((&out["ok"], &out["code"]), (&json!(false), &json!("no_seat")), "{out}");
}

#[tokio::test]
async fn probe_asks_each_provider_that_serves_it_as_the_caller() {
    let fx = Fx::new();
    assert!(refused(&fx.run("probe", json!({})).await));
    assert!(fx.run("probe", json!({"node": "altair"})).await["reason"].as_str().unwrap().contains("serves probe"));

    let fx2 = Fx::new();
    fx2.gw().capabilities.lock().unwrap().insert("harness-cc".into(), vec!["probe".into()]);
    fx2.cc().answer("probe", json!({"ok": true, "answers": true, "daemon": "up"}));
    let out = fx2.run_as("probe", json!({"node": "altair"}), Some("altair")).await;
    assert_eq!(out, json!({"harness": "harness-cc", "ok": true, "answers": true, "daemon": "up"}));
    assert_eq!(fx2.cc().calls_of("probe"), [json!({"node": "altair"})]);
    assert!(fx2.ds().calls_of("probe").is_empty());
    let sent = fx2.gw().sent.lock().unwrap().iter().find(|c| c.verb == "probe").cloned().unwrap();
    assert_eq!(sent.as_.as_deref(), Some("altair"));
}

#[tokio::test]
async fn boot_alarms_for_a_waited_on_session_that_is_gone() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    fx.cc().answer("status", json!({"ok": false, "unknown_ref": true, "reason": "no such session"}));
    fx.app.clone().boot().await;
    fx.app.settle().await;
    assert!(fx.app.recent_lost().await[0]["reason"].as_str().unwrap().contains(&other()));
}
