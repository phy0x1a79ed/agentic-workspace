//! The session behind a caller, and a line typed into a session, through reflection
//! (`gatewayclient/sessions.py`). A service reads no harness's files.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};

use crate::hub::{CallOpts, Hub};
use crate::supervise::{SubscriptionOpts, SupervisedSubscription};

/// The session `pid` belongs to, or None when it is none or reflection cannot answer. The record
/// carries `session_id` (R9), `name`, `job`, `hosting` (`background` or `terminal`), `pid`,
/// `cwd` and `harness`.
pub async fn session_of(hub: &Hub, pid: u32) -> Option<Value> {
    if pid == 0 {
        return None;
    }
    let opts = CallOpts { timeout: Duration::from_secs(10), ..CallOpts::default() };
    let out = hub.call_with("reflection", "session_of", json!({"pid": pid}), &opts).await.ok()?;
    let ok = out.get("ok").and_then(Value::as_bool) == Some(true);
    let id = out.get("session_id").is_some_and(|s| s.as_str().is_some_and(|s| !s.is_empty()));
    (ok && id).then_some(out)
}

/// Type `text` into the session with canonical id `session` and submit it. An error says why it
/// was not delivered.
pub async fn tell(hub: &Hub, session: &str, text: &str) -> anyhow::Result<Value> {
    let opts = CallOpts { timeout: Duration::from_secs(30), ..CallOpts::default() };
    let out = hub.call_with("reflection", "message", json!({"to": {"id": session}, "text": text}), &opts).await?;
    if out.get("ok").and_then(Value::as_bool) != Some(true) {
        let why = out.get("reason").or_else(|| out.get("error")).cloned().unwrap_or(out.clone());
        anyhow::bail!("could not type into session {session}: {why}");
    }
    Ok(out)
}

type Changed = Arc<dyn Fn(String) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

/// One subscription per node to reflection's `sessions` topic. A session start or stop calls
/// `changed(node)` as it happens, and so does every (re)connect, once, because events are not
/// replayed: the callback re-reads the list then. A rename or a remote-control change is no
/// change of who exists, so it calls nothing.
pub struct SessionWatch {
    hub: Hub,
    name: String,
    here: String,
    changed: Changed,
    subs: Mutex<HashMap<String, SupervisedSubscription>>,
}

impl SessionWatch {
    pub fn new<F, Fut>(hub: Hub, name: &str, changed: F) -> Arc<Self>
    where
        F: Fn(String) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = ()> + Send + 'static,
    {
        let changed: Changed = Arc::new(move |node| Box::pin(changed(node)));
        Arc::new(Self { hub, name: name.into(), here: crate::config::node_name(), changed, subs: Mutex::default() })
    }

    /// Watch `node` too. Idempotent.
    pub fn ensure(&self, node: &str) {
        let mut subs = self.subs.lock().unwrap_or_else(|p| p.into_inner());
        if subs.contains_key(node) {
            return;
        }
        let peer = (node != self.here).then(|| node.to_string());
        let (hub, changed, n) = (self.hub.clone(), self.changed.clone(), node.to_string());
        let (changed_h, n_h) = (changed.clone(), node.to_string());
        // A quiet topic is no reason to resubscribe, and so to re-read, on a timer.
        let opts = SubscriptionOpts { idle_timeout: Duration::from_secs(3600), ..SubscriptionOpts::default() };
        let sub = SupervisedSubscription::spawn(
            &format!("{}:sessions@{node}", self.name),
            opts,
            move || {
                let (hub, changed, peer, n) = (hub.clone(), changed.clone(), peer.clone(), n.clone());
                async move {
                    // Subscribe first, so a change while the catch-up runs waits in the socket.
                    let stream = hub.subscribe("reflection", "sessions", peer.as_deref(), None).await?;
                    changed(n).await;
                    Ok(stream)
                }
            },
            move |ev: Value| {
                let (changed, n) = (changed_h.clone(), n_h.clone());
                async move {
                    if matches!(ev.get("event").and_then(Value::as_str), Some("started" | "stopped")) {
                        changed(n).await;
                    }
                    Ok(())
                }
            },
        );
        subs.insert(node.to_string(), sub);
    }
}
