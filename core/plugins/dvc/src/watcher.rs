//! Watches every live run to a verdict. routine's timers fire the jobs through
//! `dvc run-job`, and the verbs submit directly. Either way a live row appears in
//! `dvc_runs`, and the adopt sweep gives every live row that no watcher holds one here.
//! That one rule covers a service restart mid-transfer, a killed `run-job`, a manual verb
//! and a row orphaned between its insert and its submit. Adoption never resubmits: the task
//! id is authoritative, and a retry would duplicate a multi-hour transfer.
//!
//! Globus has no push, so a verdict costs a poll. The row is always written before the
//! `job.status` event that reports it: events are best-effort, the row is the record.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::globus::{self, Api};
use crate::runs::{Row, RunsDao};
use crate::{jobs, now};

pub const TOPIC: &str = "job.status";
/// A live row with no task id this long never got submitted. This process's own submits
/// are exempt through `jobs::submitting`.
pub const ORPHAN_AFTER_S: f64 = 3600.0;
/// A daily job still running past the next one's slot.
pub const STALL_AFTER_S: f64 = 26.0 * 3600.0;
pub const PROGRESS_EMIT_S: f64 = 120.0;
/// Consecutive failed polls before a watcher gives up: five minutes at the default poll,
/// enough to ride out a token refresh or an API blip.
pub const MAX_POLL_FAILURES: u32 = 10;

pub type Emit = Arc<dyn Fn(Value) + Send + Sync>;

#[derive(Clone)]
pub struct Timing {
    pub poll: Duration,
    pub sweep: Duration,
}

impl Default for Timing {
    fn default() -> Self {
        Self { poll: Duration::from_secs(30), sweep: Duration::from_secs(30) }
    }
}

pub struct Watcher {
    pub dao: Arc<RunsDao>,
    pub api: Arc<dyn Api>,
    pub emit: Emit,
    pub timing: Timing,
    watching: Mutex<HashMap<String, tokio::task::JoinHandle<()>>>,
    last_sweep: Mutex<Option<Instant>>,
    sweeps: Mutex<u64>,
}

fn s<'a>(r: &'a Row, k: &str) -> &'a str {
    r.get(k).and_then(Value::as_str).unwrap_or("")
}

fn f(r: &Row, k: &str) -> f64 {
    r.get(k).and_then(Value::as_f64).unwrap_or(0.0)
}

async fn blocking<T: Send + 'static>(work: impl FnOnce() -> T + Send + 'static) -> T {
    match tokio::task::spawn_blocking(work).await {
        Ok(v) => v,
        Err(e) => std::panic::resume_unwind(e.into_panic()),
    }
}

/// The `job.status` payload.
pub fn payload(status: &str, run: &Row, message: &str, doc: Option<&Value>) -> Value {
    let started = f(run, "started_at");
    let t = now();
    let mut p = json!({
        "run_id": s(run, "id"),
        "job": s(run, "job"),
        "trigger": s(run, "trigger"),
        "status": status,
        "task_id": s(run, "task_id"),
        "timestamp": t,
        "elapsed_s": if started > 0.0 { json!(((t - started) * 10.0).round() / 10.0) } else { Value::Null },
        "message": message,
    });
    if let Some(d) = doc {
        for k in ["files", "files_transferred", "files_skipped", "bytes_transferred", "faults", "nice_status"] {
            p[k] = d.get(k).cloned().unwrap_or(Value::Null);
        }
    }
    p
}

/// The skipped-error summary a run's note carries: `<n> source path(s) skipped (<CODE>×<n>, ...); first: <path>`.
pub fn skipped_note(skipped: &[Value]) -> String {
    let mut codes: BTreeMap<String, usize> = BTreeMap::new();
    for e in skipped {
        let c = match e.get("error_code") {
            Some(Value::String(c)) if !c.is_empty() => c.clone(),
            _ => "?".into(),
        };
        *codes.entry(c).or_default() += 1;
    }
    let summary = codes.iter().map(|(c, n)| format!("{c}×{n}")).collect::<Vec<_>>().join(", ");
    let first = skipped.first().and_then(|e| e.get("source_path")).and_then(Value::as_str).unwrap_or("");
    format!("{} source path(s) skipped ({summary}); first: {first}", skipped.len())
}

impl Watcher {
    pub fn new(dao: Arc<RunsDao>, api: Arc<dyn Api>, emit: Emit, timing: Timing) -> Arc<Self> {
        Arc::new(Self { dao, api, emit, timing, watching: Mutex::new(HashMap::new()), last_sweep: Mutex::new(None), sweeps: Mutex::new(0) })
    }

    /// What `jobs` reports about the watcher itself.
    pub fn health(&self) -> Value {
        let alive = self.watching.lock().unwrap_or_else(|p| p.into_inner()).values().filter(|h| !h.is_finished()).count();
        let age = self.last_sweep.lock().unwrap_or_else(|p| p.into_inner()).map(|t| (t.elapsed().as_secs_f64() * 10.0).round() / 10.0);
        let limit = self.timing.sweep.as_secs_f64() * 2.0 + 30.0;
        json!({
            "watching": alive,
            "sweeps": *self.sweeps.lock().unwrap_or_else(|p| p.into_inner()),
            "last_sweep_age_s": age,
            "stopped": age.is_none_or(|a| a > limit),
        })
    }

    fn start_watch(self: &Arc<Self>, run: Row) {
        let id = s(&run, "id").to_string();
        let mut w = self.watching.lock().unwrap_or_else(|p| p.into_inner());
        if w.get(&id).is_some_and(|h| !h.is_finished()) {
            return;
        }
        let me = self.clone();
        w.insert(id, tokio::spawn(async move { me.watch(run).await }));
    }

    fn watched(&self, id: &str) -> bool {
        self.watching.lock().unwrap_or_else(|p| p.into_inner()).get(id).is_some_and(|h| !h.is_finished())
    }

    /// Attach a watcher to every live run that has none.
    pub async fn sweep(self: &Arc<Self>) -> anyhow::Result<()> {
        let dao = self.dao.clone();
        let live = blocking(move || dao.live_runs()).await?;
        let t = now();
        let mine = jobs::submitting();
        for run in live {
            let id = s(&run, "id").to_string();
            if self.watched(&id) {
                continue;
            }
            if s(&run, "task_id").is_empty() {
                if mine.contains(&id) || t - f(&run, "started_at") <= ORPHAN_AFTER_S {
                    continue;
                }
                let msg = "orphaned: the run claimed the slot but no Globus task was ever recorded against it, so nothing was submitted";
                let (dao, rid) = (self.dao.clone(), id.clone());
                blocking(move || dao.fail(&rid, msg)).await?;
                tracing::error!("dvc: {} run {id} {msg}", s(&run, "job"));
                (self.emit)(payload("error", &run, msg, None));
                continue;
            }
            tracing::info!("dvc: adopting live run {id} ({}, task {})", s(&run, "job"), s(&run, "task_id"));
            self.start_watch(run);
        }
        *self.last_sweep.lock().unwrap_or_else(|p| p.into_inner()) = Some(Instant::now());
        *self.sweeps.lock().unwrap_or_else(|p| p.into_inner()) += 1;
        Ok(())
    }

    /// Sweep now and then on every tick, for the life of the service. A failed sweep is
    /// logged and skipped: a watcher that quietly stopped is how a backup stops reaching a
    /// verdict.
    pub async fn run(self: Arc<Self>) {
        loop {
            if let Err(e) = self.sweep().await {
                tracing::warn!("dvc: adopt sweep failed: {e:#}");
            }
            tokio::time::sleep(self.timing.sweep).await;
        }
    }

    async fn note_skipped(&self, run: &Row, doc: &Value) -> Vec<Value> {
        let (api, task) = (self.api.clone(), s(run, "task_id").to_string());
        let skipped = match blocking(move || globus::skipped_errors(api.as_ref(), &task)).await {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!("dvc: could not read skipped errors for {}: {e}", s(run, "task_id"));
                return vec![];
            }
        };
        if skipped.is_empty() {
            return skipped;
        }
        let note = skipped_note(&skipped);
        let (dao, rid, n) = (self.dao.clone(), s(run, "id").to_string(), note.clone());
        if let Err(e) = blocking(move || dao.set_note(&rid, &n)).await {
            tracing::warn!("dvc: could not note skipped paths: {e}");
        }
        let moved = doc.get("files_transferred").and_then(Value::as_i64).unwrap_or(0);
        if moved == 0 {
            tracing::error!("dvc: {} task {} {note}, having transferred nothing", s(run, "job"), s(run, "task_id"));
        } else {
            tracing::warn!("dvc: {} task {} {note}, having transferred {moved}", s(run, "job"), s(run, "task_id"));
        }
        skipped
    }

    /// Poll one task to a verdict, recording before every emit.
    async fn watch(self: Arc<Self>, run: Row) {
        let (rid, job, task) = (s(&run, "id").to_string(), s(&run, "job").to_string(), s(&run, "task_id").to_string());
        let started = f(&run, "started_at");
        let mut alerted = f(&run, "alerted_at") > 0.0;
        if f(&run, "polled_at") == 0.0 {
            (self.emit)(payload("submitted", &run, &format!("submitted {task}"), None));
        }
        let mut last_key: Option<Value> = None;
        let mut last_emit = 0.0;
        let mut failures = 0;
        loop {
            let (api, t) = (self.api.clone(), task.clone());
            let doc = match blocking(move || globus::task_status(api.as_ref(), &t)).await {
                Ok(d) => {
                    failures = 0;
                    d
                }
                Err(e) => {
                    failures += 1;
                    tracing::warn!("dvc: poll {failures}/{MAX_POLL_FAILURES} failed for task {task}: {e}");
                    if failures >= MAX_POLL_FAILURES {
                        let msg = format!("gave up polling task {task}: {e}");
                        let (dao, r, m) = (self.dao.clone(), rid.clone(), msg.clone());
                        if let Err(e) = blocking(move || dao.fail(&r, &m)).await {
                            tracing::error!("dvc: could not record {rid}: {e}");
                        }
                        (self.emit)(payload("error", &run, &msg, None));
                        return;
                    }
                    tokio::time::sleep(self.timing.poll).await;
                    continue;
                }
            };
            let (dao, r, d) = (self.dao.clone(), rid.clone(), doc.clone());
            let status = match blocking(move || jobs::record_status(&dao, &r, &d)).await {
                Ok(st) => st,
                Err(e) => {
                    tracing::warn!("dvc: could not record a poll of {task}: {e}");
                    tokio::time::sleep(self.timing.poll).await;
                    continue;
                }
            };
            if globus::TERMINAL.contains(&status.as_str()) {
                let ok = status == "SUCCEEDED";
                let skipped = self.note_skipped(&run, &doc).await;
                let line = format!(
                    "dvc: {job} task {task} {status} (files={}/{} bytes={} faults={}{}) {}",
                    doc["files_transferred"], doc["files"], doc["bytes_transferred"], doc["faults"],
                    if skipped.is_empty() { String::new() } else { format!(" SKIPPED={}", skipped.len()) },
                    doc["nice_status"].as_str().unwrap_or("")
                );
                if ok {
                    tracing::info!("{line}");
                } else {
                    tracing::error!("{line}");
                }
                let msg = match skipped.first() {
                    Some(first) => format!("{} source path(s) skipped: {}", skipped.len(), first.get("error_code").and_then(Value::as_str).unwrap_or("None")),
                    None => doc["nice_status"].as_str().unwrap_or("").to_string(),
                };
                (self.emit)(payload(if ok { "succeeded" } else { "failed" }, &run, &msg, Some(&doc)));
                return;
            }
            let t = now();
            if !alerted && started > 0.0 && t - started > STALL_AFTER_S {
                alerted = true;
                let (dao, r) = (self.dao.clone(), rid.clone());
                let _ = blocking(move || dao.mark_alerted(&r)).await;
                let hours = (t - started) / 3600.0;
                tracing::error!("dvc: {job} task {task} still running after {hours:.1}h — still polling");
                (self.emit)(payload("stalled", &run, &format!("still running after {hours:.1}h"), Some(&doc)));
            }
            let key = json!([doc["status"], doc["files_transferred"], doc["bytes_transferred"], doc["faults"]]);
            if last_key.as_ref() != Some(&key) || t - last_emit >= PROGRESS_EMIT_S {
                last_key = Some(key);
                last_emit = t;
                (self.emit)(payload("progress", &run, "", Some(&doc)));
            }
            tokio::time::sleep(self.timing.poll).await;
        }
    }
}

/// A run row with its epoch stamps spelled out, for a human reader.
pub fn run_view(row: Option<Row>) -> Value {
    let Some(row) = row else { return Value::Null };
    let mut out: Map<String, Value> = row.clone();
    for k in ["started_at", "submitted_at", "polled_at", "finished_at"] {
        out.insert(format!("{k}_iso"), json!(crate::iso_value(row.get(k))));
    }
    Value::Object(out)
}
