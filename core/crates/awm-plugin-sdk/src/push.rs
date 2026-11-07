//! The push loop of channels.md R69: lines typed into a holder's background job, in order per
//! holder (`gatewayclient/push.py`). social's seats and the channel plugin deliver through it.
//!
//! A [`Pusher`] never drops a line. When the holder is gone, or refuses past the retry window,
//! the line and every line queued behind it go back to the owner through [`Pusher::new`]'s
//! `on_return`, in order, so the owner can requeue them for the next holder.

use std::collections::{HashMap, HashSet, VecDeque};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::Notify;

use crate::holders::row_live;
use crate::hub::{CallOpts, Hub, HubError};

/// What reflection said to one line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Delivered,
    /// The holder refused or could not be reached. The line is sent again.
    Retry(String),
    /// The holder's job no longer exists.
    Gone,
}

/// Why lines came back to the owner.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Returned {
    Gone,
    /// The holder refused every send for the whole retry window.
    Exhausted,
}

/// A holder: the node it runs on and its job id.
pub type Key = (String, String);

pub type SendFuture = Pin<Box<dyn Future<Output = Outcome> + Send>>;
pub type SendFn = Arc<dyn Fn(String, String, String) -> SendFuture + Send + Sync>;
pub type ReturnFn = Arc<dyn Fn(Key, Vec<String>, Returned) + Send + Sync>;

/// Reasons a reflection older than `code: "gone"` gives for a missing job.
const GONE_MARKERS: [&str; 2] = ["no session on this node has the job id", "is not running"];

#[derive(Debug, Clone, Copy)]
pub struct PushOpts {
    /// How long one line is retried before it and the lines behind it go back.
    pub retry: Duration,
    pub first_delay: Duration,
    pub max_delay: Duration,
}

impl Default for PushOpts {
    fn default() -> Self {
        Self { retry: Duration::from_secs(120), first_delay: Duration::from_secs(2), max_delay: Duration::from_secs(30) }
    }
}

fn peer_for(node: &str) -> Option<String> {
    (node != crate::config::node_name()).then(|| node.to_string())
}

fn gone_reason(v: &Value) -> bool {
    if v.get("code").and_then(Value::as_str) == Some("gone") {
        return true;
    }
    let reason = v.get("error").or_else(|| v.get("reason")).map(|r| r.as_str().map(str::to_string).unwrap_or_else(|| r.to_string())).unwrap_or_default();
    GONE_MARKERS.iter().any(|m| reason.contains(m))
}

/// Type `text` into `job` on `node` through `reflection message`.
pub async fn reflect(hub: &Hub, node: &str, job: &str, text: &str) -> Outcome {
    let opts = CallOpts { peer: peer_for(node), timeout: Duration::from_secs(60), ..CallOpts::default() };
    match hub.call_with("reflection", "message", json!({"job": job, "text": text}), &opts).await {
        Ok(v) if v.get("ok").and_then(Value::as_bool) == Some(true) => Outcome::Delivered,
        Ok(v) if gone_reason(&v) => Outcome::Gone,
        Ok(v) => Outcome::Retry(v.to_string()),
        Err(e @ HubError::Verb { .. }) if e.body().is_some_and(|b| gone_reason(b) || gone_reason(b.get("detail").unwrap_or(b))) => Outcome::Gone,
        Err(HubError::Verb { error, .. }) if GONE_MARKERS.iter().any(|m| error.contains(m)) => Outcome::Gone,
        Err(e) => Outcome::Retry(e.to_string()),
    }
}

/// The running background jobs on `node` across its harnesses, or None when it cannot tell.
/// Any `errors[]` entry means some harness did not answer, so it cannot tell.
pub async fn live_jobs(hub: &Hub, node: &str) -> Option<HashSet<String>> {
    let opts = CallOpts { peer: peer_for(node), timeout: Duration::from_secs(60), ..CallOpts::default() };
    let reply = hub.call_with("reflection", "list_other", json!({}), &opts).await.ok()?;
    let errors = reply.get("errors").and_then(Value::as_array).is_some_and(|e| !e.is_empty());
    let rows = reply.get("sessions")?.as_array()?;
    if errors {
        return None;
    }
    Some(
        rows.iter()
            .filter(|r| row_live(r))
            .filter_map(|r| r.get("job").and_then(Value::as_str).filter(|j| !j.is_empty()).map(str::to_string))
            .collect(),
    )
}

struct State {
    queues: HashMap<Key, VecDeque<String>>,
    draining: HashSet<Key>,
}

struct Inner {
    send: SendFn,
    on_return: ReturnFn,
    opts: PushOpts,
    state: Mutex<State>,
    idle: Notify,
}

/// One ordered queue per holder, each drained by its own task.
#[derive(Clone)]
pub struct Pusher(Arc<Inner>);

impl Pusher {
    pub fn new(send: SendFn, on_return: ReturnFn, opts: PushOpts) -> Self {
        let state = Mutex::new(State { queues: HashMap::new(), draining: HashSet::new() });
        Pusher(Arc::new(Inner { send, on_return, opts, state, idle: Notify::new() }))
    }

    /// A pusher that types through `reflection message` on `hub`.
    pub fn reflection(hub: Hub, on_return: ReturnFn) -> Self {
        let send: SendFn = Arc::new(move |node, job, line| {
            let hub = hub.clone();
            Box::pin(async move { reflect(&hub, &node, &job, &line).await })
        });
        Self::new(send, on_return, PushOpts::default())
    }

    pub fn push(&self, node: &str, job: &str, line: &str) {
        let key = (node.to_string(), job.to_string());
        let start = {
            let mut st = self.0.state.lock().unwrap();
            st.queues.entry(key.clone()).or_default().push_back(line.to_string());
            st.draining.insert(key.clone())
        };
        if start {
            tokio::spawn(drain(self.0.clone(), key));
        }
    }

    /// The lines still queued for a holder, oldest first.
    pub fn pending(&self, node: &str, job: &str) -> Vec<String> {
        let st = self.0.state.lock().unwrap();
        st.queues.get(&(node.to_string(), job.to_string())).map(|q| q.iter().cloned().collect()).unwrap_or_default()
    }

    /// Wait until every queue is empty.
    pub async fn idle(&self) {
        loop {
            let notified = self.0.idle.notified();
            if self.0.state.lock().unwrap().draining.is_empty() {
                return;
            }
            notified.await;
        }
    }
}

async fn drain(inner: Arc<Inner>, key: Key) {
    loop {
        let line = {
            let mut st = inner.state.lock().unwrap();
            match st.queues.get(&key).and_then(|q| q.front().cloned()) {
                Some(line) => line,
                None => {
                    st.queues.remove(&key);
                    st.draining.remove(&key);
                    drop(st);
                    inner.idle.notify_waiters();
                    return;
                }
            }
        };
        let returned = match deliver(&inner, &key, &line).await {
            None => {
                if let Some(q) = inner.state.lock().unwrap().queues.get_mut(&key) {
                    q.pop_front();
                }
                continue;
            }
            Some(why) => why,
        };
        let lines: Vec<String> = {
            let mut st = inner.state.lock().unwrap();
            st.draining.remove(&key);
            st.queues.remove(&key).map(Vec::from).unwrap_or_default()
        };
        tracing::info!(node = %key.0, job = %key.1, lines = lines.len(), ?returned, "push lines returned to their owner");
        (inner.on_return)(key, lines, returned);
        inner.idle.notify_waiters();
        return;
    }
}

/// None once the line is delivered, else why it goes back.
async fn deliver(inner: &Inner, key: &Key, line: &str) -> Option<Returned> {
    let deadline = tokio::time::Instant::now() + inner.opts.retry;
    let mut delay = inner.opts.first_delay;
    loop {
        match (inner.send)(key.0.clone(), key.1.clone(), line.to_string()).await {
            Outcome::Delivered => return None,
            Outcome::Gone => return Some(Returned::Gone),
            Outcome::Retry(why) => {
                if tokio::time::Instant::now() + delay > deadline {
                    tracing::warn!(node = %key.0, job = %key.1, %why, "push refused for the whole retry window");
                    return Some(Returned::Exhausted);
                }
                tokio::time::sleep(delay).await;
                delay = (delay * 2).min(inner.opts.max_delay);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    type Log = Arc<Mutex<Vec<(Key, Vec<String>, Returned)>>>;

    fn fast() -> PushOpts {
        PushOpts { retry: Duration::from_millis(60), first_delay: Duration::from_millis(5), max_delay: Duration::from_millis(10) }
    }

    fn pusher(script: impl Fn(&str, &str) -> Outcome + Send + Sync + 'static) -> (Pusher, Arc<Mutex<Vec<String>>>, Log) {
        let sent = Arc::new(Mutex::new(Vec::new()));
        let back: Log = Arc::new(Mutex::new(Vec::new()));
        let (s, b) = (sent.clone(), back.clone());
        let script = Arc::new(script);
        let send: SendFn = Arc::new(move |_node, job, line| {
            let (s, script) = (s.clone(), script.clone());
            Box::pin(async move {
                let out = script(&job, &line);
                if out == Outcome::Delivered {
                    s.lock().unwrap().push(line);
                }
                out
            })
        });
        let on_return: ReturnFn = Arc::new(move |k, lines, why| b.lock().unwrap().push((k, lines, why)));
        (Pusher::new(send, on_return, fast()), sent, back)
    }

    #[tokio::test]
    async fn delivers_in_order() {
        let (p, sent, back) = pusher(|_, _| Outcome::Delivered);
        for i in 0..5 {
            p.push("n", "j", &format!("l{i}"));
        }
        p.idle().await;
        assert_eq!(*sent.lock().unwrap(), vec!["l0", "l1", "l2", "l3", "l4"]);
        assert!(back.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn gone_returns_the_line_and_all_behind_it() {
        let (p, sent, back) = pusher(|_, line| if line == "l2" { Outcome::Gone } else { Outcome::Delivered });
        for i in 0..5 {
            p.push("n", "j", &format!("l{i}"));
        }
        p.idle().await;
        assert_eq!(*sent.lock().unwrap(), vec!["l0", "l1"]);
        let back = back.lock().unwrap();
        assert_eq!(back.len(), 1);
        assert_eq!(back[0].1, vec!["l2", "l3", "l4"]);
        assert_eq!(back[0].2, Returned::Gone);
    }

    #[tokio::test]
    async fn exhausted_retries_return_rather_than_drop() {
        let (p, sent, back) = pusher(|_, _| Outcome::Retry("busy".into()));
        p.push("n", "j", "a");
        p.push("n", "j", "b");
        p.idle().await;
        assert!(sent.lock().unwrap().is_empty());
        let back = back.lock().unwrap();
        assert_eq!((back[0].1.clone(), back[0].2), (vec!["a".to_string(), "b".to_string()], Returned::Exhausted));
    }

    #[tokio::test]
    async fn retry_then_deliver_keeps_order() {
        let tries = Arc::new(Mutex::new(0));
        let t = tries.clone();
        let (p, sent, back) = pusher(move |_, line| {
            let mut n = t.lock().unwrap();
            *n += 1;
            if line == "a" && *n < 3 { Outcome::Retry("x".into()) } else { Outcome::Delivered }
        });
        p.push("n", "j", "a");
        p.push("n", "j", "b");
        p.idle().await;
        assert_eq!(*sent.lock().unwrap(), vec!["a", "b"]);
        assert!(back.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn holders_drain_independently() {
        let (p, sent, back) = pusher(|job, _| if job == "dead" { Outcome::Gone } else { Outcome::Delivered });
        p.push("n", "dead", "x");
        p.push("n", "live", "y");
        p.idle().await;
        assert_eq!(*sent.lock().unwrap(), vec!["y"]);
        assert_eq!(back.lock().unwrap()[0].0, ("n".to_string(), "dead".to_string()));
    }

    #[test]
    fn gone_is_read_from_code_or_legacy_reason() {
        assert!(gone_reason(&json!({"ok": false, "code": "gone"})));
        assert!(gone_reason(&json!({"ok": false, "error": "job abc is not running"})));
        assert!(!gone_reason(&json!({"ok": false, "error": "busy"})));
    }
}
