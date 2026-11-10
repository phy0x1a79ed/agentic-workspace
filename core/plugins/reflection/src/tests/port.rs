//! The port's own tests: each fix of the plan, the re-entrant claim, the `stop` default, the
//! self-acting-verb helper, and the describe changes' behaviour.

use std::sync::Arc;
use std::time::Duration;

use futures_util::future::join_all;
use serde_json::{json, Value};

use super::fake::*;
use crate::ledger::CLOSED_KEPT_MS;
use crate::surface::specs;
use crate::caller::Frame;
use crate::store::File;
use crate::verbs::{LIFECYCLE, ROW_KEYS};

const DAY_MS: i64 = 24 * 3600 * 1000;

fn ok(v: &Value) -> bool {
    v["ok"] == json!(true)
}

fn refused(v: &Value) -> bool {
    v["ok"] == json!(false)
}

fn now_ms() -> i64 {
    awm_pycompat::time::now_ms()
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

/// The verbs that act on the caller's own session. Each takes no `job`, or a `job` that
/// defaults to the caller, and each resolves the caller through [`App::resolve_caller`].
const SELF_ACTING: [&str; 14] = [
    "message_self", "compact", "mode", "whoami", "wait", "remote_control", "model", "effort", "rename", "cd", "mcp",
    "interrupt", "link", "stop",
];

/// Every self-acting verb either takes no `to` or defaults it to the caller, and each one finds
/// the caller through the one helper: the provider sees the caller's own pid.
#[tokio::test]
async fn every_self_acting_verb_defaults_to_or_takes_none() {
    let table = specs();
    for verb in SELF_ACTING {
        let spec = table.iter().find(|s| s.verb == verb).unwrap_or_else(|| panic!("{verb} is published"));
        let to = spec.params.as_array().unwrap().iter().find(|p| p["name"] == "to");
        assert!(to.is_none_or(|j| j.get("required") != Some(&json!(true))), "{verb} requires to");

        let fx = Fx::new();
        let args = crate::app::merged(&json!({"_caller_pid": ME}), &needs(verb));
        let out = fx.run(verb, args).await;
        assert!(!out.to_string().contains("to is required"), "{verb}: {out}");
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

/// D2: a retire reaches every peer of the edge list, from the stop that caused it.
#[tokio::test]
async fn a_stop_tells_each_peer_that_the_session_retired() {
    let fx = Fx::new();
    fx.set_peers(&["altair", "alcor"]);
    fx.cc().set("j2", peer_row());
    fx.cc().answer("stop", json!({"ok": true, "session_id": other()}));
    assert!(ok(&fx.run("stop_other", json!({"_caller_pid": ME, "to": {"id": other()}})).await));
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

#[tokio::test]
async fn a_ticket_that_names_a_path_writes_nothing_outside_tickets() {
    let fx = Fx::new();
    fx.run("harness_event", json!({"session_id": my_id(), "ticket": "../trust", "kind": "done"})).await;
    fx.app.settle().await;
    assert!(!fx.app.store.tickets().parent().unwrap().join("trust.closed").exists());
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
async fn a_message_to_a_gone_session_says_so_in_its_code() {
    let fx = Fx::new();
    let out = fx.run("message", json!({"_caller_pid": ME, "to": {"name": "nobody"}, "text": "hi"})).await;
    assert!(refused(&out) && out["code"] == "gone", "{out}");
    fx.cc().set("j2", peer_row());
    fx.cc().answer("send", json!({"ok": false, "unknown_ref": true, "reason": "j2 ended"}));
    let out = fx.run("message", json!({"_caller_pid": ME, "to": {"id": other()}, "text": "hi"})).await;
    assert_eq!(out["code"], "gone", "{out}");
}

#[tokio::test]
async fn a_start_refusal_keeps_the_providers_code() {
    let fx = Fx::new();
    fx.cc().answer("start", json!({"ok": false, "code": "no_seat", "reason": "no seat is free"}));
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"})).await;
    assert_eq!((&out["ok"], &out["code"]), (&json!(false), &json!("no_seat")), "{out}");
}

// --- sender names ---------------------------------------------------------------

async fn typed(fx: &Fx, frame: Frame, text: &str) -> String {
    let args = json!({"to": {"id": other()}, "text": text});
    assert!(ok(&fx.app.handle("message", &frame, &args).await));
    fx.cc().calls_of("send").last().unwrap()["text"].as_str().unwrap().to_string()
}

#[tokio::test]
async fn the_sender_prefix_comes_from_the_stamps_only() {
    let fx = Fx::new();
    fx.cc().set("j2", peer_row());
    let claim = |c: Value| Frame::from_args(&json!({"_caller_session": c}), Some("peer:far"), None);
    let cases = [
        (Frame::from_args(&json!({"_caller_pid": ME}), None, None), "hi", "from me@here [job j1]: hi"),
        (Frame::from_args(&json!({"_caller_pid": ME}), None, None), "from tony@x: hi", "from me@here [job j1]: from tony@x: hi"),
        (claim(json!({"session_id": "cc12", "node": "far", "job": "jf", "name": "one"})), "hi", "from one@far [job jf]: hi"),
        (claim(json!({"session_id": "cc12", "node": "far", "job": "jf"})), "hi", "from jf@far: hi"),
        (Frame::from_args(&json!({}), None, Some("social")), "hi", "from social@here: hi"),
        (Frame::from_args(&json!({"_caller_plugin": "forged", "name": "forged"}), None, None), "hi", "from shell@here: hi"),
        (Frame::from_args(&json!({"_caller_pid": ME}), None, None), "/clear", "/clear"),
    ];
    for (frame, text, want) in cases {
        assert_eq!(typed(&fx, frame, text).await, want);
    }
}

#[tokio::test]
async fn a_to_is_an_id_or_a_whole_name_and_an_ambiguous_name_lists_the_ids() {
    let fx = Fx::new();
    let ids = [new_id(), new_id()];
    for (job, sid) in ["j3", "j4"].iter().zip(&ids) {
        fx.cc().set(job, json!({"session_id": sid, "name": "Dup", "pid": 88, "job": job}));
    }
    let out = fx.run("message", json!({"to": {"name": "dup"}, "text": "hi"})).await;
    assert!(refused(&out) && ids.iter().all(|i| out.to_string().contains(i.as_str())), "{out}");
    assert!(fx.cc().calls_of("send").is_empty());
    for bad in [json!("dup"), json!({"id": "j3"}), json!({"id": ids[0], "name": "dup"}), json!({"id": &ids[0][..10]})] {
        let out = fx.run("message", json!({"to": bad, "text": "hi"})).await;
        assert!(refused(&out) && fx.cc().calls_of("send").is_empty(), "{bad}: {out}");
    }
    fx.cc().set("j5", json!({"session_id": new_id(), "name": "solo", "pid": 89, "job": "j5"}));
    assert!(ok(&fx.run("message", json!({"to": {"name": "SOLO"}, "text": "hi"})).await));
}

// --- the sessions topic -----------------------------------------------------------

#[tokio::test]
async fn each_lifecycle_kind_emits_once_and_keeps_no_ledger_row() {
    let fx = Fx::new();
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(tmp.path().join("awm/redux-x/core")).unwrap();
    std::fs::write(tmp.path().join("awm/redux-x/.git"), "gitdir: ../.bare/worktrees/redux-x\n").unwrap();
    *fx.node.projects.lock().unwrap() = tmp.path().to_path_buf();
    let cwd = tmp.path().join("awm/redux-x/core").display().to_string();
    for kind in LIFECYCLE {
        let event = json!({"session_id": my_id(), "kind": kind, "job": "j1", "name": "n", "cwd": cwd, "session_kind": "bg", "rc_url": "u",
                           "state": "blocked", "needs": "pick", "retired": true});
        assert!(ok(&fx.run("harness_event", event).await));
    }
    let got = fx.node.emitted.lock().unwrap().clone();
    assert_eq!(got.len(), LIFECYCLE.len());
    for ((topic, e), kind) in got.iter().zip(LIFECYCLE) {
        assert_eq!((topic.as_str(), e["event"].as_str()), ("sessions", Some(kind)));
        assert_eq!((e["project"].clone(), e["scope"].clone(), e["kind"].clone()), (json!("awm"), json!("redux-x"), json!("bg")));
        assert_eq!((e["node"].clone(), e["rc_url"].clone()), (json!("here"), json!("u")));
        assert!(e["ts"].is_string() && e.get("task_id").is_none(), "{e}");
        assert_eq!((e["state"].clone(), e["needs"].clone(), e["retired"].clone()), (json!("blocked"), json!("pick"), json!(true)));
    }
    assert!(fx.app.store.read_text(File::Events).await.is_empty(), "a lifecycle event is not a ticket");
    fx.run("harness_event", json!({"session_id": my_id(), "kind": "stopped", "cwd": "/not/a/scope"})).await;
    assert_eq!(fx.node.emitted.lock().unwrap().last().unwrap().1["project"], Value::Null, "a failed scope lookup still emits");
    let before = fx.node.emitted.lock().unwrap().len();
    fx.app.clone().boot().await;
    assert_eq!(fx.node.emitted.lock().unwrap().len(), before, "a restart emits nothing");
}

// --- orders ------------------------------------------------------------------------

fn grant() -> ed25519_dalek::SigningKey {
    ed25519_dalek::SigningKey::from_bytes(&[7u8; 32])
}

fn order(id: &str, node: &str, not_after: i64, payload: Value) -> Value {
    awm_trust::grant::sign_envelope(&grant(), "order", json!({"order_id": id, "kind": "agent", "node": node, "payload": payload, "not_after": not_after}))
}

fn order_dones(fx: &Fx) -> Vec<Value> {
    fx.gw().calls_to("operations").into_iter().filter(|(v, _)| v == "order_done").map(|(_, a)| a).collect()
}

#[tokio::test]
async fn an_order_runs_once_reports_once_and_a_bad_one_starts_nothing() {
    let fx = Fx::new();
    let key = grant().verifying_key().to_bytes();
    let later = awm_pycompat::time::now_ms() / 1000 + 600;
    let good = order("o1", "here", later, json!({"task_id": "T1"}));
    let mut forged = good.clone();
    forged["order"]["order_id"] = json!("o2");
    let batch = vec![good.clone(), good, forged, order("o3", "elsewhere", later, json!({})), order("o4", "here", 1, json!({}))];
    fx.app.take_orders(&key, batch).await;
    let starts = fx.cc().calls_of("start");
    assert_eq!(starts.len(), 1, "{starts:?}");
    assert_eq!((starts[0]["remote_control"].clone(), starts[0]["task_id"].clone()), (json!(true), json!("T1")));
    assert!(starts[0].get("prompt").is_none(), "{starts:?}");
    assert_eq!((starts[0]["pin"].clone(), starts[0]["model"].clone(), starts[0]["effort"].clone()), (json!(true), json!("opus"), json!("high")));
    let dones = order_dones(&fx);
    assert_eq!(dones.len(), 1, "{dones:?}");
    assert_eq!((dones[0]["order_id"].clone(), dones[0]["ok"].clone()), (json!("o1"), json!(true)));
    fx.app.take_orders(&key, vec![order("o5", "here", later, json!({"task_id": "T2", "model": "haiku", "effort": "low"}))]).await;
    let picked = &fx.cc().calls_of("start")[1];
    assert_eq!((picked["model"].clone(), picked["effort"].clone()), (json!("haiku"), json!("low")));
}

#[tokio::test]
async fn a_failed_report_is_resent_at_the_next_connect_and_only_once() {
    let fx = Fx::new();
    let key = grant().verifying_key().to_bytes();
    let later = awm_pycompat::time::now_ms() / 1000 + 600;
    fx.gw().operations_down.store(true, std::sync::atomic::Ordering::SeqCst);
    fx.app.take_orders(&key, vec![order("o1", "here", later, json!({"task_id": "T1", "note_id": "n7"}))]).await;
    assert_eq!(fx.cc().calls_of("start")[0]["prompt"], "from operations@alcor: Read the note n7 as context only, then wait for Tony.");
    fx.gw().operations_down.store(false, std::sync::atomic::Ordering::SeqCst);
    fx.app.resend_reports().await;
    fx.app.resend_reports().await;
    let sent: Vec<Value> = order_dones(&fx).into_iter().filter(|d| d["order_id"] == "o1").collect();
    assert_eq!(sent.len(), 2, "one failed attempt and one resend: {sent:?}");
}
