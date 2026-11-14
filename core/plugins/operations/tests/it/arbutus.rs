//! Admin approvals in Slack (users.md R123): a session's admin_ssh request for shogun, posted
//! to Slack and decided on the approver by an admin's reply in its thread. The channel and the
//! admins come from process-wide env vars, so every test holds one lock and sets the same
//! synthetic values.

use std::sync::{Arc, Mutex, MutexGuard};

use awm_plugin_operations::arbutus::Approvals;
use crate::common::{s, ssh_pubkey, Env, FakeArbutus};
use serde_json::{json, Value};

const SESSION: &str = "cc11111111111111111111111111111111";
const TONY: &str = "U01";
const RYAN: &str = "U02";
const STEVEN: &str = "U03";

static ENV: Mutex<()> = Mutex::new(());

fn env_lock() -> MutexGuard<'static, ()> {
    let g = ENV.lock().unwrap_or_else(|e| e.into_inner());
    std::env::set_var("AWM_APPROVAL_CHANNEL", "slack-bot:C9");
    std::env::set_var("AWM_APPROVAL_ADMINS", format!("{TONY}=tony,{RYAN}=ryan"));
    g
}

struct H {
    _g: MutexGuard<'static, ()>,
    env: Env,
    io: Arc<FakeArbutus>,
    approvals: Approvals,
}

impl H {
    fn new() -> H {
        let g = env_lock();
        let env = Env::new();
        let io = FakeArbutus::new(env.ap.clone());
        let approvals = Approvals::with_dir(io.clone(), env.path("arbutus"));
        H { _g: g, env, io, approvals }
    }

    async fn request(&self, nodes: Value) -> String {
        let payload = json!({"pubkey": ssh_pubkey(), "nodes": nodes});
        let r = self.env.ap.approval_request("admin_ssh", None, "Grow altair's root", 60, payload, Some(SESSION), None).await.unwrap();
        s(&r["request_id"])
    }

    async fn open(&self) -> String {
        let rid = self.request(json!(["shogun"])).await;
        self.approvals.open(&rid, "infra-agent", "shogun", SESSION).await.unwrap();
        rid
    }

    async fn reply_with(&self, text: &str, sender: &str, extra: Value) {
        let mut e = json!({"account": "slack-bot", "channel_id": "C9", "thread_id": "100.1", "sender_id": sender,
                           "sender_name": "x", "text": text, "relayed": false, "sender_bot": false});
        e.as_object_mut().unwrap().extend(extra.as_object().cloned().unwrap_or_default());
        self.approvals.handle(&e).await;
    }

    async fn reply(&self, text: &str, sender: &str) {
        self.reply_with(text, sender, json!({})).await;
    }

    fn status(&self, rid: &str) -> Value {
        self.env.ap.approval_get(rid).unwrap()["status"].clone()
    }

    fn record(&self, rid: &str) -> Value {
        serde_json::from_str(&std::fs::read_to_string(self.env.path("arbutus").join(format!("{rid}.json"))).unwrap()).unwrap()
    }
}

#[tokio::test]
async fn open_posts_and_grants_nothing() {
    let h = H::new();
    let rid = h.open().await;
    let (account, channel, text, thread) = h.io.posts()[0].clone();
    assert_eq!((account.as_str(), channel.as_str(), thread.as_str()), ("slack-bot", "C9", ""));
    assert_eq!(*h.io.quiet.lock().unwrap(), [true]);
    assert!(text.contains(&rid) && text.contains("60 minutes") && text.contains("approve") && !text.contains("&lt;"), "{text}");
    let rec = h.record(&rid);
    assert_eq!((s(&rec["post_ts"]), s(&rec["state"]), s(&rec["session"])), ("100.1".into(), "pending".into(), SESSION.into()));
    assert_eq!(h.status(&rid), "pending");
}

#[tokio::test]
async fn ryan_approve_mints_the_shogun_certificate() {
    let h = H::new();
    let rid = h.open().await;
    h.reply("Approve, thanks", RYAN).await;
    let got = h.env.ap.approval_get(&rid).unwrap();
    assert_eq!((got["status"].as_str(), got["device"].as_str()), (Some("approved"), None), "{got}");
    let cert = ssh_key::Certificate::from_openssh(&s(&got["ssh_cert"])).unwrap();
    assert_eq!(cert.valid_principals(), ["admin@shogun"]);
    assert_eq!(cert.valid_before() - cert.valid_after(), 60 * 60);
    assert_eq!(got["grant"]["grant"]["approved_by_device"], "slack:ryan");
    assert_eq!(s(&h.record(&rid)["decided_by"]), "ryan");
    let last = h.io.posts().last().cloned().unwrap();
    assert!(last.3 == "100.1" && last.2.contains("Approved by ryan"), "{last:?}");
    let (job, line) = h.io.lines()[0].clone();
    assert!(job == SESSION && line.contains("approval_get"), "{line}");
}

#[tokio::test]
async fn deny_denies_on_the_approver() {
    let h = H::new();
    let rid = h.open().await;
    h.reply("deny", TONY).await;
    assert_eq!(h.status(&rid), "denied");
    assert_eq!(s(&h.record(&rid)["state"]), "denied");
    assert!(h.io.lines()[0].1.contains("Nothing was granted"));
}

#[tokio::test]
async fn relayed_bot_or_outsider_replies_decide_nothing() {
    for (extra, sender) in [(json!({"relayed": true}), TONY), (json!({"sender_bot": true}), TONY), (json!({}), STEVEN)] {
        let h = H::new();
        let rid = h.open().await;
        h.reply_with("approve", sender, extra).await;
        assert_eq!(h.status(&rid), "pending");
    }
    let h = H::new();
    h.open().await;
    h.reply("approve", STEVEN).await;
    assert!(h.io.posts().last().unwrap().2.contains("not on the list"));
}

#[tokio::test]
async fn casual_reply_and_other_threads_are_ignored() {
    let h = H::new();
    let rid = h.open().await;
    h.reply("ok, checking", TONY).await;
    h.approvals.handle(&json!({"account": "slack-bot", "channel_id": "C9", "thread_id": "999.9", "sender_id": TONY, "text": "approve"})).await;
    h.approvals.handle(&json!({"account": "slack-bot", "channel_id": "C8", "thread_id": "100.1", "sender_id": TONY, "text": "approve"})).await;
    h.approvals.handle(&json!({"account": "slack-bot", "channel_id": "C9", "sender_id": TONY, "text": "approve"})).await;
    assert_eq!(h.status(&rid), "pending");
    assert_eq!(h.io.posts().len(), 1);
}

#[tokio::test]
async fn second_answer_changes_nothing() {
    let h = H::new();
    let rid = h.open().await;
    h.reply("approve", TONY).await;
    h.reply("deny", RYAN).await;
    assert_eq!(h.status(&rid), "approved");
    assert!(h.io.posts().last().unwrap().2.contains("already approved"));
}

#[tokio::test]
async fn only_the_requester_posts_only_a_shogun_request() {
    let h = H::new();
    let other = h.request(json!(["shogun", "alcor"])).await;
    assert!(h.approvals.open(&other, "a", "n", SESSION).await.unwrap_err().contains("shogun alone"));
    let rid = h.request(json!(["shogun"])).await;
    assert!(h.approvals.open(&rid, "a", "n", "cc-someone-else").await.is_err());
    assert!(h.approvals.open("qnope", "a", "n", SESSION).await.is_err());
    assert!(h.io.posts().is_empty());
}

#[tokio::test]
async fn the_approver_refuses_a_slack_grant_beyond_shogun() {
    let h = H::new();
    let rid = h.request(json!(["alcor"])).await;
    let err = h.env.ap.decide_by_admin(&rid, "ryan", true).await.unwrap_err();
    assert!(err.message.contains("shogun alone"), "{}", err.message);
    assert_eq!(h.status(&rid), "pending");
}
