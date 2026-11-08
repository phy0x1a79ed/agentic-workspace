//! `tests/test_reflection_dispatch.py`, one Rust test per Python test: reflection against fake
//! harness plugins, the contract seen from reflection's side. A parametrized Python test runs
//! each of its cases here.

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

use awm_plugin_sdk::config::EdgeList;
use serde_json::{json, Value};

use super::fake::*;
use crate::caller::Frame;
use crate::providers::Provider;
use crate::surface::specs;

async fn no_wake(_row: Value) -> Result<Value, String> {
    panic!("woke a waiter")
}

fn ok(v: &Value) -> bool {
    v["ok"] == json!(true)
}

fn refused(v: &Value) -> bool {
    v["ok"] == json!(false)
}

fn error(v: &Value) -> String {
    v["error"].as_str().unwrap_or_default().to_string()
}

fn reason(v: &Value) -> String {
    v["reason"].as_str().unwrap_or_default().to_string()
}

#[tokio::test]
async fn test_every_published_verb_has_a_handler_and_a_reflection_name() {
    let fx = Fx::new();
    let table: Value = serde_json::from_str(include_str!("../surface.json")).unwrap();
    for f in table["functions"].as_array().unwrap() {
        assert_eq!(f["tool"], json!(format!("reflection_{}", f["name"].as_str().unwrap())), "{f}");
    }
    for s in specs() {
        let out = fx.run(&s.verb, json!({})).await;
        assert!(!error(&out).starts_with("unknown verb"), "{}: {out}", s.verb);
    }
}

#[test]
fn test_the_internal_verbs_stay_off_mcp() {
    let hidden: BTreeSet<String> =
        specs().into_iter().filter(|s| s.surfaces.as_deref() == Some(&["cli".to_string(), "http".to_string()][..])).map(|s| s.verb).collect();
    for v in ["harness_event", "session_of", "retired", "retired_among"] {
        assert!(hidden.contains(v), "{v}");
    }
}

/// The daemon and warm-pool verbs are cch's own (install.md R135 item 4).
#[test]
fn test_no_harness_specific_verb_is_reflections() {
    let names: BTreeSet<String> = specs().into_iter().map(|s| s.verb).collect();
    for v in ["claim_other", "seed_other", "remove_other", "revive_pinned", "daemon_status", "daemon_ensure", "daemon_hold", "daemon_release"] {
        assert!(!names.contains(v), "{v}");
    }
}

#[tokio::test]
async fn test_message_self_goes_to_the_harness_that_owns_the_caller() {
    let fx = Fx::new();
    let out = fx.run("message_self", json!({"_caller_pid": ME, "text": "/compact", "followup": "go on"})).await;
    assert!(ok(&out) && out["ticket"] == "t-1", "{out}");
    let (verb, args) = fx.cc().last();
    assert!(verb == "send" && args["pid"] == ME && args["submit"] == true, "{args}");
    assert_eq!(fx.ds().verbs(), ["resolve"]);
    let tickets = fx.app.open_tickets().await;
    assert_eq!(tickets.len(), 1);
    assert_eq!((tickets[0]["session_id"].as_str(), tickets[0]["tool"].as_str()), (Some(my_id().as_str()), Some("harness-cc")));
}

#[tokio::test]
async fn test_a_caller_no_harness_owns_is_refused() {
    let fx = Fx::new();
    let out = fx.run("compact", json!({"_caller_pid": 99})).await;
    assert!(refused(&out) && error(&out).contains("pid 99"), "{out}");
    assert!(!fx.cc().verbs().contains(&"compact".to_string()));
}

#[tokio::test]
async fn test_a_call_with_no_caller_pid_is_refused() {
    let fx = Fx::new();
    let out = fx.run("compact", json!({})).await;
    assert!(refused(&out) && error(&out).contains("could not tell which session"), "{out}");
}

#[tokio::test]
async fn test_a_harness_that_owns_the_pid_but_not_the_session_is_refused() {
    let fx = Fx::new();
    fx.ds().answer("resolve", json!({"ok": true, "mine": true, "session_id": null, "error": "dsh cannot tell which session called"}));
    fx.cc().clear();
    let out = fx.run("compact", json!({"_caller_pid": ME})).await;
    assert_eq!(out, json!({"ok": false, "error": "dsh cannot tell which session called"}));
}

#[tokio::test]
async fn test_an_r9_id_picks_the_harness_by_its_platform_code() {
    let fx = Fx::new();
    let ds_id = format!("ds{}", "a".repeat(32));
    fx.run("interrupt", json!({"_caller_pid": ME, "job": ds_id})).await;
    assert!(fx.cc().verbs() == ["resolve"] || !fx.cc().verbs().contains(&"find".to_string()));
    assert_eq!(fx.ds().last(), ("interrupt".to_string(), json!({"session_id": ds_id, "pid": ME})));
}

#[tokio::test]
async fn test_a_job_is_found_by_asking_each_harness() {
    let fx = Fx::new();
    fx.run("model", json!({"_caller_pid": ME, "job": "j1", "model": "opus"})).await;
    assert_eq!(fx.cc().last(), ("set".to_string(), json!({"ref": "j1", "pid": ME, "model": "opus"})));
}

#[tokio::test]
async fn test_mcp_goes_to_the_harness_of_the_session_it_names() {
    let fx = Fx::new();
    fx.run("mcp", json!({"_caller_pid": ME, "job": "j1", "action": "reconnect", "server": "awm"})).await;
    assert_eq!(fx.cc().last(), ("mcp".to_string(), json!({"ref": "j1", "pid": ME, "action": "reconnect", "server": "awm"})));
}

#[tokio::test]
async fn test_a_ref_two_harnesses_claim_is_refused() {
    let fx = Fx::new();
    fx.ds().set("j1", json!({"session_id": format!("ds{}", "b".repeat(32))}));
    let out = fx.run("interrupt", json!({"_caller_pid": ME, "job": "j1"})).await;
    assert!(refused(&out) && error(&out).contains("harness-cc, harness-ds"), "{out}");
}

#[tokio::test]
async fn test_an_unknown_job_names_the_peer_that_runs_it() {
    let fx = Fx::new();
    fx.set_peers(&["altair"]);
    fx.gw().peer_answer("altair", "list_other", Ok(json!({"sessions": [{"job": "67212311", "name": "x"}]})));
    let out = fx.run("message", json!({"_caller_pid": ME, "job": "67212311", "text": "hi"})).await;
    assert!(refused(&out) && error(&out).contains("peer='altair'"), "{out}");
    let asked: Vec<(String, String)> = fx.gw().peer_calls().into_iter().map(|(p, v, _)| (p, v)).collect();
    assert_eq!(asked, [("altair".to_string(), "list_other".to_string())]);
}

#[tokio::test]
async fn test_a_call_from_a_peer_does_not_fan_out_again() {
    let fx = Fx::new();
    fx.set_peers(&["altair"]);
    let out = fx.run("message", json!({"job": "67212311", "text": "hi"})).await;
    assert!(error(&out).contains("no session on this node"), "{out}");
    assert!(fx.gw().peer_calls().is_empty(), "fanned out");
}

#[tokio::test]
async fn test_a_harness_that_does_not_answer_is_skipped() {
    let fx = Fx::new();
    fx.ds().down.store(true, std::sync::atomic::Ordering::SeqCst);
    let out = fx.run("list_other", json!({})).await;
    let harnesses: Vec<&str> = out["sessions"].as_array().unwrap().iter().map(|r| r["harness"].as_str().unwrap()).collect();
    assert_eq!(harnesses, ["harness-cc"]);
}

#[tokio::test]
async fn test_list_other_merges_every_harness() {
    let fx = Fx::new();
    fx.ds().set("x", json!({"session_id": format!("ds{}", "c".repeat(32)), "name": "web"}));
    let out = fx.run("list_other", json!({"project": "awm"})).await;
    let harnesses: BTreeSet<&str> = out["sessions"].as_array().unwrap().iter().map(|r| r["harness"].as_str().unwrap()).collect();
    assert_eq!(harnesses, BTreeSet::from(["harness-cc", "harness-ds"]));
    assert_eq!(fx.cc().last(), ("list".to_string(), json!({"project": "awm"})));
}

/// A session with no record shows None, which a gate reads as trusted when a harness knows it.
#[tokio::test]
async fn test_list_other_shows_each_recorded_level() {
    let fx = Fx::new();
    let web = format!("ds{}", "c".repeat(32));
    fx.ds().set("x", json!({"session_id": web, "name": "web"}));
    fx.app.record_trust(&web, "cooperative", "test", None).await;
    let out = fx.run("list_other", json!({})).await;
    let row = |h: &str| out["sessions"].as_array().unwrap().iter().find(|r| r["harness"] == h).unwrap().clone();
    assert_eq!(row("harness-ds")["trust"], "cooperative");
    assert_eq!(row("harness-cc")["trust"], Value::Null);
}

/// Only a session a harness recorded, by hand with no level, reads trusted (R103 item 2).
#[tokio::test]
async fn test_a_session_no_harness_knows_reads_untrusted() {
    let fx = Fx::new();
    assert_eq!(fx.app.level_of(Some(&my_id()), false, None).await, "trusted");
    fx.cc().answer("status", json!({"ok": false, "reason": "no such session"}));
    assert_eq!(fx.app.level_of(Some(&format!("cc{}", "9".repeat(32))), false, None).await, "untrusted");
    assert_eq!(fx.app.level_of(None, false, None).await, "trusted", "a caller that is no session");
}

#[tokio::test]
async fn test_stop_retires_the_session_and_removes_its_job() {
    let fx = Fx::new();
    fx.run("stop", json!({"_caller_pid": ME, "job": "j1", "confirm": true})).await;
    assert_eq!(fx.cc().last().1["keep"], false);
}

#[tokio::test]
async fn test_start_other_passes_the_spec_through_and_records_trust() {
    let fx = Fx::new();
    let out = fx
        .run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "task_id": "t9", "role": "worker",
                                    "trust": "cooperative", "name": "seat:ana"}))
        .await;
    let starts = fx.cc().calls_of("start");
    assert_eq!(starts.len(), 1);
    assert!(starts[0]["task_id"] == "t9" && starts[0]["pid"] == ME, "{}", starts[0]);
    assert_eq!(out["trust"], "cooperative", "{out}");
    assert_eq!(fx.app.level_of(Some(&new_id()), false, None).await, "cooperative");
    assert_eq!(fx.trust_file().await[new_id()]["origin"], "seat:ana");
}

#[tokio::test]
async fn test_start_other_records_the_caller_as_the_parent() {
    let fx = Fx::new();
    fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"})).await;
    assert_eq!(fx.links(), [(new_id(), my_id(), "start".to_string())]);
}

#[tokio::test]
async fn test_a_start_with_no_calling_session_records_no_parent() {
    let fx = Fx::new();
    fx.run("start_other", json!({"project": "awm", "scope": "x"})).await;
    assert!(fx.links().is_empty());
}

#[tokio::test]
async fn test_a_clear_event_records_the_old_session_as_the_parent() {
    let fx = Fx::new();
    let old = "0e380e38-0e38-0e38-0e38-0e380e380e38";
    let new = format!("cc{}", "2".repeat(32));
    let out = fx.run("harness_event", json!({"session_id": new, "kind": "clear", "parent": old})).await;
    assert_eq!(out, json!({"ok": true, "recorded": true}));
    assert_eq!(fx.links(), [(new.clone(), my_id(), "clear".to_string())]);
    assert!(refused(&fx.run("harness_event", json!({"session_id": new, "kind": "clear"})).await));
}

#[tokio::test]
async fn test_an_isolated_start_is_untrusted_unless_trust_is_named() {
    let fx = Fx::new();
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "isolated": true})).await;
    assert_eq!(fx.cc().calls_of("start")[0]["isolated"], true);
    assert_eq!(out["trust"], "untrusted");
}

#[tokio::test]
async fn test_a_start_above_the_callers_trust_is_refused() {
    let fx = Fx::new();
    fx.app.record_trust(&my_id(), "cooperative", "test", None).await;
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "trust": "trusted"})).await;
    assert!(refused(&out) && error(&out).contains("grant"), "{out}");
    assert!(!fx.cc().verbs().contains(&"start".to_string()));
}

#[tokio::test]
async fn test_a_steer_the_callers_trust_forbids_is_refused() {
    let fx = Fx::new();
    fx.cc().set("j2", peer_row());
    fx.deny_steers("no");
    let out = fx.run("interrupt", json!({"_caller_pid": ME, "job": "j2"})).await;
    assert_eq!(out, json!({"ok": false, "error": "no"}));
    assert!(!fx.cc().verbs().contains(&"interrupt".to_string()));
}

/// A session acting on itself steers nobody: retire's self-stop works at every level.
#[tokio::test]
async fn test_a_session_stops_itself_by_its_own_job() {
    for level in ["trusted", "cooperative", "untrusted"] {
        let fx = Fx::new();
        fx.app.record_trust(&my_id(), level, "test", Some("j1")).await;
        let out = fx.run("stop", json!({"_caller_pid": ME, "job": "j1", "confirm": true})).await;
        assert!(ok(&out), "{level}: {out}");
        let (verb, args) = fx.cc().last();
        assert!(verb == "stop" && args["confirm"] == true, "{level}");
    }
}

#[tokio::test]
async fn test_a_self_stop_never_asks_the_steer_policy() {
    let fx = Fx::new();
    fx.deny_steers("no");
    assert!(ok(&fx.run("stop", json!({"_caller_pid": ME, "job": "j1", "confirm": true})).await));
    assert!(fx.asked().is_empty());
}

#[tokio::test]
async fn test_a_trusted_sessions_stop_of_another_job_still_runs_the_check() {
    for verb in ["stop", "stop_other"] {
        let fx = Fx::new();
        fx.cc().set("j2", peer_row());
        fx.deny_steers("no");
        let out = fx.run(verb, json!({"_caller_pid": ME, "job": "j2", "confirm": true})).await;
        assert_eq!(out, json!({"ok": false, "error": "no"}), "{verb}");
        assert_eq!(fx.asked(), [("trusted".to_string(), verb.to_string(), Some("trusted".to_string()))]);
        assert!(!fx.cc().verbs().contains(&"stop".to_string()));
    }
}

#[tokio::test]
async fn test_an_untrusted_session_cannot_stop_another() {
    for verb in ["stop", "stop_other"] {
        let fx = Fx::new();
        fx.cc().set("j2", peer_row());
        fx.app.record_trust(&my_id(), "untrusted", "test", Some("j1")).await;
        let out = fx.run(verb, json!({"_caller_pid": ME, "job": "j2", "confirm": true})).await;
        assert!(refused(&out) && error(&out).contains("untrusted"), "{verb}: {out}");
        assert!(!fx.cc().verbs().contains(&"stop".to_string()));
    }
}

/// The target named by job is described by its harness, so its level counts.
#[tokio::test]
async fn test_a_short_job_ref_carries_the_targets_level() {
    let fx = Fx::new();
    fx.cc().set("j2", peer_row());
    fx.app.record_trust(&my_id(), "cooperative", "test", Some("j1")).await;
    let out = fx.run("interrupt", json!({"_caller_pid": ME, "job": "j2"})).await;
    assert!(refused(&out) && error(&out).contains("trusted"), "{out}");
}

/// No ledger entry and no harness that finds the id by status: whoami and the steer check must
/// agree, since the harness resolved the caller by its pid.
#[tokio::test]
async fn test_a_caller_whoami_calls_trusted_steers_as_trusted() {
    let fx = Fx::new();
    fx.cc().set("j2", peer_row());
    fx.cc().answer("status", json!({"ok": false, "reason": "no such session"}));
    assert_eq!(fx.run("whoami", json!({"_caller_pid": ME})).await["trust"], "trusted");
    assert!(ok(&fx.run("stop", json!({"_caller_pid": ME, "job": "j2", "confirm": true})).await));
}

/// A cleared or respawned conversation keeps its job, and so its level.
#[tokio::test]
async fn test_a_level_follows_the_job_to_a_new_session_id() {
    for level in ["cooperative", "untrusted"] {
        let fx = Fx::new();
        fx.cc().set("j2", peer_row());
        fx.app.record_trust(&format!("cc{}", "5".repeat(32)), level, "test", Some("j1")).await;
        assert_eq!(fx.run("whoami", json!({"_caller_pid": ME})).await["trust"], level);
        assert!(refused(&fx.run("stop", json!({"_caller_pid": ME, "job": "j2", "confirm": true})).await), "{level}");
        assert!(ok(&fx.run("stop", json!({"_caller_pid": ME, "job": "j1", "confirm": true})).await), "{level}");
    }
}

#[tokio::test]
async fn test_start_other_records_the_new_sessions_job() {
    let fx = Fx::new();
    fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"})).await;
    assert_eq!(fx.trust_file().await[new_id()]["job"], "abcd1234");
}

#[tokio::test]
async fn test_the_first_local_message_carries_the_hint_once() {
    let fx = Fx::new();
    let first = fx.run("message", json!({"_caller_pid": ME, "job": "j1", "text": "hi"})).await;
    let second = fx.run("message", json!({"_caller_pid": ME, "job": "j1", "text": "hi"})).await;
    assert_eq!(first["hint"], awm_trust::FIRST_LOCAL_MESSAGE_HINT);
    assert!(second.get("hint").is_none(), "{second}");
}

#[tokio::test]
async fn test_whoami_adds_trust_task_and_harness() {
    let fx = Fx::new();
    let out = fx.run("whoami", json!({"_caller_pid": ME})).await;
    assert_eq!((&out["session_id"], &out["harness"], &out["task_id"], &out["trust"]), (&json!(my_id()), &json!("harness-cc"), &json!("task-9"), &json!("trusted")));
}

#[tokio::test]
async fn test_session_of_names_the_session_behind_a_pid_a_service_holds() {
    let fx = Fx::new();
    let out = fx.run("session_of", json!({"pid": ME})).await;
    assert_eq!((&out["ok"], &out["session_id"], &out["harness"]), (&json!(true), &json!(my_id()), &json!("harness-cc")));
}

#[tokio::test]
async fn test_session_of_a_pid_no_harness_claims_is_not_known() {
    let fx = Fx::new();
    let out = fx.run("session_of", json!({"pid": 31337})).await;
    assert!(ok(&out) && out["known"] == false && out.get("session_id").is_none(), "{out}");
}

#[tokio::test]
async fn test_cd_claims_the_new_scope_and_releases_the_old() {
    let fx = Fx::new();
    *fx.gw().scope_fixed.lock().unwrap() = Some(json!({"ok": true, "claimed": true}));
    let out = fx.run("cd", json!({"_caller_pid": ME, "path": "/w/projects/awm/new"})).await;
    assert!(ok(&out) && out["claim"]["claimed"] == true, "{out}");
    let calls = fx.gw().calls_to("scope");
    assert_eq!(calls.iter().map(|c| c.0.as_str()).collect::<Vec<_>>(), ["claim", "release"]);
    assert!(calls[0].1["scope"] == "new" && calls[1].1["scope"] == "old");
    assert_eq!(fx.cc().last(), ("set".to_string(), json!({"pid": ME, "cwd": "/w/projects/awm/new"})));
}

#[tokio::test]
async fn test_cd_hands_the_new_scopes_brief_to_the_harness() {
    let fx = Fx::new();
    *fx.gw().scope_fixed.lock().unwrap() = Some(json!({"ok": true, "claimed": true}));
    *fx.gw().brief.lock().unwrap() = json!({"brief": {"line": "The brief of awm/new: For x."}});
    fx.run("cd", json!({"_caller_pid": ME, "path": "/w/projects/awm/new"})).await;
    assert_eq!(fx.cc().last(), ("set".to_string(), json!({"pid": ME, "cwd": "/w/projects/awm/new", "brief": "The brief of awm/new: For x."})));
    let asked: Vec<Value> = fx.gw().calls_to("journal").into_iter().filter(|(v, _)| v == "brief_read").map(|(_, a)| a).collect();
    assert_eq!(asked, [json!({"project": "awm", "scope": "new"})]);
}

#[tokio::test]
async fn test_the_brief_line_reads_the_journal() {
    let fx = Fx::new();
    *fx.gw().brief.lock().unwrap() = json!({"brief": {"line": "The brief of awm/s: For the scopes service."}});
    assert_eq!(fx.app.brief_line("awm", "s").await, "The brief of awm/s: For the scopes service.");
    *fx.gw().brief.lock().unwrap() = json!({"brief": null});
    assert_eq!(fx.app.brief_line("awm", "s").await, "");
}

#[tokio::test]
async fn test_a_background_session_does_not_move_into_a_held_scope() {
    let fx = Fx::new();
    *fx.gw().scope_fixed.lock().unwrap() = Some(json!({"ok": false, "claimed": false, "holder": "cc99"}));
    let out = fx.run("cd", json!({"_caller_pid": ME, "path": "/w/projects/awm/new"})).await;
    assert!(refused(&out) && reason(&out).contains("cc99"), "{out}");
    assert!(!fx.cc().verbs().contains(&"set".to_string()));
}

#[tokio::test]
async fn test_cd_says_when_scope_claims_are_unavailable() {
    let fx = Fx::new();
    let out = fx.run("cd", json!({"_caller_pid": ME, "path": "/w/projects/awm/new"})).await;
    assert_eq!(out["claim"], "unavailable");
}

#[tokio::test]
async fn test_a_lost_event_raises_the_alarm_and_closes_its_ticket() {
    let fx = Fx::new();
    fx.run("compact", json!({"_caller_pid": ME, "followup": "go on"})).await;
    let out = fx.run("harness_event", json!({"session_id": my_id(), "ticket": "t-1", "kind": "lost", "reason": "worker swapped twice"})).await;
    assert!(ok(&out) && out["alarm"]["journal"] == true, "{out}");
    assert!(fx.app.open_tickets().await.is_empty());
    let lost = fx.app.recent_lost().await;
    assert_eq!(lost.len(), 1);
    assert!(lost[0]["reason"] == "worker swapped twice" && lost[0]["command"] == "/compact", "{}", lost[0]);
    let posts = fx.posts();
    assert_eq!(posts.len(), 1);
    assert_eq!((&posts[0]["project"], &posts[0]["scope"]), (&json!("awm"), &json!("redux-harness")));
    fx.app.settle().await;
}

#[tokio::test]
async fn test_a_done_event_closes_its_ticket_quietly() {
    let fx = Fx::new();
    fx.run("compact", json!({"_caller_pid": ME, "followup": "go on"})).await;
    assert_eq!(fx.run("harness_event", json!({"session_id": my_id(), "ticket": "t-1", "kind": "done"})).await, json!({"ok": true}));
    assert!(fx.app.open_tickets().await.is_empty() && fx.app.recent_lost().await.is_empty());
    fx.app.settle().await;
}

#[tokio::test]
async fn test_an_event_that_beats_its_ticket_keeps_it_closed() {
    let fx = Fx::new();
    fx.run("harness_event", json!({"session_id": my_id(), "ticket": "t-1", "kind": "done"})).await;
    fx.app.settle().await;
    fx.run("compact", json!({"_caller_pid": ME, "followup": "go on"})).await;
    assert!(fx.app.open_tickets().await.is_empty());
}

#[tokio::test]
async fn test_the_sweep_alarms_only_for_a_ticket_its_plugin_dropped() {
    use crate::ledger::Ticket;
    let fx = Fx::new();
    for t in ["held", "dropped", "unasked"] {
        let tool = if t == "unasked" { "harness-ds" } else { "harness-cc" };
        let ticket = Ticket { ticket: t.into(), session_id: my_id(), tool: tool.into(), command: "/compact".into(), followup: "go on".into(), ..Ticket::default() };
        fx.app.open_ticket(&ticket).await;
    }
    let later = 10_i64.pow(13);
    let held = |tool: String| async move { (tool == "harness-cc").then(|| vec!["held".to_string()]) };
    assert_eq!(fx.app.sweep(held, later).await, ["dropped"]);
    let open: BTreeSet<String> = fx.app.open_tickets().await.iter().map(|t| t["ticket"].as_str().unwrap().to_string()).collect();
    assert_eq!(open, BTreeSet::from(["held".to_string(), "unasked".to_string()]));
    assert!(fx.app.recent_lost().await[0]["reason"].as_str().unwrap().contains("no longer holds"));
}

#[tokio::test]
async fn test_a_failure_is_logged_without_the_callers_text() {
    let logs = Captured::default();
    let _guard = logs.install();
    let fx = Fx::new();
    fx.cc().clear();
    fx.run("message_self", json!({"_caller_pid": ME, "text": "secret words"})).await;
    let text = logs.text();
    assert!(!text.contains("secret words"), "{text}");
    assert!(text.contains("message_self failed for caller pid 4242"), "{text}");
}

/// No provider is named in reflection: the table lists them, each says its code.
#[tokio::test]
async fn test_the_providers_come_from_the_live_contract_table() {
    let fx = Fx::new();
    *fx.gw().rows.lock().unwrap() = Some(Ok(vec![
        json!({"name": "harness-cc", "major": 1, "plugin": "cch", "peer": null}),
        json!({"name": "harness-ds", "major": 2, "plugin": "dsh", "peer": null}),
        json!({"name": "harness-x", "major": 1, "plugin": "x", "peer": "altair"}),
    ]));
    {
        let mut codes = fx.gw().codes.lock().unwrap();
        codes.remove("harness-ds");
        codes.insert("harness-x".into(), "ds".into());
    }
    assert_eq!(fx.app.providers().await, [Provider::new("harness-cc", Some("cc"), None), Provider::new("harness-x", Some("ds"), Some("altair"))]);
    assert_eq!(fx.gw().table_reads.load(std::sync::atomic::Ordering::SeqCst), 1);
    assert_eq!(crate::providers::CONTRACT, "harness@1");
}

#[tokio::test]
async fn test_no_contract_table_is_no_provider() {
    let fx = Fx::new();
    *fx.gw().rows.lock().unwrap() = Some(Err("refused".into()));
    assert!(fx.app.providers().await.is_empty());
    assert!(fx.app.for_platform("cc").await.is_err());
}

#[tokio::test]
async fn test_an_isolated_caller_is_named_by_its_session_id() {
    let fx = Fx::new();
    let out = fx.run("whoami", json!({"_caller_session_id": my_id()})).await;
    assert_eq!(out["session_id"], json!(my_id()));
    assert!(fx.cc().calls_of("status").contains(&json!({"session_id": my_id()})));
    assert!(!fx.cc().verbs().contains(&"resolve".to_string()));
    fx.run("compact", json!({"_caller_session_id": my_id()})).await;
    let (verb, args) = fx.cc().last();
    assert!(verb == "compact" && args["session_id"] == json!(my_id()) && args.get("pid").is_none(), "{args}");
}

#[tokio::test]
async fn test_hint_asks_each_peer_and_names_the_holder() {
    let fx = Fx::new();
    fx.set_peers(&["altair", "deneb", "mira"]);
    fx.gw().peer_answer("altair", "list_other", Ok(json!({"sessions": [{"job": "67212311", "name": "x"}]})));
    fx.gw().peer_answer("deneb", "list_other", Ok(json!({"sessions": [{"job": "11111111", "name": "y"}]})));
    fx.gw().peer_answer("mira", "list_other", Err("asleep".into()));
    assert_eq!(fx.app.hint("67212311").await, ". It runs on altair: call again with peer='altair'");
    assert_eq!(fx.app.holders("Y").await, ["deneb"]);
    assert_eq!(fx.app.hint("nobody").await, "");
}

#[tokio::test]
async fn test_scope_of() {
    let fx = Fx::new();
    let cases: [(Option<&str>, Option<(&str, &str)>); 5] = [
        (Some("/w/projects/awm/redux-harness"), Some(("awm", "redux-harness"))),
        (Some("/w/projects/metasmith/lab/a"), Some(("metasmith", "lab/a"))),
        (Some("/w/projects/awm"), None),
        (Some("/home/tony"), None),
        (None, None),
    ];
    for (cwd, want) in cases {
        let want = want.map(|(p, s)| (p.to_string(), s.to_string()));
        assert_eq!(fx.app.scope_of(cwd).await, want, "{cwd:?}");
    }
}

// --- wait (sessions.md R51) -----------------------------------------------------

#[tokio::test]
async fn test_wait_records_the_wait_then_stops_the_caller_keeping_its_job() {
    let fx = Fx::new();
    let out = fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    assert!(ok(&out) && out["waiting"] == true, "{out}");
    assert_eq!(fx.cc().last(), ("stop".to_string(), json!({"ref": "j1", "pid": ME, "keep": true, "confirm": true})));
    let waits = fx.run("waits", json!({})).await["waits"].clone();
    assert_eq!(waits.as_array().unwrap().len(), 1);
    let row = &waits[0];
    assert_eq!((&row["session_id"], &row["until"], &row["job"]), (&json!(my_id()), &json!([other()]), &json!("j1")));
    assert_eq!((&row["project"], &row["scope"]), (&json!("awm"), &json!("redux-harness")));
}

#[tokio::test]
async fn test_wait_takes_session_uuids_and_refuses_names() {
    let fx = Fx::new();
    let uuid = "22222222-2222-2222-2222-222222222222";
    assert_eq!(fx.run("wait", json!({"_caller_pid": ME, "until": [uuid]})).await["until"], json!([other()]));
    let out = fx.run("wait", json!({"_caller_pid": ME, "until": ["builder"]})).await;
    assert!(refused(&out) && reason(&out).contains("session ids"), "{out}");
}

#[tokio::test]
async fn test_a_wait_whose_stop_fails_is_not_left_behind() {
    let fx = Fx::new();
    fx.cc().answer("stop", json!({"ok": false, "reason": "claude stop failed"}));
    let out = fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    assert!(refused(&out));
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
}

#[tokio::test]
async fn test_a_terminal_session_waits_without_stopping() {
    let fx = Fx::new();
    let mut me = fx.cc().row("j1").unwrap();
    me["job"] = Value::Null;
    fx.cc().set("j1", me);
    let out = fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    assert!(ok(&out) && out["waiting"] == true && out["terminal"] == true, "{out}");
    assert!(!fx.cc().verbs().contains(&"stop".to_string()));
    fx.run("retired", json!({"session_id": other()})).await;
    let (verb, args) = fx.cc().last();
    assert_eq!(verb, "send");
    assert_eq!(args["session_id"], json!(my_id()));
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
}

#[tokio::test]
async fn test_the_last_retire_resumes_the_waiter_with_the_wake_line() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other(), third()]})).await;
    fx.cc().set("w2", json!({"session_id": other(), "name": "w2", "job": "w2"}));
    fx.cc().answer("stop", json!({"ok": true, "session_id": other()}));
    assert_eq!(fx.run("stop", json!({"job": "w2"})).await["woken"], json!([]));
    assert!(!fx.cc().verbs().contains(&"resume".to_string()));
    assert_eq!(fx.run("waits", json!({})).await["waits"][0]["retired"], json!([other()]));
    fx.cc().answer("stop", json!({"ok": true, "session_id": third()}));
    assert_eq!(fx.run("stop", json!({"job": third()})).await["woken"], json!([my_id()]));
    let (verb, args) = fx.cc().last();
    assert!(verb == "resume" && args["ref"] == "j1" && args["prompt"].as_str().unwrap().contains(&other()), "{args}");
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
    fx.app.settle().await;
}

#[tokio::test]
async fn test_a_waiter_that_itself_waits_has_not_finished() {
    use crate::waits::NewWait;
    let fx = Fx::new();
    fx.app.add_wait(NewWait { session_id: other(), until: vec![third()], tool: "harness-cc".into(), job: Some("w2".into()), ..NewWait::default() }).await;
    fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    fx.run("retired", json!({"session_id": third()})).await;
    let rows = fx.run("waits", json!({})).await["waits"].clone();
    let rows = rows.as_array().unwrap();
    assert!(!rows.iter().any(|w| w["session_id"] == json!(other())));
    let mine = rows.iter().find(|w| w["session_id"] == json!(my_id())).unwrap();
    assert_eq!(mine["retired"], json!([]));
}

#[tokio::test]
async fn test_a_session_already_retired_is_not_waited_on() {
    let fx = Fx::new();
    fx.app.retire(&other(), true, no_wake).await;
    let out = fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    assert!(ok(&out) && out["waiting"] == false, "{out}");
    assert!(!fx.cc().verbs().contains(&"stop".to_string()));
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
}

#[tokio::test]
async fn test_a_failed_wake_raises_an_alarm_and_keeps_the_wait() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    fx.cc().answer("resume", json!({"ok": false, "reason": "no transcript"}));
    assert_eq!(fx.run("retired", json!({"session_id": other()})).await["woken"], json!([]));
    assert!(fx.app.recent_lost().await[0]["reason"].as_str().unwrap().contains("no transcript"));
    assert_eq!(fx.run("waits", json!({})).await["waits"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn test_a_retiring_waiter_clears_its_wait() {
    use crate::waits::NewWait;
    let fx = Fx::new();
    fx.app.add_wait(NewWait { session_id: other(), until: vec![third()], tool: "harness-cc".into(), job: Some("w2".into()), ..NewWait::default() }).await;
    fx.app.retire(&other(), true, no_wake).await;
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
}

#[tokio::test]
async fn test_a_resume_by_hand_clears_the_wait() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    fx.run("resume_other", json!({"job": "j1"})).await;
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
}

#[tokio::test]
async fn test_a_retire_tells_every_peer() {
    let fx = Fx::new();
    fx.set_peers(&["altair", "alcor"]);
    fx.app.tell_peers(&other()).await;
    let told: Vec<(String, String, Value)> = fx.gw().peer_calls();
    assert_eq!(
        told,
        [
            ("altair".to_string(), "retired".to_string(), json!({"session_id": other()})),
            ("alcor".to_string(), "retired".to_string(), json!({"session_id": other()})),
        ]
    );
}

#[tokio::test]
async fn test_the_start_check_alarms_once_for_a_lost_session() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other(), third()]})).await;
    let (o, t) = (other(), third());
    let state = |sid: String| {
        let (o, t) = (o.clone(), t.clone());
        async move {
            if sid == o {
                Some("gone".to_string())
            } else if sid == t {
                Some("live".to_string())
            } else {
                None
            }
        }
    };
    let lost = fx.app.check_waits(state, |_ids| async { vec![] }, no_wake).await;
    assert_eq!(lost, [other()]);
    assert!(fx.app.recent_lost().await[0]["reason"].as_str().unwrap().contains(&other()));
    assert_eq!(fx.run("waits", json!({})).await["waits"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn test_the_start_check_counts_a_retire_it_missed() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    let woke = Arc::new(Mutex::new(vec![]));
    let w = woke.clone();
    let wake = move |row: Value| {
        w.lock().unwrap().push(row["session_id"].as_str().unwrap().to_string());
        async { Ok(json!({"ok": true})) }
    };
    let o = other();
    let state = |_sid: String| async { panic!("asked") };
    fx.app.check_waits(state, move |_ids| {
        let o = o.clone();
        async move { vec![o] }
    }, wake).await;
    assert_eq!(*woke.lock().unwrap(), [my_id()]);
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
}

/// operations' task_for_session reads session_id (sessions.md R57 step 2).
#[tokio::test]
async fn test_whoami_asks_for_the_task_by_session_id() {
    let fx = Fx::new();
    assert_eq!(fx.app.task_of(&my_id()).await, json!("task-9"));
    assert_eq!(fx.gw().calls_to("operations"), [("task_for_session".to_string(), json!({"session_id": my_id()}))]);
}

#[tokio::test]
async fn test_session_of_gives_the_gate_the_mode_and_the_trust() {
    let fx = Fx::new();
    let mut me = fx.cc().row("j1").unwrap();
    me["mode"] = json!("worker");
    fx.cc().set("j1", me);
    let out = fx.run("session_of", json!({"pid": ME})).await;
    assert!(out["known"] == true && out["session_id"] == json!(my_id()), "{out}");
    assert_eq!((&out["mode"], &out["trust"]), (&json!("worker"), &json!("trusted")));
}

/// Tony's own terminal sessions have no ledger entry: the gateway must not lock them out now
/// that a failed lookup is untrusted (fleet.md R103 item 2).
#[tokio::test]
async fn test_a_hand_started_session_reads_as_trusted_and_a_recorded_one_keeps_its_level() {
    let fx = Fx::new();
    assert_eq!(fx.run("session_of", json!({"pid": ME})).await["trust"], "trusted");
    fx.app.record_trust(&my_id(), "cooperative", "test", None).await;
    assert_eq!(fx.run("session_of", json!({"pid": ME})).await["trust"], "cooperative");
}

#[tokio::test]
async fn test_session_of_a_pid_no_plugin_claims_carries_the_plugins_mode() {
    let fx = Fx::new();
    fx.cc().answer("resolve", json!({"ok": true, "mine": false, "mode": null}));
    let out = fx.run("session_of", json!({"pid": 77})).await;
    assert!(ok(&out) && out["known"] == false && out["mode"].is_null(), "{out}");
}

#[tokio::test]
async fn test_session_of_with_no_plugin_answering_is_unknown() {
    let fx = Fx::new();
    fx.cc().down.store(true, std::sync::atomic::Ordering::SeqCst);
    fx.ds().down.store(true, std::sync::atomic::Ordering::SeqCst);
    let out = fx.run("session_of", json!({"pid": 77})).await;
    assert!(out["known"] == false && out["mode"] == "unknown", "{out}");
}

#[test]
fn test_the_hint_asks_only_the_nodes_this_node_reaches() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("edges.yml");
    std::fs::write(
        &path,
        "nodes:\n  here: {role: homebase}\n  altair: {role: homebase}\n  deneb: {role: homebase}\n  mira: {role: homebase}\n\
         edges:\n  here: {altair: lan, mira: lan}\n  deneb: {here: lan}\n",
    )
    .unwrap();
    let edges = EdgeList::load(&path).unwrap();
    assert_eq!(crate::peers::reached_peers(&edges, "here"), ["altair", "mira"]);
}

#[tokio::test]
async fn test_start_other_claims_its_scope_for_the_new_session() {
    let fx = Fx::new();
    fx.gw().use_claims();
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"})).await;
    assert!(ok(&out) && out["claim"]["claimed"] == true, "{out}");
    let held = fx.gw().with_claims(|c| c.holder("awm", "x"));
    assert_eq!(held, Some(json!({"session_id": new_id(), "pid": NEW_PID, "job": "abcd1234"})));
    assert!(fx.cc().calls_of("status").contains(&json!({"session_id": new_id()})));
}

#[tokio::test]
async fn test_a_start_into_a_held_scope_is_refused_before_it_launches() {
    let fx = Fx::new();
    fx.gw().use_claims();
    fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"})).await;
    let starts = fx.cc().calls_of("start").len();
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "name": "two"})).await;
    assert!(refused(&out) && reason(&out).contains(&new_id()), "{out}");
    assert_eq!(fx.cc().calls_of("start").len(), starts);
}

#[tokio::test]
async fn test_a_start_that_loses_the_claim_stops_the_new_session() {
    let fx = Fx::new();
    fx.gw().use_claims();
    fx.gw().with_claims(|c| {
        c.unlisted = true;
        c.hold("awm", "x", json!({"session_id": format!("cc{}", "9".repeat(32)), "pid": 1, "job": "other"}));
    });
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"})).await;
    assert!(refused(&out) && out["stopped"] == "abcd1234", "{out}");
    let (verb, args) = fx.cc().last();
    assert!(verb == "stop" && args["session_id"] == json!(new_id()) && args["keep"] == false, "{args}");
}

#[tokio::test]
async fn test_a_lobby_start_claims_nothing() {
    for spec in [json!({"project": "awm"}), json!({"project": "awm", "scope": "x", "suggest": true})] {
        let fx = Fx::new();
        fx.gw().use_claims();
        let out = fx.run("start_other", crate::app::merged(&json!({"_caller_pid": ME}), &spec)).await;
        assert!(ok(&out) && out.get("claim").is_none(), "{spec}: {out}");
        assert!(fx.gw().with_claims(|c| c.calls.is_empty()), "{spec}");
    }
}

#[tokio::test]
async fn test_a_stop_releases_the_sessions_claims() {
    for verb in ["stop", "stop_other"] {
        let fx = Fx::new();
        fx.gw().use_claims();
        fx.gw().with_claims(|c| {
            c.hold("awm", "x", json!({"session_id": my_id(), "pid": ME, "job": "j1"}));
            c.hold("awm", "y", json!({"session_id": format!("cc{}", "9".repeat(32)), "pid": 1, "job": "other"}));
        });
        fx.cc().answer("stop", json!({"ok": true, "job": "j1", "session_id": my_id()}));
        let out = fx.run(verb, json!({"_caller_pid": ME, "job": "j1", "confirm": true})).await;
        assert!(ok(&out), "{verb}: {out}");
        let held: Vec<(String, String)> = fx.gw().with_claims(|c| c.held.iter().map(|(k, _)| k.clone()).collect());
        assert_eq!(held, [("awm".to_string(), "y".to_string())], "{verb}");
        fx.app.settle().await;
    }
}

#[tokio::test]
async fn test_start_other_without_model_and_effort_is_refused_before_it_launches() {
    let fx = Fx::new();
    for missing in [json!({"effort": "high"}), json!({"model": "opus"}), json!({})] {
        let args = crate::app::merged(&json!({"_caller_pid": ME, "project": "awm", "scope": "x"}), &missing);
        let out = fx.app.handle("start_other", &Frame::from_args(&args, None, None), &args).await;
        assert!(refused(&out) && reason(&out).contains("model and effort are required"), "{missing}: {out}");
    }
}

#[tokio::test]
async fn test_effort_above_high_is_refused_on_start_and_on_the_effort_verb() {
    for level in ["xhigh", "max"] {
        let fx = Fx::new();
        let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "model": "opus", "effort": level})).await;
        assert!(refused(&out) && reason(&out).contains("upper limit"), "{level}: {out}");
        assert!(reason(&fx.run("effort", json!({"level": level})).await).ends_with("upper limit for any model."), "{level}");
    }
}
