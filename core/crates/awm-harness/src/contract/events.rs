//! How a harness tells reflection that a command with a follow-up ended (harness-contract.md
//! § Events).
//!
//! An event reflection does not take is spooled to a file the plugin names, one JSON line each,
//! and sent again by [`Reporter::resend`] at the plugin's next start. A lost event is never lost
//! twice.

use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Done,
    Lost,
    Clear,
}

/// One event, with the keys of reflection's `harness_event` verb. Absent fields are left out.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HarnessEvent {
    pub session_id: String,
    pub kind: Kind,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub parent: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub ticket: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub command: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub followup: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub pid: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub cwd: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub told_session: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub injected_at_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub reissues: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub at_ms: Option<i64>,
}

impl HarnessEvent {
    pub fn new(session_id: impl Into<String>, kind: Kind) -> Self {
        Self {
            session_id: session_id.into(),
            kind,
            parent: None,
            ticket: None,
            command: None,
            followup: None,
            reason: None,
            name: None,
            pid: None,
            cwd: None,
            told_session: None,
            injected_at_ms: None,
            reissues: None,
            at_ms: None,
        }
    }
}

/// The notice a harness types into a live session whose command and follow-up were lost.
pub fn lost_notice(command: &str, followup: &str, reason: &str) -> String {
    format!(
        "[awm reflection] Your `{command}` and the follow-up \"{followup}\" were lost: {reason}. Nothing is \
         queued for you now. Re-run the command if you still need it, then carry on. Automated message \
         from the reflection service; it is not a new instruction from the user."
    )
}

type SendFuture = Pin<Box<dyn Future<Output = Result<Value, String>> + Send>>;
/// What carries one event to reflection: the hub in a plugin, a fake in a test.
pub type Sender = Arc<dyn Fn(Value) -> SendFuture + Send + Sync>;

pub const SEND_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Clone)]
pub struct Reporter {
    send: Sender,
    spool: PathBuf,
    lock: Arc<tokio::sync::Mutex<()>>,
}

impl Reporter {
    /// A reporter that calls reflection's `harness_event` through `hub` and spools to `spool`.
    pub fn new(hub: awm_plugin_sdk::Hub, spool: PathBuf) -> Self {
        let send: Sender = Arc::new(move |event: Value| {
            let hub = hub.clone();
            Box::pin(async move {
                let opts = awm_plugin_sdk::CallOpts { timeout: SEND_TIMEOUT, ..Default::default() };
                hub.call_with("reflection", "harness_event", event, &opts).await.map_err(|e| e.to_string())
            })
        });
        Self::with_sender(send, spool)
    }

    pub fn with_sender(send: Sender, spool: PathBuf) -> Self {
        Self { send, spool, lock: Arc::default() }
    }

    pub fn spool_path(&self) -> &std::path::Path {
        &self.spool
    }

    /// Send `event`. A reply whose `ok` is not false counts as taken. Anything else spools the
    /// event and answers false.
    pub async fn report(&self, event: &HarnessEvent) -> bool {
        self.report_value(serde_json::to_value(event).expect("an event serializes")).await
    }

    async fn report_value(&self, event: Value) -> bool {
        let reply = (self.send)(event.clone()).await;
        let error = match &reply {
            Ok(Value::Object(m)) if m.get("ok") != Some(&Value::Bool(false)) => return true,
            Ok(v) => v.get("error").and_then(Value::as_str).unwrap_or("refused").to_string(),
            Err(e) => e.clone(),
        };
        tracing::error!(
            kind = %event["kind"], session = %event["session_id"], %error,
            "reflection did not take the event; spooled to resend"
        );
        let _held = self.lock.lock().await;
        if let Err(e) = self.append(&event) {
            tracing::error!(kind = %event["kind"], "could not spool the event either: {e}");
        }
        false
    }

    fn append(&self, event: &Value) -> std::io::Result<()> {
        use std::io::Write;
        if let Some(dir) = self.spool.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&self.spool)?;
        writeln!(f, "{event}")
    }

    /// Send every spooled event again, emptying the spool first, and answer how many reflection
    /// took. An event refused again goes back to the spool.
    pub async fn resend(&self) -> usize {
        let text = {
            let _held = self.lock.lock().await;
            let Ok(text) = std::fs::read_to_string(&self.spool) else { return 0 };
            if std::fs::remove_file(&self.spool).is_err() {
                return 0;
            }
            text
        };
        let mut sent = 0;
        for line in text.lines() {
            if let Ok(event) = serde_json::from_str::<Value>(line) {
                sent += self.report_value(event).await as usize;
            }
        }
        sent
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::Mutex;

    fn fake(replies: Vec<Result<Value, String>>) -> (Sender, Arc<Mutex<Vec<Value>>>) {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let replies = Arc::new(Mutex::new(replies));
        let s = seen.clone();
        let send: Sender = Arc::new(move |v| {
            s.lock().unwrap().push(v);
            let r = replies.lock().unwrap().remove(0);
            Box::pin(async move { r })
        });
        (send, seen)
    }

    #[test]
    fn keys_match_reflections_verb() {
        let mut e = HarnessEvent::new("cc00", Kind::Lost);
        e.parent = Some("p".into());
        e.ticket = Some("t".into());
        e.command = Some("/compact".into());
        e.followup = Some("f".into());
        e.reason = Some("r".into());
        e.name = Some("n".into());
        e.pid = Some(1);
        e.cwd = Some("/".into());
        e.told_session = Some(true);
        e.injected_at_ms = Some(1);
        e.reissues = Some(0);
        e.at_ms = Some(2);
        let v = serde_json::to_value(&e).unwrap();
        let keys: Vec<&str> = v.as_object().unwrap().keys().map(String::as_str).collect();
        let base: Value =
            serde_json::from_str(include_str!("../../../../../docs/plugin-redux/ports/baseline/reflection.describe.json"))
                .unwrap();
        let verb = base["verbs"].as_array().unwrap().iter().find(|v| v["verb"] == "harness_event").unwrap();
        let want: Vec<&str> = verb["params"].as_array().unwrap().iter().map(|p| p["name"].as_str().unwrap()).collect();
        let (mut a, mut b) = (keys.clone(), want.clone());
        a.sort();
        b.sort();
        assert_eq!(a, b);
        assert_eq!(v["kind"], "lost");
        assert!(serde_json::to_value(HarnessEvent::new("cc", Kind::Done)).unwrap().get("pid").is_none());
    }

    #[tokio::test]
    async fn spools_refusal_and_transport_error_then_resends() {
        let dir = tempfile::tempdir().unwrap();
        let spool = dir.path().join("sub/unsent.jsonl");
        let (send, seen) = fake(vec![
            Ok(json!({"ok": true})),
            Ok(json!({"ok": false, "error": "no"})),
            Err("down".into()),
            Ok(json!({"ok": true})),
            Err("still down".into()),
        ]);
        let r = Reporter::with_sender(send, spool.clone());
        assert!(r.report(&HarnessEvent::new("cc1", Kind::Done)).await);
        assert!(!r.report(&HarnessEvent::new("cc2", Kind::Done)).await);
        assert!(!r.report(&HarnessEvent::new("cc3", Kind::Lost)).await);
        assert_eq!(std::fs::read_to_string(&spool).unwrap().lines().count(), 2);
        assert_eq!(r.resend().await, 1);
        let left = std::fs::read_to_string(&spool).unwrap();
        assert_eq!(left.lines().count(), 1);
        assert!(left.contains("cc3"));
        assert_eq!(seen.lock().unwrap().len(), 5);
    }

    #[tokio::test]
    async fn resend_without_spool_is_zero() {
        let dir = tempfile::tempdir().unwrap();
        let (send, _) = fake(vec![]);
        assert_eq!(Reporter::with_sender(send, dir.path().join("none")).resend().await, 0);
    }
}
