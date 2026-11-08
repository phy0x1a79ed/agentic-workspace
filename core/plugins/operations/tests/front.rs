//! The session verbs (queue.md R145): request_human, the urgent DM, Tony's prompt, the hooks,
//! approvals, Arbutus and intake, and the service's caller gates. Ported from test_front.py
//! and test_plugin_callers.py with the approved revision: card_url for panel_url, approvals
//! on the requester's own card, intake in Needs you, ops_notes and jobs on session rows.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use awm_plugin_operations::arbutus::Approvals;
use awm_plugin_operations::front::{Front, Stamp};
use awm_plugin_operations::home::LocalClient;
use awm_plugin_operations::relay::Relay;
use awm_plugin_operations::service::{self, Ops};
use awm_plugin_operations::store::Open;
use common::{s, Env, FakeArbutus, FakeOutside, TestClock, A, B, ORIGIN};
use serde_json::{json, Map, Value};

const CARD_URL: &str = "https://board.example.test/card/{task_id}";

struct H {
    env: Env,
    front: Arc<Front>,
    out: Arc<FakeOutside>,
    clock: TestClock,
}

fn harness_with(arbutus: Option<Arc<Approvals>>, card_url: Option<&str>) -> H {
    let env = Env::new();
    let relay = Arc::new(Relay::open("mizar", &env.path("r.db"), Arc::new(LocalClient { home: env.home.clone() })).unwrap());
    let out = FakeOutside::new();
    let clock = TestClock::at(1000.0);
    let front = Arc::new(Front {
        relay, node: "mizar".into(), outside: out.clone(), arbutus, card_url: card_url.map(str::to_string),
        approver_origin: ORIGIN.into(), remind_s: 60.0, clock: clock.clock(),
    });
    H { env, front, out, clock }
}

fn harness() -> H {
    harness_with(None, Some(CARD_URL))
}

fn pid(p: u32) -> Stamp {
    Stamp { pid: Some(p), ..Stamp::default() }
}

/// The claim alcor's gateway stamps on a door call: the session as its node sent it.
fn door(session: &str, node: &str) -> Stamp {
    Stamp { claim: Some(json!({"session_id": session, "node": node, "job": "job-d", "name": "remote-work"})), ..Stamp::default() }
}

fn none() -> Stamp {
    Stamp::default()
}

impl H {
    async fn call(&self, verb: &str, st: Stamp, args: Value) -> Value {
        self.front.call(verb, &args, &st).await
    }

    fn card(&self, t: &Value) -> Value {
        self.env.store.task_get(&s(t)).unwrap()
    }

    fn ops(&self) -> Ops {
        Ops { front: self.front.clone(), home: Some(self.env.home.clone()) }
    }
}

fn ok(v: &Value) -> bool {
    v["ok"] == json!(true)
}

#[tokio::test]
async fn blocked_opens_a_card_and_urgent_sends_a_dm() {
    let h = harness();
    let out = h.call("request_human", pid(11), json!({"kind": "blocked", "reason": "needs the VPN key", "urgent": true})).await;
    assert!(ok(&out), "{out}");
    assert_eq!((s(&out["section"]), s(&out["status"])), ("needs_you".into(), "blocked".into()));
    assert_eq!(out["notified"], true);
    let dm = &h.out.dms()[0];
    assert!(dm.contains("needs the VPN key") && dm.contains("fix-tunnel") && dm.contains("job-a"), "{dm}");
    let tid = s(&out["task_id"]);
    assert_eq!(out["card_url"], json!(format!("https://board.example.test/card/{tid}")));
    assert!(out.get("panel_url").is_none());
    let got = h.call("task_get", none(), json!({"task_id": tid})).await;
    assert_eq!(got["card_url"], out["card_url"]);
    assert_eq!(got["task"]["task_id"], json!(tid));
    let card = h.card(&out["task_id"]);
    assert_eq!(card["meta"]["reason"], "needs the VPN key");
    assert_eq!((s(&card["sessions"][0]["session_id"]), s(&card["sessions"][0]["job"])), (A.into(), "job-a".into()),
               "a write from a caller with a job records it");
    let out = h.call("request_human", pid(11), json!({"kind": "review", "summary": "look at the diff"})).await;
    assert_eq!(out["status"], "review");
    assert_eq!(h.out.dms().len(), 1);
}

#[tokio::test]
async fn card_url_is_null_when_the_board_link_is_unset() {
    let h = harness_with(None, None);
    let out = h.call("request_human", pid(11), json!({"kind": "blocked", "reason": "x"})).await;
    assert!(ok(&out) && out["card_url"].is_null(), "{out}");
    let got = h.call("task_get", none(), json!({"task_id": out["task_id"]})).await;
    assert!(got["card_url"].is_null());
}

#[tokio::test]
async fn request_human_needs_a_known_kind_and_its_text() {
    let h = harness();
    assert!(!ok(&h.call("request_human", pid(11), json!({"kind": "sideways", "reason": "x"})).await));
    let e = h.call("request_human", pid(11), json!({"kind": "blocked"})).await;
    assert_eq!(e["error"], "reason is required");
    let e = h.call("request_human", pid(11), json!({"kind": "review", "summary": "  "})).await;
    assert_eq!(e["error"], "summary is required");
    let review = h.call("request_human", pid(11), json!({"summary": "a summary alone is a review"})).await;
    assert_eq!(review["status"], "review");
}

#[tokio::test]
async fn a_service_with_no_session_gets_a_card_per_call() {
    let h = harness();
    let a = h.call("request_human", none(), json!({"kind": "blocked", "reason": "disk full on lyra"})).await;
    let b = h.call("request_human", none(), json!({"kind": "blocked", "reason": "disk full on lyra"})).await;
    assert_ne!(a["task_id"], b["task_id"]);
    assert_eq!(h.card(&a["task_id"])["title"], "disk full on lyra");
}

#[tokio::test]
async fn tonys_prompt_moves_the_card_to_working() {
    let h = harness();
    let out = h.call("request_human", pid(11), json!({"kind": "blocked", "reason": "which port?"})).await;
    assert_eq!(h.call("hook_prompt", pid(22), json!({"prompt": "hi"})).await["moved"], false);
    let moved = h.call("hook_prompt", pid(11), json!({"prompt": "use 2201"})).await;
    assert_eq!((moved["moved"].clone(), moved["blocked"].clone()), (json!(true), json!(false)));
    assert_eq!(h.card(&out["task_id"])["section"], "working");
    assert_eq!(h.call("hook_prompt", pid(11), json!({"prompt": "thanks"})).await["moved"], false);
}

#[tokio::test]
async fn report_needs_a_task_and_a_known_state() {
    let h = harness();
    assert!(!ok(&h.call("task_report", pid(11), json!({"state": "done"})).await));
    let t = h.call("task_open", pid(11), json!({"title": "x"})).await["task_id"].clone();
    assert!(!ok(&h.call("task_report", pid(11), json!({"state": "sideways"})).await));
    let done = h.call("task_report", pid(11), json!({"state": "done"})).await;
    assert_eq!((done["task_id"].clone(), done["section"].clone()), (t, json!("history")));
}

#[tokio::test]
async fn join_tells_the_main_session() {
    let h = harness();
    let t = h.call("task_open", pid(11), json!({"title": "main work", "name": "fix-tunnel"})).await["task_id"].clone();
    let out = h.call("task_join", pid(22), json!({"task_id": t})).await;
    assert_eq!((s(&out["main_session_id"]), out["main_notified"].clone()), (A.into(), json!(true)));
    let told = &h.out.told()[0];
    assert_eq!((told.0.as_deref(), told.1.as_str()), (Some("mizar"), "fix-tunnel"));
    assert!(told.2.contains(B));
    let joined = h.card(&t)["sessions"].as_array().unwrap().iter().find(|x| x["session_id"] == B).cloned().unwrap();
    assert_eq!((s(&joined["role"]), s(&joined["job"])), ("joined".into(), "job-b".into()));
}

#[tokio::test]
async fn only_the_requesting_session_cancels() {
    let h = harness();
    let r = h.call("approval_request", pid(11), json!({"kind": "vault_hold", "reason": "r", "session_id": B})).await;
    assert!(ok(&r), "{r}");
    let rid = s(&r["request_id"]);
    assert_eq!(r["modal_url"], json!(format!("{ORIGIN}/modal/approve/{rid}")));
    assert_eq!(h.env.ap.approval_get(&rid).unwrap()["requester_session"], A, "a named session is ignored");
    assert!(!ok(&h.call("approval_cancel", pid(22), json!({"request_id": rid})).await));
    assert!(!ok(&h.call("approval_cancel", none(), json!({"request_id": rid})).await));
    let lie = h.call("approval_cancel", pid(22), json!({"request_id": rid, "session_id": A})).await;
    assert!(!ok(&lie) && s(&lie["error"]).contains("does not match"), "{lie}");
    let out = h.call("approval_cancel", pid(11), json!({"request_id": rid})).await;
    assert!(ok(&out) && out["status"] == "cancelled", "{out}");
}

#[tokio::test]
async fn an_approval_tags_the_requesters_own_card() {
    let h = harness();
    let t = h.call("track_task", pid(11), json!({"title": "cutover"})).await["task_id"].clone();
    let r1 = h.call("approval_request", pid(11), json!({"kind": "vault_hold", "reason": "checks tripped"})).await;
    assert_eq!(r1["task_id"], t, "no card of its own");
    let card = h.card(&t);
    assert_eq!((s(&card["section"]), s(&card["status"])), ("needs_you".into(), "approval".into()));
    assert_eq!(card["meta"]["reason"], "vault_hold: checks tripped (15 min)");
    let r2 = h.call("approval_request", pid(11), json!({"kind": "vault_hold", "reason": "again"})).await;
    assert_eq!(r2["task_id"], t);
    assert_eq!(h.env.store.task_list(None, None, None, None).unwrap()["cards"].as_array().unwrap().len(), 1);
    h.call("approval_cancel", pid(11), json!({"request_id": r1["request_id"]})).await;
    assert_eq!(h.card(&t)["section"], "needs_you", "the second request still waits");
    h.call("approval_cancel", pid(11), json!({"request_id": r2["request_id"]})).await;
    let card = h.card(&t);
    assert_eq!((s(&card["section"]), card["status"].clone()), ("working".into(), Value::Null));
    let a = h.env.ap.approvals_of(&s(&t));
    assert_eq!(a.iter().map(|x| s(&x["state"])).collect::<Vec<_>>(), ["cancelled", "cancelled"]);
}

#[tokio::test]

async fn a_session_id_that_is_no_r9_id_is_refused() {
    let h = harness();
    let bad = h.call("task_for_session", none(), json!({"session_id": "job0001"})).await;
    assert!(!ok(&bad), "{bad}");
}

#[tokio::test]
async fn a_claude_uuid_names_its_r9_session() {
    let h = harness();
    let uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    let out = h.call("task_open", none(), json!({"title": "from a service", "session_id": uuid})).await;
    assert_eq!(h.card(&out["task_id"])["sessions"][0]["session_id"], A);
    let got = h.call("task_for_session", none(), json!({"session_id": A})).await;
    assert_eq!(got["task_id"], out["task_id"]);
    let again = h.call("task_for_session", none(), json!({"session_id": uuid.to_uppercase()})).await;
    assert_eq!(again["task_id"], out["task_id"]);
}

const BUNDLE: &str = "reason\tGrow altair's root\nminutes\t60\nssh\tshaula-admin\n";

#[tokio::test]
async fn arbutus_approval_blocks_the_card_until_slack_decides() {
    let h = harness();
    assert!(!ok(&h.call("request_human", pid(11), json!({"kind": "approval", "bundle": "grow"})).await), "no Arbutus here");
    let dir = tempfile::tempdir().unwrap();
    let (pending, bundles) = (dir.path().join("pending"), dir.path().join("bundles"));
    std::fs::create_dir_all(&bundles).unwrap();
    std::fs::write(bundles.join("grow.bundle"), BUNDLE).unwrap();
    let io = FakeArbutus::new(0);
    let arb = Arc::new(Approvals::with_dirs(io.clone(), pending.clone(), bundles));
    let h = harness_with(Some(arb), Some(CARD_URL));
    let out = h.call("request_human", pid(11), json!({"kind": "approval", "bundle": "grow"})).await;
    assert!(ok(&out), "{out}");
    let r = s(&out["approval"]);
    assert_eq!(out["status"], "blocked");
    let req: Value = serde_json::from_str(&std::fs::read_to_string(pending.join(&r).join("request.json")).unwrap()).unwrap();
    assert_eq!((req["task"].clone(), s(&req["agent"]), s(&req["job"]), s(&req["node"])), (out["task_id"].clone(), "fix-tunnel".into(), "job-a".into(), "mizar".into()));
    assert!(s(&h.card(&out["task_id"])["meta"]["reason"]).contains("Tony or Ryan"));
    assert_eq!(io.posts().len(), 1);
    h.front.arbutus_decided(&s(&out["task_id"]), "Approved by ryan in Slack.").await;
    assert_eq!(h.card(&out["task_id"])["section"], "working");
}

#[tokio::test]
async fn an_isolated_session_is_known_by_the_id_the_door_stamped() {
    let h = harness();
    let st = Stamp { session_id: Some(B.into()), ..Stamp::default() };
    let out = h.call("task_open", st, json!({"title": "boxed work"})).await;
    assert_eq!(h.card(&out["task_id"])["sessions"][0]["session_id"], B);
}

#[tokio::test]
async fn a_session_through_the_door_acts_on_its_own_card() {
    let h = harness();
    let d = format!("cc{}", "d".repeat(32));
    let out = h.call("request_human", door(&d, "lyra"), json!({"kind": "blocked", "reason": "needs a key", "urgent": true})).await;
    assert!(ok(&out) && out["status"] == "blocked", "{out}");
    let card = h.card(&out["task_id"]);
    let ses = &card["sessions"][0];
    assert_eq!((s(&ses["session_id"]), s(&ses["node"]), s(&ses["job"])), (d.clone(), "lyra".into(), "job-d".into()));
    let dm = &h.out.dms()[0];
    assert!(dm.contains("on **lyra**") && dm.contains("job-d` on lyra"), "{dm}");
    let plan = h.call("hook_plan", door(&d, "lyra"), json!({"plan_text": "# Next"})).await;
    assert_eq!((plan["task_id"].clone(), plan["opened"].clone()), (out["task_id"].clone(), json!(false)));
    let new = format!("cc{}", "e".repeat(32));
    let cleared = h.call("hook_session_clear", door(&new, "lyra"), json!({"new_session_id": new})).await;
    assert_eq!((s(&cleared["ended"]), cleared["task_id"].clone()), (d, out["task_id"].clone()));
}

#[tokio::test]
async fn a_door_claim_from_this_node_records_no_other_node() {
    let h = harness();
    let d = format!("cc{}", "d".repeat(32));
    let out = h.call("track_task", door(&d, "mizar"), json!({"title": "here"})).await;
    assert_eq!(h.card(&out["task_id"])["sessions"][0]["node"], "mizar");
}

#[tokio::test]
async fn intake_refuses_a_session_through_the_door() {
    let h = harness();
    let d = format!("cc{}", "d".repeat(32));
    let out = h.call("intake_post", door(&d, "lyra"), json!({"board_card": "b1", "sender": "x", "title": "t"})).await;
    assert!(!ok(&out) && s(&out["error"]).contains("not a session's"), "{out}");
    let out = h.call("intake_message", pid(11), json!({"board_card": "b1", "sender": "x", "body": "t"})).await;
    assert!(!ok(&out));
}

#[tokio::test]
async fn a_plan_opens_a_planned_card_and_approval_moves_it_to_working() {
    let h = harness();
    let out = h.call("hook_plan", pid(11), json!({"session_id": A, "plan_text": "# Fix the tunnel\n\nsteps"})).await;
    assert_eq!((out["opened"].clone(), out["blocked"].clone()), (json!(true), json!(true)));
    let tid = s(&out["task_id"]);
    assert!(s(&out["notice"]).contains(&tid));
    let card = h.card(&out["task_id"]);
    assert_eq!((s(&card["title"]), s(&card["section"]), s(&card["status"])), ("Fix the tunnel".into(), "needs_you".into(), "planned".into()));
    assert!(s(&card["meta"]["plan"]).starts_with("# Fix the tunnel"));
    let again = h.call("hook_plan", pid(11), json!({"session_id": A, "plan_text": "# Fix the tunnel v2"})).await;
    assert_eq!((again["task_id"].clone(), again["opened"].clone()), (out["task_id"].clone(), json!(false)));
    assert!(again.get("notice").is_none());
    let done = h.call("hook_plan_approved", pid(11), json!({"session_id": A, "plan_text": "# Fix the tunnel v3"})).await;
    let card = h.card(&out["task_id"]);
    assert_eq!((done["blocked"].clone(), s(&card["section"])), (json!(false), "working".into()));
    assert_eq!(card["meta"]["plan"], "# Fix the tunnel v3");
    assert_eq!(h.call("hook_prompt", pid(11), json!({"prompt": "go"})).await["moved"], false);
}

#[tokio::test]
async fn a_plan_with_no_heading_takes_the_sessions_name() {
    let h = harness();
    let out = h.call("hook_plan", pid(11), json!({"session_id": A, "plan_text": "no heading here"})).await;
    assert_eq!(h.card(&out["task_id"])["title"], "fix-tunnel");
}

#[tokio::test]
async fn todos_show_on_the_card_and_need_one() {
    let h = harness();
    let items = json!([{"id": "1", "subject": "port hooks", "status": "in_progress"}]);
    assert_eq!(h.call("hook_todos", pid(11), json!({"session_id": A, "items": items})).await["tracked"], false);
    let t = h.call("track_task", pid(11), json!({"title": "cutover"})).await["task_id"].clone();
    assert_eq!(h.call("hook_todos", pid(11), json!({"session_id": A, "items": items})).await["tracked"], true);
    assert_eq!(h.card(&t)["meta"]["todos"], items);
    assert!(!ok(&h.call("hook_todos", pid(11), json!({"session_id": A, "items": "x"})).await));
}

#[tokio::test]
async fn track_task_returns_the_card_or_opens_one() {
    let h = harness();
    let first = h.call("track_task", pid(11), json!({"title": "cutover work"})).await;
    assert!(first["opened"] == true && first.get("notice").is_some(), "{first}");
    assert_eq!(h.card(&first["task_id"])["section"], "working");
    let again = h.call("track_task", pid(11), json!({"title": "other"})).await;
    assert_eq!(again, json!({"ok": true, "task_id": first["task_id"], "opened": false}));
}

#[tokio::test]
async fn a_clear_takes_the_old_session_off_its_card() {
    let h = harness();
    let t = h.call("track_task", pid(11), json!({"title": "before the clear"})).await["task_id"].clone();
    let new = format!("cc{}", "c".repeat(32));
    h.out.pids.lock().unwrap().insert(33, json!({"ok": true, "session_id": new, "job": "job-a", "name": "fix-tunnel"}));
    let out = h.call("hook_session_clear", pid(33), json!({"new_session_id": new})).await;
    assert_eq!((s(&out["ended"]), out["task_id"].clone()), (A.into(), t.clone()));
    let active: HashMap<String, bool> = h.env.store.task_sessions(&s(&t)).unwrap().iter().map(|x| (s(&x["session_id"]), x["active"].as_bool().unwrap())).collect();
    assert_eq!(active, HashMap::from([(A.to_string(), false)]));
    assert_eq!(h.call("task_for_session", none(), json!({"session_id": new})).await["task_id"], Value::Null);
    let plan = h.call("hook_plan", pid(33), json!({"session_id": new, "plan_text": "# Next"})).await;
    assert!(plan["opened"] == true && plan["task_id"] != t, "{plan}");
}

#[tokio::test]
async fn a_clear_with_no_job_or_card_ends_nothing() {
    let h = harness();
    assert_eq!(h.call("hook_session_clear", none(), json!({"new_session_id": A})).await["ended"], Value::Null);
    assert_eq!(h.call("hook_session_clear", pid(11), json!({"new_session_id": A})).await["ended"], Value::Null);
}

#[tokio::test]
async fn claim_moves_a_needs_you_card_to_working() {
    let h = harness();
    let o = Open { kind: Some("note".into()), section: Some("needs_you".into()), ..Open::default() };
    let t = s(&h.env.store.task_open("idea", o, None).unwrap()["task_id"]);
    assert!(!ok(&h.call("claim_task", pid(11), json!({})).await));
    let out = h.call("claim_task", pid(11), json!({"task_id": format!("claim task {t}")})).await;
    assert!(ok(&out) && out["section"] == "working", "{out}");
    let card = h.env.store.task_get(&t).unwrap();
    assert_eq!((s(&card["kind"]), s(&card["sessions"][0]["role"]), s(&card["sessions"][0]["job"])), ("task".into(), "main".into(), "job-a".into()));
    h.env.store.task_report(&t, Some(A), "done", None, None, None).unwrap();
    assert!(!ok(&h.call("claim_task", pid(22), json!({"task_id": t})).await), "History is not claimable");
}

#[tokio::test]
async fn messages_post_and_read_on_a_card() {
    let h = harness();
    let t = h.call("track_task", pid(11), json!({"title": "deploy"})).await["task_id"].clone();
    let out = h.call("swarm_message_post", none(), json!({"card_id": t, "body": "deploy passed"})).await;
    assert!(ok(&out) && out["seq"] == 1, "{out}");
    h.call("swarm_message_post", pid(22), json!({"card_id": t, "body": "seen"})).await;
    let got = h.call("swarm_messages", none(), json!({"card_id": t})).await;
    let pairs: Vec<(String, String)> = got["messages"].as_array().unwrap().iter().map(|m| (s(&m["author"]), s(&m["body"]))).collect();
    assert_eq!(pairs, [("a service".to_string(), "deploy passed".to_string()), ("other".to_string(), "seen".to_string())]);
    let later = h.call("swarm_messages", none(), json!({"card_id": t, "after": 1})).await;
    assert_eq!(later["messages"].as_array().unwrap().iter().map(|m| s(&m["body"])).collect::<Vec<_>>(), ["seen"]);
    assert!(!ok(&h.call("swarm_message_post", none(), json!({"card_id": t, "body": " "})).await));
}

#[tokio::test]
async fn an_urgent_block_reminds_tony_once() {
    let h = harness();
    let out = h.call("request_human", pid(11), json!({"kind": "blocked", "reason": "key expired", "urgent": true})).await;
    assert_eq!(h.front.remind_due().await, 0);
    h.clock.advance(61.0);
    assert_eq!(h.front.remind_due().await, 1);
    let last = h.out.dms().last().cloned().unwrap();
    assert!(last.starts_with("Still waiting:") && last.contains("key expired"), "{last}");
    h.clock.advance(3600.0);
    assert_eq!(h.front.remind_due().await, 0);
    assert_eq!(h.out.dms().len(), 2);
    h.call("request_human", pid(11), json!({"kind": "blocked", "reason": "again", "urgent": true})).await;
    h.call("hook_prompt", pid(11), json!({"prompt": "here you go"})).await;
    h.clock.advance(61.0);
    assert_eq!(h.front.remind_due().await, 0);
    assert_eq!(h.card(&out["task_id"])["section"], "working");
}

#[tokio::test]
async fn a_board_post_becomes_one_needs_you_card_and_its_thread_reaches_the_session() {
    let h = harness();
    let post = json!({"board_card": "c0ffee", "sender": "caviar/agent", "title": "help with x", "body": "details"});
    assert!(!ok(&h.call("intake_post", pid(11), post.clone()).await), "sessions cannot post intake");
    let out = h.call("intake_post", none(), post.clone()).await;
    assert_eq!(out["opened"], true);
    let tid = s(&out["task_id"]);
    assert!(h.out.dms().last().unwrap().contains(&format!("claim task {tid}")));
    let card = h.card(&out["task_id"]);
    assert_eq!((s(&card["section"]), s(&card["status"]), card["tags"].clone(), s(&card["title"])),
               ("needs_you".into(), "intake".into(), json!(["board"]), "caviar/agent: help with x".into()));
    assert_eq!(card["meta"], json!({"board_card": "c0ffee", "from": "caviar/agent"}));
    assert_eq!(h.env.store.task_messages(&tid, 0).unwrap().iter().map(|m| s(&m["body"])).collect::<Vec<_>>(), ["details"]);
    let again = h.call("intake_post", none(), post).await;
    assert_eq!(again, json!({"ok": true, "task_id": tid, "opened": false}));
    assert_eq!(h.out.dms().len(), 1);
    h.call("claim_task", pid(11), json!({"task_id": tid})).await;
    let said = h.call("intake_message", none(), json!({"board_card": "c0ffee", "sender": "caviar/agent", "body": "any news?"})).await;
    assert_eq!(said["told"], true);
    assert_eq!(h.out.told().last().unwrap().1, "fix-tunnel");
    let reply = h.call("intake_post", none(), json!({"board_card": "c0ffee2", "sender": "caviar/agent", "title": "re: help", "body": "more", "reply_to": "c0ffee"})).await;
    assert_eq!(reply["task_id"], json!(tid));
    assert_eq!(h.env.store.task_messages(&tid, 0).unwrap().last().unwrap()["body"], "more");
    let nope = h.call("intake_message", none(), json!({"board_card": "nope", "sender": "x", "body": "y"})).await;
    assert_eq!(nope["task_id"], Value::Null);
}

#[tokio::test]
async fn status_and_keys_answer_from_this_node() {
    let h = harness();
    assert_eq!(h.call("status", none(), json!({})).await, json!({"ok": true, "node": "mizar", "outbox": 0, "refused": 0, "alcor_down_since": null}));
    let keys = h.call("keys", none(), json!({})).await;
    assert_eq!(keys["grant_pub"], json!(h.env.ap.keys.grant_pub_b64u()));
    assert!(s(&keys["ssh_user_ca"]).starts_with("ssh-ed25519 "));
    assert!(!ok(&h.call("drop_table", none(), json!({})).await));
}

// -- the caller gates of service::Ops::handle (test_plugin_callers.py and the revision) ------

#[tokio::test]
async fn a_plugin_caller_is_refused_the_operator_verbs() {
    let h = harness();
    let ops = h.ops();
    for verb in service::NO_PLUGIN {
        let out = ops.handle(verb, json!({"device_id": "d1", "order_id": "o1", "ok": true}), none(), None, Some("search")).await;
        assert_eq!(out, json!({"ok": false, "error": format!("{verb} is not open to plugin \"search\"")}));
    }
}

#[tokio::test]
async fn the_operator_still_calls_them() {
    let h = harness();
    let ops = h.ops();
    assert_eq!(ops.handle("devices", json!({}), none(), None, None).await, json!([]));
    assert_eq!(ops.handle("device_revoke", json!({"device_id": "d1"}), none(), None, None).await["revoked"], false);
    assert_eq!(ops.handle("order_pull", json!({"node": "mizar"}), none(), None, None).await, json!([]));
    let order = h.env.ap.issue_order("agent", "mizar", json!({"task_id": "ABEiM0RV"})).unwrap();
    let done = ops.handle("order_done", json!({"order_id": order["order"]["order_id"], "ok": true}), none(), None, None).await;
    assert_eq!(done["recorded"], true);
    assert!(!ok(&ops.handle("devices", json!({}), pid(11), None, None).await), "a session is no operator");
}

#[tokio::test]
async fn the_vault_verbs_refuse_a_session_or_an_as() {
    let h = harness();
    let ops = h.ops();
    for verb in service::NO_SESSION {
        let want = json!({"ok": false, "error": format!("{verb} takes this node's own services only")});
        let plugin = (verb != "devices").then_some("trilium");
        assert_eq!(ops.handle(verb, json!({}), pid(11), None, plugin).await, want);
        let st = Stamp { session_id: Some(B.into()), ..Stamp::default() };
        assert_eq!(ops.handle(verb, json!({}), st, None, plugin).await, want);
        assert_eq!(ops.handle(verb, json!({}), door(A, "lyra"), None, plugin).await, want);
        assert_eq!(ops.handle(verb, json!({}), none(), Some("someone"), None).await, want);
    }
}

#[tokio::test]
async fn board_import_is_for_trilium_alone() {
    let h = harness();
    let ops = h.ops();
    let want = json!({"ok": false, "error": "board_import is open to the trilium plugin only"});
    assert_eq!(ops.handle("board_import", json!({"cards": []}), none(), None, None).await, want);
    assert_eq!(ops.handle("board_import", json!({"cards": []}), none(), None, Some("search")).await, want);
    let out = ops.handle("board_import", json!({"cards": []}), none(), None, Some("trilium")).await;
    assert_eq!(out["ok"], true, "{out}");
    let feed = ops.handle("board_changes", json!({"after": 0}), none(), None, Some("trilium")).await;
    assert_eq!((feed["cards"].clone(), feed["more"].clone()), (json!([]), json!(false)));
}

#[tokio::test]
async fn other_verbs_stay_open_to_plugins() {
    let h = harness();
    let ops = h.ops();
    let out = ops.handle("task_for_session", json!({"session_id": A}), none(), None, Some("reflection")).await;
    assert!(ok(&out), "{out}");
}

#[tokio::test]
async fn ops_post_keeps_one_notice_per_kind_and_node() {
    let h = harness();
    let ops = h.ops();
    let post = |title: &str| json!({"kind": "vpn", "node": "mizar", "title": title, "body": "the tunnel is down"});
    assert_eq!(ops.handle("ops_post", post("VPN down"), none(), None, Some("vpn")).await, json!({"ok": true}));
    ops.handle("ops_post", post("VPN still down"), none(), None, Some("vpn")).await;
    ops.handle("ops_post", json!({"kind": "disk", "node": "lyra", "title": "Disk", "body": "91%"}), none(), None, None).await;
    let missing = ops.handle("ops_post", json!({"kind": "vpn", "node": "mizar", "title": " ", "body": "x"}), none(), None, None).await;
    assert_eq!(missing, json!({"ok": false, "error": "kind, node, title and body are required"}));
    let list = ops.handle("ops_list", json!({}), none(), None, Some("trilium")).await;
    let notes: Vec<(String, String, String)> = list["notes"].as_array().unwrap().iter().map(|n| (s(&n["kind"]), s(&n["node"]), s(&n["title"]))).collect();
    assert_eq!(notes, [("disk".to_string(), "lyra".to_string(), "Disk".to_string()), ("vpn".to_string(), "mizar".to_string(), "VPN still down".to_string())]);
    let note = &list["notes"][1];
    let keys: Vec<&String> = note.as_object().map(Map::keys).unwrap().collect();
    assert_eq!(keys, ["kind", "node", "title", "body", "ts"]);
}

#[tokio::test]
async fn the_home_verbs_answer_only_on_the_home_node() {
    let h = harness();
    let ops = Ops { front: h.front.clone(), home: None };
    let want = json!({"ok": false, "error": "this verb lives on the home node (alcor)"});
    for verb in ["board_changes", "ops_list"] {
        assert_eq!(ops.handle(verb, json!({}), none(), None, Some("trilium")).await, want);
    }
    assert_eq!(ops.handle("devices", json!({}), none(), None, None).await, want);
    assert_eq!(ops.handle("board_import", json!({}), none(), None, Some("trilium")).await, want);
}
