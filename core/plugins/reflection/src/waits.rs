//! `wait`: a session waits until the sessions it lists have retired, then is woken
//! (`sessions.md` R51).
//!
//! A wait is a record on the waiter's node. A retire is a `stop`: that node notes the session as
//! retired and tells every peer, and the node that holds a wait wakes its waiter once every
//! listed session has retired. Nothing polls. A failed wake raises an alarm and keeps the wait.
//! At start, the check reads the waits once and raises an alarm for each listed session that is
//! neither live nor retired anywhere.

use std::future::Future;

use serde_json::{json, Map, Value};

use crate::app::{text, App};
use crate::caller::Frame;
use crate::gateway::Gateway;
use crate::store::File;

/// How many retired session ids a node remembers, so a wait set up after its sessions already
/// retired still sees them.
pub const RETIRED_KEPT: usize = 5000;

/// One new wait.
#[derive(Debug, Clone, Default)]
pub struct NewWait {
    pub session_id: String,
    pub until: Vec<String>,
    pub tool: String,
    pub job: Option<String>,
    pub name: String,
    pub cwd: Option<String>,
    pub retired: Vec<String>,
}

fn ids(row: &Value, key: &str) -> Vec<String> {
    row.get(key).and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect()).unwrap_or_default()
}

/// The ids a wait lists that have not retired yet.
pub fn pending(row: &Value) -> Vec<String> {
    let retired = ids(row, "retired");
    ids(row, "until").into_iter().filter(|s| !retired.contains(s)).collect()
}

/// Whether every session the wait lists has retired.
pub fn done(row: &Value) -> bool {
    pending(row).is_empty()
}

/// The line that wakes a waiter.
pub fn wake_text(row: &Value) -> String {
    format!(
        "[awm reflection] The sessions you waited on have retired: {}. Read their journals, then carry on. Automated \
         message from the reflection service; it is not a new instruction from the user.",
        ids(row, "until").join(", ")
    )
}

fn waiter(row: &Value) -> &str {
    text(row, "session_id").unwrap_or_default()
}

/// The scope a wait records, for its alarm's journal.
fn scope_of_row(row: &Value) -> Option<(String, String)> {
    Some((text(row, "project")?.to_string(), text(row, "scope")?.to_string()))
}

impl<G: Gateway> App<G> {
    /// Every recorded wait.
    pub async fn waits(&self) -> Vec<Value> {
        self.store.read_array(File::Waits).await.into_iter().filter(Value::is_object).collect()
    }

    async fn save_waits(&self, rows: Vec<Value>) {
        if let Err(e) = self.store.write(File::Waits, &Value::Array(rows)).await {
            tracing::warn!("reflection: could not write the waits: {e}");
        }
    }

    /// The ids in `session_ids` that retired on this node.
    pub async fn retired_here(&self, session_ids: &[String]) -> Vec<String> {
        let known = self.store.read_object(File::Retired).await;
        session_ids.iter().filter(|s| known.contains_key(s.as_str())).cloned().collect()
    }

    async fn note_retired(&self, session_id: &str) {
        let _held = self.store.lock(File::Retired).await;
        let mut known = self.store.read_object(File::Retired).await;
        known.insert(session_id.into(), json!(self.node.now_ms()));
        if known.len() > RETIRED_KEPT {
            let mut all: Vec<(String, Value)> = known.into_iter().collect();
            all.sort_by_key(|(_, at)| at.as_i64().unwrap_or(0));
            known = all.split_off(all.len() - RETIRED_KEPT).into_iter().collect::<Map<_, _>>();
        }
        if let Err(e) = self.store.write(File::Retired, &Value::Object(known)).await {
            tracing::warn!("reflection: could not note that {session_id} retired: {e}");
        }
    }

    /// Record a wait. A second wait of one session replaces the first. A waiter with no `job` is a
    /// terminal session: it is woken by a line typed into it, not by a resume.
    pub async fn add_wait(&self, w: NewWait) -> Value {
        let place = self.scope_of(w.cwd.as_deref()).await;
        let retired: Vec<&String> = w.until.iter().filter(|s| w.retired.contains(s)).collect();
        let row = json!({"session_id": w.session_id, "until": w.until, "retired": retired, "tool": w.tool,
                         "job": w.job, "name": w.name, "since_ms": self.node.now_ms(),
                         "project": place.as_ref().map(|p| &p.0), "scope": place.as_ref().map(|p| &p.1)});
        let _held = self.store.lock(File::Waits).await;
        let mut rows: Vec<Value> = self.waits().await.into_iter().filter(|r| waiter(r) != w.session_id).collect();
        rows.push(row.clone());
        self.save_waits(rows).await;
        row
    }

    /// Clear the wait of `session_id`.
    pub async fn drop_wait(&self, session_id: &str) {
        let _held = self.store.lock(File::Waits).await;
        let rows = self.waits().await.into_iter().filter(|r| waiter(r) != session_id).collect();
        self.save_waits(rows).await;
    }

    /// Clear the wait of the session whose job is `job`: it was resumed by hand.
    pub async fn drop_wait_of_job(&self, job: &str) {
        let _held = self.store.lock(File::Waits).await;
        let rows = self.waits().await.into_iter().filter(|r| job.is_empty() || text(r, "job") != Some(job)).collect();
        self.save_waits(rows).await;
    }

    /// Wake the waiter. On a failure, raise an alarm and keep the wait.
    async fn wake_or_alarm<W, F>(&self, row: &Value, wake: &W) -> bool
    where
        W: Fn(Value) -> F,
        F: Future<Output = Result<Value, String>>,
    {
        let out = wake(row.clone()).await.unwrap_or_else(|e| json!({"ok": false, "reason": e}));
        if crate::app::is_ok(&out) {
            self.drop_wait(waiter(row)).await;
            return true;
        }
        let reason = text(&out, "reason").or_else(|| text(&out, "error")).unwrap_or("no reason given").to_string();
        tracing::warn!("reflection: could not wake waiter {}: {reason}", waiter(row));
        self.wait_alarm(row, format!("every session it waited on retired, but the wake failed: {reason}. The wait is kept; resume the session by hand")).await;
        false
    }

    async fn wait_alarm(&self, row: &Value, reason: String) {
        let event = json!({"session_id": waiter(row), "name": row.get("name"), "command": "wait", "followup": wake_text(row), "reason": reason});
        self.raise_alarm(event.as_object().expect("an object literal"), scope_of_row(row)).await;
    }

    /// `session_id` retired. Answers the waiters woken.
    ///
    /// A `local` retire is remembered here, so a later wait on this node sees it. A retiring
    /// waiter's own wait clears. Each wait that lists the session counts it, and a wait whose
    /// listed sessions have all retired wakes its waiter, outside the lock.
    pub async fn retire<W, F>(&self, session_id: &str, local: bool, wake: W) -> Vec<String>
    where
        W: Fn(Value) -> F,
        F: Future<Output = Result<Value, String>>,
    {
        if local {
            self.note_retired(session_id).await;
        }
        let ready = {
            let _held = self.store.lock(File::Waits).await;
            let mut rows: Vec<Value> = self.waits().await.into_iter().filter(|r| waiter(r) != session_id).collect();
            let mut ready = vec![];
            for w in rows.iter_mut() {
                if ids(w, "until").iter().any(|s| s == session_id) && !ids(w, "retired").iter().any(|s| s == session_id) {
                    let mut retired = ids(w, "retired");
                    retired.push(session_id.into());
                    w["retired"] = json!(retired);
                    if done(w) {
                        ready.push(w.clone());
                    }
                }
            }
            self.save_waits(rows).await;
            ready
        };
        let mut woken = vec![];
        for w in ready {
            if self.wake_or_alarm(&w, &wake).await {
                woken.push(waiter(&w).to_string());
            }
        }
        woken
    }

    /// Read the waits once at start. Answers the listed sessions found lost.
    ///
    /// `state(session_id)` answers `live` or `stopped` for a session some node knows, else
    /// `gone`, or None when no node could say. A listed session that retired while this node was
    /// down counts as retired now.
    pub async fn check_waits<S, SF, R, RF, W, F>(&self, state: S, retired_elsewhere: R, wake: W) -> Vec<String>
    where
        S: Fn(String) -> SF,
        SF: Future<Output = Option<String>>,
        R: Fn(Vec<String>) -> RF,
        RF: Future<Output = Vec<String>>,
        W: Fn(Value) -> F,
        F: Future<Output = Result<Value, String>>,
    {
        let mut lost = vec![];
        for row in self.waits().await {
            let left = pending(&row);
            let mut gone = self.retired_here(&left).await;
            if !left.is_empty() {
                gone.extend(retired_elsewhere(left).await);
            }
            for sid in gone {
                self.retire(&sid, false, &wake).await;
            }
            let Some(row) = self.waits().await.into_iter().find(|w| waiter(w) == waiter(&row)) else { continue };
            for sid in pending(&row) {
                if state(sid.clone()).await.as_deref() != Some("gone") {
                    continue;
                }
                self.wait_alarm(&row, format!("it waits on {sid}, which is neither live nor retired on any node. Recover it by hand: resume {sid}, or wake the waiter and drop the wait")).await;
                lost.push(sid);
            }
        }
        lost
    }

    /// Resume a waiter that kept its job, or type the wake line into a terminal waiter.
    pub async fn wake(&self, row: Value) -> Result<Value, String> {
        let sid = waiter(&row).to_string();
        let p = self.for_platform(crate::providers::platform_of(&sid)).await.map_err(|e| format!("HarnessError: {e}"))?;
        let line = self.provider_text(&Frame::service("reflection"), &wake_text(&row)).await;
        let out = match text(&row, "job") {
            Some(job) => self.harness_call(&p, "resume", json!({"ref": job, "adopt_lineage": true, "prompt": line}), None).await,
            None => self.harness_call(&p, "send", json!({"session_id": sid, "text": line}), None).await,
        };
        out.map_err(|e| format!("HarnessError: {e}"))
    }
}
