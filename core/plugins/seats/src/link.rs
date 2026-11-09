//! The connection this node opens to social on alcor. alcor has no route here, so this end does
//! all the dialing.
//!
//! Down: one held `seat_pull`, which carries the id of the last row stored here as its ack. Each
//! row is stored before it is acked, so a lost answer costs a resend and never a row. Up: the
//! outbox, sent in order, one row per `seat_report`. A loss of the link only delays both: seat
//! state and each agent's life are read on this node.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::{Backoff, CallOpts, Hub};
use serde_json::{json, Value};
use tokio::sync::{oneshot, Notify};

use crate::engine::{Incoming, Seats, Settings};
use crate::store::{Decl, Seed, Store};

const WAIT_S: u64 = 55;
const PULL_TIMEOUT: Duration = Duration::from_secs(75);
const REPORT_TIMEOUT: Duration = Duration::from_secs(120);
const BACKOFF_MIN_S: f64 = 2.0;
const BACKOFF_MAX_S: f64 = 60.0;
/// How long `seats send` waits for the answer before it says the reply is queued.
const SEND_WAIT: Duration = Duration::from_secs(8);
/// How long a row social keeps asking to resend stays in line. Only a reachable social asks.
const GIVE_UP_S: f64 = 3600.0;

/// The two calls to social.
#[async_trait]
pub trait Alcor: Send + Sync {
    async fn pull(&self, ack_through: i64, wait_s: u64) -> Result<Value, String>;
    async fn report(&self, row: Value) -> Result<Value, String>;
}

/// social through its singleton door.
pub struct ThroughDoor(pub Hub);

#[async_trait]
impl Alcor for ThroughDoor {
    async fn pull(&self, ack_through: i64, wait_s: u64) -> Result<Value, String> {
        let opts = CallOpts { timeout: PULL_TIMEOUT, ..CallOpts::default() };
        self.0.call_with("social", "seat_pull", json!({"ack_through": ack_through, "wait_s": wait_s}), &opts).await.map_err(|e| e.to_string())
    }

    async fn report(&self, row: Value) -> Result<Value, String> {
        let opts = CallOpts { timeout: REPORT_TIMEOUT, ..CallOpts::default() };
        self.0.call_with("social", "seat_report", row, &opts).await.map_err(|e| e.to_string())
    }
}

/// What one flush did.
#[derive(Debug, PartialEq)]
pub enum Flushed {
    Empty,
    Sent,
    Retry(String),
}

pub struct Link {
    alcor: Arc<dyn Alcor>,
    store: Store,
    epoch: String,
    seats: OnceLock<Arc<Seats>>,
    now: fn() -> f64,
    out: Notify,
    connected: AtomicBool,
    down_since: Mutex<f64>,
    waiting: Mutex<HashMap<i64, oneshot::Sender<Value>>>,
}

impl Link {
    /// `epoch` names this database, so social starts a rebuilt one's row ids over.
    pub fn new(alcor: Arc<dyn Alcor>, store: Store, epoch: String, now: fn() -> f64) -> Arc<Self> {
        Arc::new(Self { alcor, store, epoch, seats: OnceLock::new(), now, out: Notify::new(), connected: AtomicBool::new(false), down_since: Mutex::new(0.0), waiting: Mutex::default() })
    }

    pub fn attach(&self, seats: Arc<Seats>) {
        let _ = self.seats.set(seats);
    }

    fn seats(&self) -> &Arc<Seats> {
        self.seats.get().expect("attached before the loops start")
    }

    /// Queue one row for social.
    pub async fn enqueue(&self, kind: &str, body: &Value) -> i64 {
        let id = self.store.outbox_push(kind, body, (self.now)()).await;
        self.out.notify_one();
        id
    }

    /// Queue a reply and wait briefly for its answer. A slow link is no failure: the row stays.
    pub async fn send(&self, body: &Value) -> Value {
        let (tx, rx) = oneshot::channel();
        let id = self.store.outbox_push("send", body, (self.now)()).await;
        self.waiting.lock().unwrap().insert(id, tx);
        self.out.notify_one();
        match tokio::time::timeout(SEND_WAIT, rx).await {
            Ok(Ok(answer)) => answer,
            _ => json!({"ok": true, "queued": true, "note": "the link to the chat is slow or down. Your reply is queued on this node and goes out, once, when it is back. Do not send it again."}),
        }
    }

    pub async fn status(&self) -> Value {
        let down_since = *self.down_since.lock().unwrap();
        json!({"connected": self.connected.load(Ordering::SeqCst), "down_since": if down_since > 0.0 { json!(down_since) } else { Value::Null }, "outbox": self.store.outbox_len().await})
    }

    fn link_up(&self) {
        *self.down_since.lock().unwrap() = 0.0;
        if !self.connected.swap(true, Ordering::SeqCst) {
            self.out.notify_one();
        }
    }

    fn link_down(&self) {
        self.connected.store(false, Ordering::SeqCst);
        let mut since = self.down_since.lock().unwrap();
        if *since == 0.0 {
            *since = (self.now)();
        }
    }

    /// Hold one pull, then store what it brought.
    pub async fn pull_once(&self) -> Result<(), String> {
        let mark: i64 = self.store.meta("pull_mark").await.and_then(|m| m.parse().ok()).unwrap_or(0);
        let reply = match self.alcor.pull(mark, WAIT_S).await {
            Ok(r) => r,
            Err(e) => {
                self.link_down();
                return Err(e);
            }
        };
        self.link_up();
        if let Some(s) = reply.get("settings").and_then(|s| serde_json::from_value::<Settings>(s.clone()).ok()) {
            self.seats().configure(s).await;
        }
        for row in reply.get("rows").and_then(Value::as_array).into_iter().flatten() {
            let id = row["id"].as_i64().unwrap_or(0);
            if id <= mark {
                continue;
            }
            self.apply(row).await;
            self.store.set_meta("pull_mark", &id.to_string()).await;
        }
        Ok(())
    }

    async fn apply(&self, row: &Value) {
        let seats = self.seats();
        let decl = || serde_json::from_value::<Decl>(row["seat"].clone()).unwrap_or_default();
        let text = |k: &str| row[k].as_str().unwrap_or("").to_string();
        match row["kind"].as_str().unwrap_or("") {
            "line" => match serde_json::from_value::<Incoming>(row.clone()) {
                Ok(m) => seats.take(m).await,
                Err(e) => tracing::warn!("seats: a line row did not parse: {e}"),
            },
            "seed" => seats.seed(decl(), serde_json::from_value::<Seed>(row["state"].clone()).unwrap_or_default()).await,
            "adopt" => {
                seats.adopt(&decl(), &text("job"), &text("session_name")).await;
            }
            "released" => seats.released(&text("account"), &text("channel_id"), &text("job")).await,
            "remote_control" => {
                seats.set_remote_control(&text("account"), &text("channel_id"), row["on"].as_bool().unwrap_or(false)).await;
            }
            "hand" => seats.hand(&text("job"), &text("line")),
            other => tracing::warn!("seats: social sent a row of unknown kind {other:?}"),
        }
    }

    /// Send the outbox's first row.
    pub async fn flush_once(&self) -> Flushed {
        let Some(row) = self.store.outbox_first().await else { return Flushed::Empty };
        let args = json!({"epoch": self.epoch, "id": row.id, "kind": row.kind, "body": row.body});
        let answer = match self.alcor.report(args).await {
            Ok(a) => a,
            Err(e) => return Flushed::Retry(e),
        };
        let wants_retry = answer.get("retry").and_then(Value::as_bool) == Some(true);
        if wants_retry && (self.now)() - row.at < GIVE_UP_S {
            return Flushed::Retry(answer.get("error").and_then(Value::as_str).unwrap_or("social asked for a resend").to_string());
        }
        if answer.get("ok").and_then(Value::as_bool) != Some(true) {
            tracing::warn!("seats: social refused a {} row: {answer}", row.kind);
        }
        self.store.outbox_delete(row.id).await;
        if let Some(tx) = self.waiting.lock().unwrap().remove(&row.id) {
            let _ = tx.send(answer);
        }
        Flushed::Sent
    }

    pub async fn pull_forever(self: Arc<Self>) {
        let mut backoff = Backoff::doubling(BACKOFF_MIN_S, BACKOFF_MAX_S);
        loop {
            match self.pull_once().await {
                Ok(()) => backoff.reset(),
                Err(e) => {
                    tracing::info!("seats: the pull from social failed: {e}");
                    tokio::time::sleep(backoff.wait()).await;
                }
            }
        }
    }

    pub async fn flush_forever(self: Arc<Self>) {
        let mut backoff = Backoff::doubling(BACKOFF_MIN_S, BACKOFF_MAX_S);
        loop {
            match self.flush_once().await {
                Flushed::Empty => {
                    backoff.reset();
                    self.out.notified().await;
                }
                Flushed::Sent => backoff.reset(),
                Flushed::Retry(why) => {
                    tracing::info!("seats: a row for social waits: {why}");
                    tokio::select! {
                        _ = tokio::time::sleep(backoff.wait()) => {}
                        _ = self.out.notified() => {}
                    }
                }
            }
        }
    }
}
