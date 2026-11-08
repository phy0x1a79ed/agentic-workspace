//! Admin approvals (users.md R123): a bundle posted to Slack, decided by an admin's reply in
//! its thread. Ported from test_arbutus.py. The channel and the admins come from process-wide
//! env vars, so every test holds one lock and sets the same synthetic values.

use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use awm_plugin_operations::arbutus::{parse_bundle, Approvals};
use crate::common::{s, FakeArbutus};
use serde_json::{json, Value};

const TONY: &str = "U01";
const RYAN: &str = "U02";
const STEVEN: &str = "U03";
const BUNDLE: &str = "reason\tGrow altair's root\nminutes\t60\nssh\tshaula-admin\ntf\tupdate\topenstack_compute_instance_v2.service[\"altair\"]\n";

static ENV: Mutex<()> = Mutex::new(());

fn env_lock() -> MutexGuard<'static, ()> {
    let g = ENV.lock().unwrap_or_else(|e| e.into_inner());
    std::env::set_var("AWM_APPROVAL_CHANNEL", "slack-bot:C9");
    std::env::set_var("AWM_APPROVAL_ADMINS", format!("{TONY}=tony,{RYAN}=ryan"));
    g
}

type Decided = Arc<Mutex<Vec<(String, String)>>>;

struct H {
    _g: MutexGuard<'static, ()>,
    dir: tempfile::TempDir,
    io: Arc<FakeArbutus>,
    approvals: Approvals,
    decided: Decided,
}

impl H {
    fn new(rc: i32) -> H {
        let g = env_lock();
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("bundles")).unwrap();
        std::fs::write(dir.path().join("bundles/grow.bundle"), BUNDLE).unwrap();
        let io = FakeArbutus::new(rc);
        let approvals = Approvals::with_dirs(io.clone(), dir.path().join("ssh/admin-approvals/pending"), dir.path().join("bundles"));
        let decided: Decided = Arc::new(Mutex::new(vec![]));
        let d = decided.clone();
        approvals.set_on_decided(Arc::new(move |task: String, line: String| {
            let d = d.clone();
            Box::pin(async move { d.lock().unwrap().push((task, line)) })
        }));
        H { _g: g, dir, io, approvals, decided }
    }

    fn pending(&self) -> PathBuf {
        self.dir.path().join("ssh/admin-approvals/pending")
    }

    async fn open(&self) -> Value {
        let req = self.approvals.open("grow", "infra-agent", "altair", "job1").await.unwrap();
        self.approvals.attach(&s(&req["ref"]), "ABCDEFGH").unwrap();
        req
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

    fn state(&self, r: &Value) -> Value {
        serde_json::from_str(&std::fs::read_to_string(self.pending().join(s(r)).join("request.json")).unwrap()).unwrap()
    }

    async fn decided(&self) -> Vec<(String, String)> {
        for _ in 0..50 {
            if !self.decided.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        self.decided.lock().unwrap().clone()
    }
}

#[tokio::test]
async fn open_freezes_and_posts() {
    use std::os::unix::fs::PermissionsExt;
    let h = H::new(0);
    let req = h.open().await;
    let r = s(&req["ref"]);
    assert!(r.len() == 6 && r.bytes().all(|b| b.is_ascii_hexdigit()), "{r}");
    let frozen = h.pending().join(&r).join("grow.bundle");
    assert_eq!(std::fs::read_to_string(&frozen).unwrap(), BUNDLE);
    assert_eq!(std::fs::metadata(frozen.parent().unwrap()).unwrap().permissions().mode() & 0o777, 0o700);
    let (account, channel, text, thread) = h.io.posts()[0].clone();
    assert_eq!((account.as_str(), channel.as_str(), thread.as_str()), ("slack-bot", "C9", ""));
    assert_eq!(*h.io.quiet.lock().unwrap(), [true]);
    assert!(text.contains("shaula-admin") && text.contains("approve") && !text.contains("&lt;"), "{text}");
    let st = h.state(&req["ref"]);
    assert_eq!((s(&st["post_ts"]), s(&st["task"]), s(&st["state"])), ("100.1".into(), "ABCDEFGH".into(), "pending".into()));
    assert_eq!((s(&st["reason"]), s(&st["minutes"])), ("Grow altair's root".into(), "60".into()));
    assert_eq!(st["ssh"], json!(["shaula-admin"]));
}

#[tokio::test]
async fn admin_approve_runs_on_the_frozen_copy() {
    let h = H::new(0);
    let req = h.open().await;
    h.reply("Approve, thanks", TONY).await;
    let (name, bundles, by) = h.io.runs()[0].clone();
    assert_eq!((name.as_str(), by.as_str()), ("grow", "tony via Slack"));
    assert_eq!(bundles, h.pending().join(s(&req["ref"])));
    let st = h.state(&req["ref"]);
    assert_eq!((s(&st["state"]), s(&st["decided_by"])), ("approved".into(), "tony".into()));
    let last = h.io.posts().last().cloned().unwrap();
    assert_eq!(last.3, "100.1");
    assert!(last.2.contains("Approved by tony"));
    let (job, line) = h.io.lines()[0].clone();
    assert_eq!(job, "job1");
    assert!(line.contains("approved") && line.contains("ssh windows are open for 60 minutes") && line.contains("scripts/tf --approval grow"), "{line}");
    assert_eq!(h.decided().await, [("ABCDEFGH".to_string(), line)], "the card hears the same line");
}

#[tokio::test]
async fn ryan_counts_and_deny_grants_nothing() {
    let h = H::new(0);
    let req = h.open().await;
    h.reply("deny", RYAN).await;
    assert!(h.io.runs().is_empty());
    let st = h.state(&req["ref"]);
    assert_eq!((s(&st["state"]), s(&st["decided_by"])), ("denied".into(), "ryan".into()));
    assert!(h.io.lines()[0].1.contains("Nothing was granted"));
}

#[tokio::test]
async fn relayed_or_bot_message_is_ignored() {
    for extra in [json!({"relayed": true}), json!({"sender_bot": true})] {
        let h = H::new(0);
        let req = h.open().await;
        h.reply_with("approve", TONY, extra).await;
        assert!(h.io.runs().is_empty());
        assert_eq!(h.io.posts().len(), 1);
        assert_eq!(h.state(&req["ref"])["state"], "pending");
    }
}

#[tokio::test]
async fn non_admin_is_told_and_nothing_runs() {
    let h = H::new(0);
    let req = h.open().await;
    h.reply("approve", STEVEN).await;
    assert!(h.io.runs().is_empty());
    assert!(h.io.posts().last().unwrap().2.contains("not on the list"));
    assert_eq!(h.state(&req["ref"])["state"], "pending");
}

#[tokio::test]
async fn casual_reply_and_other_threads_are_ignored() {
    let h = H::new(0);
    h.open().await;
    h.reply("ok, checking", TONY).await;
    h.approvals.handle(&json!({"account": "slack-bot", "channel_id": "C9", "thread_id": "999.9", "sender_id": TONY, "text": "approve"})).await;
    h.approvals.handle(&json!({"account": "slack-bot", "channel_id": "C8", "thread_id": "100.1", "sender_id": TONY, "text": "approve"})).await;
    h.approvals.handle(&json!({"account": "slack-bot", "channel_id": "C9", "sender_id": TONY, "text": "approve"})).await;
    assert!(h.io.runs().is_empty());
    assert_eq!(h.io.posts().len(), 1);
}

#[tokio::test]
async fn second_answer_changes_nothing() {
    let h = H::new(0);
    let req = h.open().await;
    h.reply("approve", TONY).await;
    h.reply("approve", RYAN).await;
    assert_eq!(h.io.runs().len(), 1);
    assert!(h.io.posts().last().unwrap().2.contains("already approved"));
    assert_eq!(h.state(&req["ref"])["decided_by"], "tony");
}

#[tokio::test]
async fn changed_frozen_copy_is_refused() {
    let h = H::new(0);
    let req = h.open().await;
    std::fs::write(h.pending().join(s(&req["ref"])).join("grow.bundle"), format!("{BUNDLE}ssh\t*\n")).unwrap();
    h.reply("approve", TONY).await;
    assert!(h.io.runs().is_empty());
    assert_eq!(h.state(&req["ref"])["state"], "refused");
}

#[tokio::test]
async fn expired_request_is_closed() {
    let h = H::new(0);
    let req = h.open().await;
    let mut st = h.state(&req["ref"]);
    st["created"] = json!(awm_plugin_operations::now() - 2.0 * 86400.0);
    std::fs::write(h.pending().join(s(&req["ref"])).join("request.json"), st.to_string()).unwrap();
    h.reply("approve", TONY).await;
    assert!(h.io.runs().is_empty());
    assert_eq!(h.state(&req["ref"])["state"], "expired");
}

#[tokio::test]
async fn open_refuses_a_bad_name_or_bundle() {
    let h = H::new(0);
    assert!(h.approvals.open("../etc", "a", "n", "").await.is_err());
    assert!(h.approvals.open("missing", "a", "n", "").await.is_err());
    std::fs::write(h.dir.path().join("bundles/bad.bundle"), "minutes\t5\n").unwrap();
    assert!(h.approvals.open("bad", "a", "n", "").await.is_err());
    assert!(h.io.posts().is_empty());
}

#[test]
fn parse_bundle_refuses_what_the_script_refuses() {
    for text in ["minutes\t60\n", "reason\tx\nminutes\t999\n", "reason\tx\nminutes\t5\ntf\tdelete\n", "reason\tx\nminutes\t0\n", "reason\tx\nminutes\t+5\n"] {
        assert!(parse_bundle(text).is_err(), "{text:?}");
    }
    let ok = parse_bundle("# a comment\n\nreason\tx\nminutes\t5\nssh\ta\nssh\tb\ntf\tupdate\tres.x\n").unwrap();
    assert_eq!(ok, json!({"reason": "x", "minutes": "5", "ssh": ["a", "b"], "tf": ["update res.x"]}));
}
