//! The `agent` orders alcor holds for this node. One held `order_pull` is the push channel. Each
//! order is checked against alcor's grant key, recorded before anything starts, and reported once.

use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::app::{is_ok, text, truthy, App};
use crate::caller::Frame;
use crate::gateway::{Call, CallError, Gateway};
use crate::store::File;

const WAIT_S: u64 = 55;
const PULL_TIMEOUT: Duration = Duration::from_secs(75);
const DONE_TIMEOUT: Duration = Duration::from_secs(30);
const BACKOFF_MIN: Duration = Duration::from_secs(2);
const BACKOFF_MAX: Duration = Duration::from_secs(60);
/// Long past any order's `not_after` (10 min), so a replay still finds its record.
const KEEP_MS: i64 = 24 * 3600 * 1000;
const NOTE_MAX: usize = 500;

/// The session name for an order: the title's first three words, under 40 characters.
fn session_name(payload: &Value) -> String {
    let task = text(payload, "task_id").unwrap_or("order");
    let name = text(payload, "title").map(|t| t.split_whitespace().take(3).collect::<Vec<_>>().join(" ")).filter(|n| !n.is_empty());
    name.unwrap_or_else(|| format!("task {task}")).chars().take(39).collect()
}

/// The first line of the started session. A note's line is fixed, so no vault text reaches it.
fn order_prompt(payload: &Value) -> String {
    if let Some(note) = text(payload, "note_id") {
        return format!("Read the note {note} as context only, then wait for Tony.");
    }
    let task = text(payload, "task_id").unwrap_or_default();
    match text(payload, "title") {
        Some(title) => format!("Task {task}: {title}"),
        None => format!("Task {task}"),
    }
}

/// The `start_other` args of one `agent` order.
fn order_start(body: &Value) -> Value {
    let p = &body["payload"];
    let mut start = json!({
        "task_id": p["task_id"], "role": "main", "remote_control": true, "suggest": true,
        "model": "opus", "effort": "high", "started_by": "Tony from the vault",
        "name": session_name(p), "prompt": order_prompt(p),
    });
    for key in ["project", "scope"] {
        if let Some(v) = p.get(key).filter(|v| truthy(Some(v))) {
            start[key] = v.clone();
        }
    }
    start
}

fn clip(note: String) -> String {
    note.chars().take(NOTE_MAX).collect()
}

impl<G: Gateway> App<G> {
    /// Hold one pull at a time for as long as the service runs. A failed pull backs off, and the
    /// first pull that answers after it resends the reports that could not be delivered.
    pub async fn order_loop(self: Arc<Self>, grant_key: [u8; 32]) {
        let mut connected = false;
        let mut backoff = BACKOFF_MIN;
        loop {
            match self.pull().await {
                Ok(envelopes) => {
                    backoff = BACKOFF_MIN;
                    if !connected {
                        self.resend_reports().await;
                        connected = true;
                    }
                    self.take_orders(&grant_key, envelopes).await;
                }
                Err(e) => {
                    connected = false;
                    tracing::warn!("reflection: order pull failed: {e}");
                    tokio::time::sleep(backoff).await;
                    backoff = (backoff * 2).min(BACKOFF_MAX);
                }
            }
        }
    }

    async fn pull(&self) -> Result<Vec<Value>, CallError> {
        let ask = json!({"node": self.node.name(), "kinds": ["agent"], "wait_s": WAIT_S});
        let out = self.gw.call(Call::new("operations", "order_pull", ask, PULL_TIMEOUT)).await?;
        Ok(out.as_array().cloned().unwrap_or_default())
    }

    /// Run each new verified order. A bad envelope gets one WARN and no `order_done`.
    pub async fn take_orders(self: &Arc<Self>, grant_key: &[u8; 32], envelopes: Vec<Value>) {
        for envelope in envelopes {
            let body = match awm_trust::grant::verify_order(&envelope, grant_key, &self.node.name(), self.node.now_ms() / 1000) {
                Ok(body) => body,
                Err(e) => {
                    tracing::warn!("reflection: dropped an order: {e}");
                    continue;
                }
            };
            let Some(id) = text(&body, "order_id").map(str::to_string) else {
                tracing::warn!("reflection: dropped an order with no order_id");
                continue;
            };
            if !self.record_order(&id).await {
                continue;
            }
            let (ok, note) = self.run_order(&body).await;
            self.report(&id, ok, &note).await;
        }
    }

    /// Record `id` before anything starts. False: seen before, or not recorded, so do not run it.
    async fn record_order(&self, id: &str) -> bool {
        let now = self.node.now_ms();
        let _guard = self.store.lock(File::Orders).await;
        let mut seen = self.store.read_object(File::Orders).await;
        if seen.contains_key(id) {
            return false;
        }
        seen.retain(|_, row| !truthy(row.get("reported")) || row["at_ms"].as_i64().unwrap_or(0) > now - KEEP_MS);
        seen.insert(id.to_string(), json!({"at_ms": now}));
        match self.store.write(File::Orders, &Value::Object(seen)).await {
            Ok(()) => true,
            Err(e) => {
                tracing::warn!("reflection: could not record order {id}, so it does not run: {e}");
                false
            }
        }
    }

    async fn run_order(self: &Arc<Self>, body: &Value) -> (bool, String) {
        if body["kind"] != "agent" {
            return (false, format!("this node takes agent orders only, not {}", body["kind"]));
        }
        let out = self.handle("start_other", &Frame::order(), &order_start(body)).await;
        if is_ok(&out) {
            (true, format!("job {} session {}", text(&out, "job").unwrap_or("?"), text(&out, "session_id").unwrap_or("?")))
        } else {
            (false, clip(out.get("reason").or(out.get("error")).unwrap_or(&out).to_string()))
        }
    }

    /// Note the outcome and send the one `order_done`. A failed send stays spooled in the record.
    async fn report(&self, id: &str, ok: bool, note: &str) {
        self.update_order(id, json!({"ok": ok, "note": note})).await;
        self.send_done(id, ok, note).await;
    }

    async fn send_done(&self, id: &str, ok: bool, note: &str) {
        let done = json!({"order_id": id, "ok": ok, "note": note});
        match self.gw.call(Call::new("operations", "order_done", done, DONE_TIMEOUT)).await {
            Ok(_) => self.update_order(id, json!({"reported": true})).await,
            Err(e) => tracing::warn!("reflection: order {id} done not sent, kept for the next connect: {e}"),
        }
    }

    async fn update_order(&self, id: &str, fields: Value) {
        let _guard = self.store.lock(File::Orders).await;
        let mut seen = self.store.read_object(File::Orders).await;
        let Some(Value::Object(row)) = seen.get_mut(id) else { return };
        row.extend(fields.as_object().cloned().unwrap_or_default());
        if let Err(e) = self.store.write(File::Orders, &Value::Object(seen)).await {
            tracing::warn!("reflection: could not update order {id}: {e}");
        }
    }

    /// Send what the last run could not. A record with no outcome stopped mid-start.
    pub async fn resend_reports(&self) {
        let pending: Map<String, Value> = self.store.read_object(File::Orders).await.into_iter().filter(|(_, r)| !truthy(r.get("reported"))).collect();
        for (id, row) in pending {
            let ok = truthy(row.get("ok"));
            let note = text(&row, "note").unwrap_or("reflection stopped before the order's outcome was recorded");
            self.send_done(&id, ok, note).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_prompt_and_name_come_from_the_payload() {
        let noted = json!({"task_id": "T1", "note_id": "n7", "title": "x"});
        assert_eq!(order_prompt(&noted), "Read the note n7 as context only, then wait for Tony.");
        assert_eq!(order_prompt(&json!({"task_id": "T1", "title": "Fix it"})), "Task T1: Fix it");
        assert_eq!(session_name(&json!({"task_id": "T1", "title": "one two three four"})), "one two three");
        assert_eq!(session_name(&json!({"task_id": "T1"})), "task T1");
        assert!(session_name(&json!({"title": "a".repeat(80)})).chars().count() < 40);
    }
}
