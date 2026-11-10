//! The session verbs (queue.md R145): request_human, the urgent DM, Tony's prompt, the hooks,
//! approvals, Arbutus, and the service's caller gates. Ported from test_front.py
//! and test_plugin_callers.py with the approved revision: card_url for panel_url, approvals
//! on the requester's own card, ops_notes and jobs on session rows.

use std::collections::HashMap;
use std::sync::Arc;

use awm_plugin_operations::arbutus::Approvals;
use awm_plugin_operations::front::{Front, Reach, Stamp};
use awm_plugin_operations::home::LocalClient;
use awm_plugin_operations::relay::Relay;
use awm_plugin_operations::service::{self, Ops};
use awm_plugin_operations::store::Open;
use crate::common::{s, Env, FakeArbutus, FakeOutside, TestClock, A, B, ORIGIN};
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
        link_wait: std::time::Duration::ZERO, asking: Default::default(), roster_moved: Default::default(),
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
async fn a_block_moves_the_card_and_urgent_sends_a_dm() {
    let h = harness();
    h.call("task_open", pid(11), json!({"title": "vpn", "name": "fix-tunnel"})).await;
    let out = h.call("request_human", pid(11), json!({"kind": "blocked", "reason": "needs the VPN key", "urgent": true})).await;
    assert!(ok(&out), "{out}");
    assert_eq!((s(&out["section"]), s(&out["status"])), ("needs_you".into(), "blocked".into()));
    assert_eq!(out["notified"], true);
    let dm = &h.out.dms()[0];
    assert!(dm.contains("needs the VPN key") && dm.contains("fix-tunnel") && dm.contains("job-a"), "{dm}");
    let tid = s(&out["task_id"]);
    assert!(out.get("card_url").is_none(), "the session is not shown the board");
    let got = h.call("task_get", none(), json!({"task_id": tid})).await;
    assert_eq!(got["card_url"], json!(format!("https://board.example.test/card/{tid}")));
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
async fn no_card_means_a_dm_with_the_session_link_and_no_new_card() {
    let h = harness();
    let out = h.call("request_human", pid(11), json!({"kind": "blocked", "reason": "needs the VPN key"})).await;
    assert_eq!((out["ok"].clone(), out["notified"].clone()), (json!(true), json!(true)), "{out}");
    assert!(out.get("task_id").is_none(), "{out}");
    let dm = &h.out.asks()[0];
    assert!(dm.contains("fix-tunnel") && dm.contains("needs the VPN key") && dm.contains("https://claude.ai/code/mizar-job-a"), "{dm}");
    assert_eq!(h.call("task_for_session", pid(11), json!({})).await["task_id"], Value::Null);

    h.out.reach.lock().unwrap().insert("job-b".into(), Reach::Unavailable);
    h.call("request_human", pid(22), json!({"kind": "review", "summary": "look"})).await;
    assert!(h.out.asks()[1].contains("`claude attach job-b` on mizar"), "a job with no link says how to attach");

    let d = format!("cc{}", "d".repeat(32));
    h.call("request_human", door(&d, "lyra"), json!({"reason": "from afar"})).await;
    assert!(h.out.asks()[2].contains("on **lyra**"), "{:?}", h.out.asks());

    let service = h.call("request_human", none(), json!({"kind": "blocked", "reason": "a seat has no scope"})).await;
    assert_eq!(service["notified"], true);
    assert!(h.out.asks()[3].starts_with("**A service**"));
    assert_eq!(h.out.reaches.lock().unwrap().len(), 3, "no job, no /rc");

    *h.out.ask_fails.lock().unwrap() = true;
    let failed = h.call("request_human", pid(11), json!({"reason": "again"})).await;
    assert!(!ok(&failed) && s(&failed["error"]).contains("did not go out"), "{failed}");
}

#[tokio::test]
async fn a_queued_remote_control_sends_its_link_once_it_connects() {
    let h = harness();
    h.out.reach.lock().unwrap().insert("job-a".into(), Reach::Connecting);
    let set_roster = |link: Option<&str>| {
        let jobs = HashMap::from([("job-a".to_string(), link.map(str::to_string))]);
        h.out.roster.lock().unwrap().insert("mizar".into(), jobs);
    };

    let first = h.call("request_human", pid(11), json!({"reason": "one"})).await;
    assert_eq!(first["link"], Value::Null);
    assert!(s(&first["note"]).contains("end your turn"), "{first}");
    assert!(h.out.asks()[0].contains("link follows") && h.out.asks()[0].contains("claude attach job-a"), "{:?}", h.out.asks());
    h.call("request_human", pid(11), json!({"reason": "two"})).await;
    assert_eq!(h.out.reaches.lock().unwrap().len(), 1, "a second call in the turn types no second /rc");

    assert_eq!(h.front.links_due().await, 0, "no roster answer keeps the mark");
    set_roster(None);
    assert_eq!(h.front.links_due().await, 0, "still connecting");
    set_roster(Some("https://claude.ai/code/session_a"));
    *h.out.ask_fails.lock().unwrap() = true;
    assert_eq!(h.front.links_due().await, 0, "a failed DM keeps the mark");
    *h.out.ask_fails.lock().unwrap() = false;
    assert_eq!(h.front.links_due().await, 1);
    assert!(h.out.asks().last().unwrap().contains("https://claude.ai/code/session_a"));
    assert_eq!(h.front.links_due().await, 0, "the link goes out once");
}

#[tokio::test]
async fn a_link_that_connects_during_the_call_goes_in_the_one_dm() {
    let h = harness();
    h.out.reach.lock().unwrap().insert("job-a".into(), Reach::Connecting);
    h.out.roster.lock().unwrap().insert("mizar".into(), HashMap::from([("job-a".to_string(), Some("https://claude.ai/code/session_a".to_string()))]));
    let out = h.call("request_human", pid(11), json!({"reason": "one"})).await;
    assert_eq!(out["link"], "https://claude.ai/code/session_a");
    assert_eq!(h.out.asks().len(), 1);
    assert!(h.out.asks()[0].ends_with("https://claude.ai/code/session_a"), "{:?}", h.out.asks());
    assert_eq!(h.front.links_due().await, 0);
}

#[tokio::test]
async fn a_session_that_goes_while_connecting_is_forgotten() {
    let h = harness();
    h.out.reach.lock().unwrap().insert("job-a".into(), Reach::Connecting);
    h.call("request_human", pid(11), json!({"reason": "one"})).await;
    h.out.roster.lock().unwrap().insert("mizar".into(), HashMap::new());
    assert_eq!(h.front.links_due().await, 0);
    let asked = h.out.asks().len();
    h.out.roster.lock().unwrap().insert("mizar".into(), HashMap::from([("job-a".to_string(), Some("https://claude.ai/code/x".to_string()))]));
    assert_eq!((h.front.links_due().await, h.out.asks().len()), (0, asked));
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
async fn a_session_through_the_door_acts_on_its_own_card() {
    let h = harness();
    let d = format!("cc{}", "d".repeat(32));
    h.call("task_open", door(&d, "lyra"), json!({"title": "key", "name": "remote-work"})).await;
    let out = h.call("request_human", door(&d, "lyra"), json!({"kind": "blocked", "reason": "needs a key", "urgent": true})).await;
    assert!(ok(&out) && out["status"] == "blocked", "{out}");
    let card = h.card(&out["task_id"]);
    let ses = &card["sessions"][0];
    assert_eq!((s(&ses["session_id"]), s(&ses["node"]), s(&ses["job"])), (d.clone(), "lyra".into(), "job-d".into()));
    let dm = &h.out.dms()[0];
    assert!(dm.contains("on **lyra**") && dm.contains("job-d` on lyra"), "{dm}");
    let plan = h.call("hook_plan_approved", door(&d, "lyra"), json!({"plan_text": "# Next"})).await;
    assert_eq!((plan["task_id"].clone(), plan["opened"].clone()), (out["task_id"].clone(), json!(false)));
    let new = format!("cc{}", "e".repeat(32));
    let cleared = h.call("hook_session_clear", door(&new, "lyra"), json!({"new_session_id": new})).await;
    assert_eq!((s(&cleared["ended"]), cleared["task_id"].clone()), (d, out["task_id"].clone()));
}

#[tokio::test]
async fn an_approved_plan_opens_a_card_titled_by_its_heading_but_not_for_a_seat() {
    let h = harness();
    let out = h.call("hook_plan_approved", pid(11), json!({"session_id": A, "plan_text": "# Fix the tunnel\n\nsteps"})).await;
    assert_eq!(out["opened"], json!(true), "{out}");
    assert!(out.get("notice").is_none(), "the session is told nothing");
    let card = h.card(&out["task_id"]);
    assert_eq!((s(&card["title"]), s(&card["section"])), ("Fix the tunnel".into(), "working".into()));
    assert!(s(&card["meta"]["plan"]).starts_with("# Fix the tunnel"));
    let again = h.call("hook_plan_approved", pid(11), json!({"session_id": A, "plan_text": "# Fix the tunnel v2"})).await;
    assert_eq!((again["task_id"].clone(), again["opened"].clone()), (out["task_id"].clone(), json!(false)));
    assert_eq!(h.card(&out["task_id"])["meta"]["plan"], "# Fix the tunnel v2");
    h.out.pids.lock().unwrap().insert(44, json!({"ok": true, "session_id": format!("cc{}", "4".repeat(32)), "job": "job-s", "name": "seat:slack/lab/ryan"}));
    let seat = h.call("hook_plan_approved", pid(44), json!({"plan_text": "# Seat work"})).await;
    assert_eq!((seat["tracked"].clone(), seat.get("task_id")), (json!(false), None), "a seat gets no card");
}

#[tokio::test]
async fn a_clear_keeps_the_job_on_its_card() {
    let h = harness();
    let t = h.call("track_task", pid(11), json!({"title": "before the clear"})).await["task_id"].clone();
    let new = format!("cc{}", "c".repeat(32));
    h.out.pids.lock().unwrap().insert(33, json!({"ok": true, "session_id": new, "job": "job-a", "name": "fix-tunnel"}));
    let out = h.call("hook_session_clear", pid(33), json!({"new_session_id": new})).await;
    assert_eq!((s(&out["ended"]), out["task_id"].clone()), (A.into(), t.clone()));
    let rows: HashMap<String, (bool, String)> = h.env.store.task_sessions(&s(&t)).unwrap().iter()
        .map(|x| (s(&x["session_id"]), (x["active"].as_bool().unwrap(), s(&x["role"])))).collect();
    assert_eq!(rows, HashMap::from([(A.to_string(), (false, "main".into())), (new.clone(), (true, "main".into()))]));
    assert_eq!(h.card(&t)["section"], "working");
    let plan = h.call("hook_plan_approved", pid(33), json!({"session_id": new, "plan_text": "# Next"})).await;
    assert_eq!((plan["opened"].clone(), plan["task_id"].clone()), (json!(false), t), "the next plan lands on the same card");
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
async fn claim_moves_a_needs_you_card_to_working() {
    let h = harness();
    let o = Open { kind: Some("note".into()), section: Some("needs_you".into()), ..Open::default() };
    let t = s(&h.env.store.task_open("idea", o, None).unwrap()["task_id"]);
    assert!(!ok(&h.call("claim_task", pid(11), json!({})).await));
    let out = h.call("claim_task", pid(11), json!({"task_id": t})).await;
    assert!(ok(&out) && out["section"] == "working", "{out}");
    let card = h.env.store.task_get(&t).unwrap();
    assert_eq!((s(&card["kind"]), s(&card["sessions"][0]["role"]), s(&card["sessions"][0]["job"])), ("task".into(), "main".into(), "job-a".into()));
    h.env.store.task_report(&t, Some(A), "done", None, None).unwrap();
    assert!(!ok(&h.call("claim_task", pid(22), json!({"task_id": t})).await), "History is not claimable");
}

#[tokio::test]
async fn an_urgent_block_reminds_tony_once() {
    let h = harness();
    h.call("task_open", pid(11), json!({"title": "key", "name": "fix-tunnel"})).await;
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
    h.env.store.session_state(Some(A), None, "mizar", "working", None).unwrap();
    h.clock.advance(61.0);
    assert_eq!(h.front.remind_due().await, 0);
    assert_eq!(h.card(&out["task_id"])["section"], "working");
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
async fn a_session_attached_by_another_nodes_service_is_recorded_on_that_node() {
    let h = harness();
    let ops = h.ops();
    let t = h.env.store.agent_card("mizar", None).unwrap()["task_id"].clone();
    let st = Stamp { peer: Some("mizar".into()), ..none() };
    let sid = "cc08a5906268d5471f84530c72522d4d3b";
    assert!(ok(&ops.handle("task_attach", json!({"task_id": t, "session_id": sid, "role": "main"}), st, Some("peer:mizar"), None).await));
    let got = h.env.store.task_get(t.as_str().unwrap()).unwrap();
    assert_eq!(got["sessions"][0]["node"], "mizar");
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
async fn ops_post_keeps_one_notice_per_kind_and_node() {
    let h = harness();
    let ops = h.ops();
    let post = |title: &str| json!({"kind": "vpn", "node": "mizar", "title": title, "body": "the tunnel is down"});
    assert_eq!(ops.handle("ops_post", post("VPN down"), none(), None, Some("vpn")).await, json!({"ok": true}));
    ops.handle("ops_post", post("VPN still down"), none(), None, Some("vpn")).await;
    ops.handle("ops_post", json!({"kind": "disk", "node": "lyra", "title": "Disk", "body": "91%"}), none(), None, None).await;
    let missing = ops.handle("ops_post", json!({"kind": "vpn", "node": "mizar", "title": " ", "body": "x"}), none(), None, None).await;
    assert_eq!(missing, json!({"ok": false, "error": "kind, node, title and body are required"}));
    let list = h.env.home.store.ops_list().unwrap();
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
    assert_eq!(ops.handle("devices", json!({}), none(), None, None).await, want);
}
