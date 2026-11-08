//! The two backup jobs, and the one path every submission takes. The service's verbs and
//! `dvc run-job` all come through [`run_job`], which claims the job's single-flight slot,
//! records the attempt and hands back a task id, so run history is complete.
//!
//! What a job does is code, here. When it runs is state in `dvc_jobs`, seeded from these
//! defaults on first insert only. The two jobs are paired: the mirror excludes the DVC
//! checkouts because the archive carries their bytes. Disabling `cache_sync` while
//! `workspace_backup` runs turns that exclusion into data loss; `coverage` shows it.

use std::collections::HashSet;
use std::sync::{Mutex, OnceLock};

use serde_json::{json, Value};

use crate::config::{self, ChinookConfig};
use crate::globus::{self, Api};
use crate::runs::{Row, RunsDao};
use crate::{backup, sync, Paths};

pub const CACHE_SYNC: &str = "cache_sync";
pub const WORKSPACE_BACKUP: &str = "workspace_backup";

pub struct JobSpec {
    pub name: &'static str,
    pub description: &'static str,
    pub label: &'static str,
    pub cron: &'static str,
    pub catchup_window_s: i64,
    /// Formatted with the in-flight task id.
    pub busy_note: &'static str,
    /// The routine that fires it.
    pub routine: &'static str,
}

pub const JOBS: [JobSpec; 2] = [
    JobSpec {
        name: CACHE_SYNC,
        description: "Append-only push of the shared DVC cache to chinook. Never deletes on the remote, so a local `dvc gc` stays recoverable.",
        label: sync::SYNC_LABEL,
        cron: "0 4 * * *",
        // Long enough to cover a machine asleep overnight; short enough that a week powered
        // off does not fire a stale backup on boot.
        catchup_window_s: 20 * 3600,
        busy_note: "sync task {task} is still running — a second full-cache scan would only duplicate its work. Pass force=true to submit anyway.",
        routine: "dvc-cache-sync",
    },
    JobSpec {
        name: WORKSPACE_BACKUP,
        description: "Mirror of the whole workspace to chinook, minus the DVC checkouts and the cache. DELETES on the remote: a local deletion propagates.",
        label: backup::BACKUP_LABEL,
        // Offset from the cache sync so the two scans do not overlap, and clear of the
        // 01:00-03:00 hours a DST shift repeats or skips.
        cron: "30 5 * * *",
        catchup_window_s: 20 * 3600,
        busy_note: "backup task {task} is still running — a second whole-tree scan would only duplicate its work. Pass force=true to submit anyway.",
        routine: "dvc-workspace-backup",
    },
];

pub fn names() -> Vec<&'static str> {
    let mut v: Vec<&str> = JOBS.iter().map(|j| j.name).collect();
    v.sort();
    v
}

pub fn spec_for(name: &str) -> Result<&'static JobSpec, String> {
    JOBS.iter().find(|j| j.name == name).ok_or_else(|| format!("unknown job '{name}' — known jobs: {}", names().join(", ")))
}

/// What a job submits. The live one builds the document and calls Globus; tests stub it.
pub trait Submitter: Send + Sync {
    fn submit(&self, job: &str, dry_run: bool) -> anyhow::Result<Value>;
}

pub struct Live<'a> {
    pub paths: &'a Paths,
    pub api: &'a dyn Api,
}

impl Submitter for Live<'_> {
    fn submit(&self, job: &str, dry_run: bool) -> anyhow::Result<Value> {
        let cfg: ChinookConfig = config::load(&self.paths.awm_dir)?;
        let api = (!dry_run).then_some(self.api);
        match job {
            CACHE_SYNC => sync::sync(self.paths, &cfg, api, dry_run),
            WORKSPACE_BACKUP => backup::backup(self.paths, &cfg, api, dry_run),
            other => anyhow::bail!("unknown job {other}"),
        }
    }
}

fn submitting_set() -> &'static Mutex<HashSet<String>> {
    static S: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    S.get_or_init(Default::default)
}

/// Run ids this process claimed whose submit has not returned. A whole-tree build takes
/// minutes, and the adopt sweep must not take it for a dead submitter.
pub fn submitting() -> HashSet<String> {
    submitting_set().lock().unwrap_or_else(|p| p.into_inner()).clone()
}

/// Turn a leftover `last_sync.json` (the cache sync's record before the run table) into a
/// live run row, once, then rename it aside.
fn adopt_legacy_state(paths: &Paths, dao: &RunsDao) {
    let state = paths.state_dir().join("last_sync.json");
    if !state.exists() {
        return;
    }
    let task = std::fs::read_to_string(&state)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.get("task_id").and_then(Value::as_str).map(str::to_string))
        .unwrap_or_default();
    if !task.is_empty() && matches!(dao.run_for_task(&task), Ok(None)) {
        match dao.adopt_task(CACHE_SYNC, &task, sync::SYNC_LABEL, "migrated from last_sync.json") {
            Ok(true) => tracing::info!("dvc: adopted in-flight task {task} from last_sync.json"),
            Ok(false) => {}
            Err(e) => tracing::warn!("dvc: could not adopt {task}: {e}"),
        }
    }
    if let Err(e) = std::fs::rename(&state, state.with_extension("json.migrated")) {
        tracing::warn!("dvc: could not rename {} aside: {e}", state.display());
    }
}

/// Create each job's `dvc_jobs` row, once.
pub fn ensure_seeded(paths: &Paths, dao: &RunsDao) -> crate::runs::Result<()> {
    for j in &JOBS {
        dao.seed_job(j.name, j.cron, j.catchup_window_s, true)?;
    }
    adopt_legacy_state(paths, dao);
    Ok(())
}

fn busy_note(spec: &JobSpec, live: Option<&Row>) -> String {
    let task = live.and_then(|r| r.get("task_id")).and_then(Value::as_str).unwrap_or("");
    if task.is_empty() {
        return format!("a previous {} run is still being submitted — refusing to start a second one. Pass force=true to supersede it.", spec.name);
    }
    spec.busy_note.replace("{task}", task)
}

fn merge(mut a: Value, extra: Value) -> Value {
    if let (Some(m), Value::Object(e)) = (a.as_object_mut(), extra) {
        m.extend(e);
    }
    a
}

#[derive(Debug, thiserror::Error)]
pub enum JobError {
    #[error("{0}")]
    Unknown(String),
    #[error("{1}")]
    Submit(&'static str, String),
    #[error(transparent)]
    Db(#[from] crate::runs::RunsError),
}

/// Submit `name`'s transfer, recording the attempt as a run. A declined run returns
/// `{submitted: false, in_flight, note}`. A dry run touches neither table.
pub fn run_job(paths: &Paths, dao: &RunsDao, sub: &dyn Submitter, name: &str, trigger: &str, force: bool, dry_run: bool) -> Result<Value, JobError> {
    let spec = spec_for(name).map_err(JobError::Unknown)?;
    if dry_run {
        let r = sub.submit(name, true).map_err(|e| JobError::Submit(class_of(&e), format!("{e:#}")))?;
        return Ok(merge(r, json!({"job": name})));
    }
    ensure_seeded(paths, dao)?;
    if force {
        // Retiring the old row does not cancel its Globus task: the forced submit really
        // duplicates it, which is what force means.
        let closed = dao.close_live(name, "superseded by a forced run")?;
        if closed > 0 {
            tracing::warn!("{name}: forced run superseded {closed} live row(s)");
        }
    }
    let Some(run) = dao.begin_run(name, trigger, "")? else {
        let live = dao.live_run(name)?;
        let note = busy_note(spec, live.as_ref());
        dao.record_skip(name, trigger, &note)?;
        tracing::info!("{name}: declined ({note})");
        let in_flight = dao.live_run(name)?.and_then(|r| r.get("task_id").and_then(Value::as_str).map(str::to_string)).unwrap_or_default();
        return Ok(json!({"submitted": false, "job": name, "in_flight": in_flight, "note": note}));
    };
    let rid = run["id"].as_str().unwrap_or("").to_string();
    submitting_set().lock().unwrap_or_else(|p| p.into_inner()).insert(rid.clone());
    let out = (|| {
        let result = match sub.submit(name, false) {
            Ok(r) => r,
            Err(e) => {
                let msg = format!("{e:#}");
                let class = class_of(&e);
                dao.fail(&rid, &format!("{class}: {msg}"))?;
                tracing::error!("{name}: submit failed: {msg}");
                return Err(JobError::Submit(class, msg));
            }
        };
        let task = result.get("task_id").and_then(Value::as_str).unwrap_or("").to_string();
        if task.is_empty() {
            dao.fail(&rid, "submit returned no task id")?;
            return Ok(merge(result, json!({"job": name, "run_id": rid})));
        }
        dao.mark_submitted(&rid, &task, spec.label)?;
        tracing::info!("{name}: submitted task {task} (trigger={trigger})");
        Ok(merge(result, json!({"job": name, "run_id": rid, "trigger": trigger})))
    })();
    submitting_set().lock().unwrap_or_else(|p| p.into_inner()).remove(&rid);
    out
}

/// Fold a Globus status document into a run row and return the row's status. A terminal
/// document closes the row; anything else is progress and leaves it live, for the watcher.
pub fn record_status(dao: &RunsDao, run_id: &str, doc: &Value) -> crate::runs::Result<String> {
    let status = doc.get("status").and_then(Value::as_str).unwrap_or("");
    if doc.get("done").and_then(Value::as_bool).unwrap_or(false) && globus::TERMINAL.contains(&status) {
        dao.finish(run_id, status, doc)?;
        return Ok(status.to_string());
    }
    dao.record_poll(run_id, doc)?;
    Ok("running".into())
}

/// The Python exception name a submit failure carried, for the run row and the verb error.
pub fn class_of(e: &anyhow::Error) -> &'static str {
    if e.downcast_ref::<config::DvcConfigError>().is_some() {
        "DvcConfigError"
    } else if e.downcast_ref::<globus::GlobusError>().is_some() {
        "GlobusError"
    } else if e.to_string().starts_with("shared cache ") {
        "FileNotFoundError"
    } else {
        "RuntimeError"
    }
}
