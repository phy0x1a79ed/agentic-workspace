//! The `dvc` tool's verbs. Their names, params and surfaces are Python's. Write verbs stay
//! off the MCP surface: an agent reads backup health, a human changes when backups run.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::{privacy, Call, Hub, Service, VerbError};
use serde_json::{json, Value};

use crate::globus::{self, Api};
use crate::jobs::{self, JobError, Live, JOBS};
use crate::runs::RunsDao;
use crate::scopedata::{self, not_found};
use crate::transfer::{self, globus_err, scope_err};
use crate::watcher::{run_view, Watcher};
use crate::{clock, coverage, cron, iso, iso_value, now, Paths};

pub const TOOL: &str = "dvc";
const CLI_HTTP: [&str; 2] = ["cli", "http"];

fn p(name: &str, ty: &str, required: bool, description: Option<&str>) -> Value {
    let mut v = json!({"name": name, "type": ty, "required": required});
    if let Some(d) = description {
        v["description"] = json!(d);
    }
    v
}

fn scope_param() -> Value {
    p("scope", "string", true, Some("Scope worktree — absolute, or relative to the workspace root (e.g. 'projects/fabfos/dev')."))
}

fn dry_run_param() -> Value {
    p("dry_run", "boolean", false, Some("Return the transfer items that would be submitted, and submit nothing."))
}

pub struct VerbSpec {
    pub name: &'static str,
    pub description: &'static str,
    pub params: Value,
    pub timeout: f64,
    pub cli_http_only: bool,
}

/// Every verb, as the Python manifest published it, apart from the clock's descriptions.
pub fn verbs() -> Vec<VerbSpec> {
    let project = || p("project", "string", true, None);
    let scope_name = || p("scope", "string", true, None);
    let v = |name, description, params, timeout| VerbSpec { name, description, params, timeout, cli_http_only: false };
    let mut out = vec![
        v("status", "Report a scope's DVC pin state against the local cache: pin count, .dir manifests, and how many objects are present vs missing. Local only — touches no network. unresolved_manifests>0 means some .dir manifests are absent, so their leaves are not yet counted.", json!([scope_param()]), 300.0),
        v("resolve", "List the exact object hashes a scope depends on, split into present / missing / unresolved_manifests. Local only. Use to see precisely what a pull would move.", json!([scope_param()]), 300.0),
        v("pull", "Fetch from chinook only the cache objects this scope pins and does not have. Submits a Globus task and returns its task_id; poll with task. A cold restore is two-phase — .dir manifests must land before their leaves can be named — so a call whose phase is 'manifests' should be repeated once that task completes to fetch the leaves. Safe to repeat: each call re-resolves and does whatever remains. After the final task, run `dvc checkout` in the scope to materialize.", json!([scope_param(), dry_run_param()]), 600.0),
        v("push", "Send the cache objects this scope pins up to chinook. Submits a Globus task and returns its task_id; poll with task. Never deletes on the remote. Fails if any .dir manifest is missing locally — you cannot push what you do not have.", json!([scope_param(), dry_run_param()]), 600.0),
        v("sync", "Submit the daily sync of the shared DVC cache (data/.dvc_cache) to chinook and return its task_id. APPEND-ONLY: never deletes on the remote, so chinook accumulates objects and a local `dvc gc` stays recoverable. Covers every project's data at once, because every project shares that one cache. Declines if a previous sync is still running unless force=true. Nothing else in the workspace is backed up by this — see coverage.", json!([
            p("dry_run", "boolean", false, Some("Build and return the transfer document; submit nothing.")),
            p("force", "boolean", false, Some("Submit even if a previous sync task is still running.")),
        ]), 300.0),
        v("coverage", "Report what would be lost if this machine died: per scope worktree, uncommitted and untracked files, branches with no upstream or ahead of one, and pins whose objects are absent from the shared cache. Read-only, no network. Code is covered by GitHub and data by the cache sync; everything else is disposable by decision, and this is how that decision is checked.", json!([
            p("all", "boolean", false, Some("Include scopes with nothing at risk (default: only at-risk ones).")),
        ]), 900.0),
        v("task", "Status of a Globus task submitted by this service: state, files transferred, bytes, throughput, faults. Pass wait=<seconds> to block until it finishes or that budget runs out (timed_out=true means still running, not failed). nice_status names the current fault, if any; an ACTIVE task with faults is still retrying.", json!([
            p("task_id", "string", true, None),
            p("wait", "integer", false, Some("Seconds to block waiting for a terminal state (default: return immediately).")),
        ]), 3600.0),
        v("jobs", "The two scheduled backups — cache_sync (append-only archive of the DVC cache) and workspace_backup (mirror of everything else, which DELETES on the remote) — with their cron, enabled state, next due time, and last outcome. routine's timers fire them: registered=false means routine holds no timer for an enabled job, which is how a backup silently stops happening. Also reports the watcher that follows each run to its verdict. Read-only.", json!([]), 30.0),
        v("runs", "Backup run history — one row per attempt, from every entry point (routine's timer, manual verb, dvc run-job). Carries task_id, trigger, status, file and byte counters, faults, and error text. Status is submitting/running, then SUCCEEDED or FAILED verbatim from Globus, or skipped (declined as already in flight) or error (never reached Globus). Read-only.", json!([
            p("job", "string", false, Some("Filter to one job (cache_sync, workspace_backup).")),
            p("limit", "integer", false, Some("Rows to return, newest first (default 20).")),
            p("status", "string", false, None),
            p("since", "string", false, Some("Only runs started at or after this ISO-8601 time or epoch second.")),
        ]), 60.0),
        VerbSpec { cli_http_only: true, ..v("run", "Trigger a backup job now, out of schedule. Submits and returns a task_id; the service watches it to a verdict recorded in runs. Declines if that job is already in flight — a second whole-tree scan only duplicates the first — unless force=true, which does not cancel the running task.", json!([
            p("job", "string", true, Some("cache_sync or workspace_backup.")),
            p("force", "boolean", false, None),
            dry_run_param(),
        ]), 300.0) },
        VerbSpec { cli_http_only: true, ..v("schedule", "Change when a backup job runs, or disable it. Accepts a 5-field cron, in local time; routine's timer takes it at once, with no restart. Disabling cache_sync while workspace_backup stays enabled is data loss, not a saving: the mirror excludes the DVC checkouts precisely because the archive holds them.", json!([
            p("job", "string", true, None),
            p("cron", "string", false, None),
            p("enabled", "boolean", false, None),
        ]), 30.0) },
        v("scope_status", "Report a scope's data view: mode (dvc, or none for a project with no DVC), the commit that pins the data, which chunks it pins, which of those are materialised on disk, and whether the workspace still matches the pins. There is no separate data branch to drift — the code revision IS the data revision.", json!([project(), scope_name()]), 300.0),
        v("mount", "Choose which data chunks this scope materialises on disk, e.g. ['data/pipeline/model']. Every chunk the branch pins stays pinned, hashed and backed up regardless — this only decides what costs inodes and checkout time here, so a scope can pin a 30 GB archive it never reads. Pass no chunks to materialise everything. To COMMIT data, use ordinary git: `dvc add <path>` then commit the generated .dvc pin alongside your code.", json!([project(), scope_name(), p("chunks", "array", false, None)]), 900.0),
        v("gc", "Reclaim shared-cache space by deleting objects no listed project references. DRY RUN BY DEFAULT — pass dry_run=false to delete. The cache is shared workspace-wide: name every DVC-wired project either in projects (its data survives) or in exclude (its data is dropped on purpose), or the call is refused. Keeps every output pinned by any commit, staged index or worktree file, plus any object touched within the grace window. Refuses while a pinned .dir manifest is missing from the cache, unless its id is in accept_missing. Takes no repo locks; only one gc runs at a time.", json!([
            p("projects", "array", true, None),
            p("exclude", "array", false, None),
            p("accept_missing", "array", false, None),
            p("dry_run", "boolean", false, None),
            p("keep", "string", false, None),
        ]), 600.0),
    ];
    out.shrink_to_fit();
    out
}

pub const EMITTER: &str = "Fires on each backup run transition. Payload includes {run_id, job, trigger, status, task_id, timestamp, elapsed_s, message}, plus files / files_transferred / bytes_transferred / faults once a Globus task exists. Statuses: submitted -> progress* -> succeeded | failed, or error | stalled (stalled is an alert, not a verdict — the run keeps going). Best-effort: events sent while nothing is subscribed are lost, so read runs first and then tail this.";

/// The handlers, apart from the gateway so tests drive them directly.
pub struct Dvc {
    pub paths: Paths,
    pub dao: Arc<RunsDao>,
    pub api: Arc<dyn Api>,
    pub watcher: Option<Arc<Watcher>>,
    pub hub: Option<Hub>,
    pub plugin_dir: PathBuf,
    pub hook_bin: PathBuf,
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty() && !matches!(s.to_lowercase().as_str(), "false" | "0" | "no"),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn req_str<'a>(a: &'a Value, k: &str) -> Result<&'a str, VerbError> {
    a.get(k).and_then(Value::as_str).ok_or_else(|| VerbError::value(format!("missing required argument: {k}")))
}

fn opt_str<'a>(a: &'a Value, k: &str) -> Option<&'a str> {
    a.get(k).and_then(Value::as_str).filter(|s| !s.is_empty())
}

fn int_arg(a: &Value, k: &str) -> Result<Option<i64>, VerbError> {
    match a.get(k) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.trim().is_empty() => Ok(None),
        Some(Value::String(s)) => s.trim().parse().map(Some).map_err(|_| VerbError::value(format!("{k} must be an integer"))),
        Some(v) => v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)).map(Some).ok_or_else(|| VerbError::value(format!("{k} must be an integer"))),
    }
}

fn str_list(a: &Value, k: &str) -> Result<Vec<String>, VerbError> {
    match a.get(k) {
        None | Some(Value::Null) => Ok(vec![]),
        Some(Value::Array(v)) => v.iter().map(|x| x.as_str().map(str::to_string).ok_or_else(|| VerbError::value(format!("{k} must be a list of strings")))).collect(),
        Some(_) => Err(VerbError::value(format!("{k} must be a list"))),
    }
}

fn job_err(e: JobError) -> VerbError {
    match e {
        JobError::Unknown(m) => VerbError::value(m),
        JobError::Submit(class, m) => VerbError::new(class, m),
        JobError::Db(e) => VerbError::runtime(e.to_string()),
    }
}

fn db_err(e: crate::runs::RunsError) -> VerbError {
    VerbError::runtime(e.to_string())
}

async fn blocking<T: Send + 'static>(work: impl FnOnce() -> T + Send + 'static) -> T {
    match tokio::task::spawn_blocking(work).await {
        Ok(v) => v,
        Err(e) => std::panic::resume_unwind(e.into_panic()),
    }
}

impl Dvc {
    /// The `scope` path, which a peer caller meets as no scope when a mark covers it.
    fn scope_path(&self, a: &Value, peer: bool) -> Result<String, VerbError> {
        let s = req_str(a, "scope")?;
        if peer && privacy::hidden_path(s) {
            return Err(scope_err(format!("not a DVC scope (no .dvc/): {s}")));
        }
        Ok(s.to_string())
    }

    /// `(project, scope)`, which a peer caller meets as no worktree when a mark covers it.
    fn project_scope(&self, a: &Value, peer: bool) -> Result<(String, String), VerbError> {
        let (project, scope) = (req_str(a, "project")?, req_str(a, "scope")?);
        if peer && privacy::hidden(project, Some(scope)) {
            return Err(not_found(format!("No worktree directory for scope '{project}/{scope}' at {}", self.paths.projects().join(project).join(scope).display())));
        }
        Ok((project.into(), scope.into()))
    }

    fn sweep_soon(&self) {
        if let Some(w) = self.watcher.clone() {
            tokio::spawn(async move {
                if let Err(e) = w.sweep().await {
                    tracing::warn!("dvc: adopt sweep failed: {e:#}");
                }
            });
        }
    }

    async fn run_job(self: &Arc<Self>, job: String, force: bool, dry_run: bool) -> Result<Value, VerbError> {
        let me = self.clone();
        let out = blocking(move || {
            let live = Live { paths: &me.paths, api: me.api.as_ref() };
            jobs::run_job(&me.paths, &me.dao, &live, &job, "manual", force, dry_run)
        })
        .await
        .map_err(job_err)?;
        if !dry_run {
            self.sweep_soon();
        }
        Ok(out)
    }

    pub async fn jobs(self: &Arc<Self>) -> Result<Value, VerbError> {
        let me = self.clone();
        let rows = blocking(move || -> crate::runs::Result<_> {
            jobs::ensure_seeded(&me.paths, &me.dao)?;
            let rows = me.dao.list_jobs()?;
            let last: Vec<_> = rows.iter().map(|r| me.dao.last_run(r["name"].as_str().unwrap_or(""))).collect::<Result<_, _>>()?;
            Ok((rows, last))
        })
        .await
        .map_err(db_err)?;
        let reg = match &self.hub {
            Some(h) => clock::registered(h).await,
            None => Err("no gateway".to_string()),
        };
        let t = now();
        let mut out = vec![];
        for (row, last) in rows.0.into_iter().zip(rows.1) {
            let name = row["name"].as_str().unwrap_or("").to_string();
            let spec = JOBS.iter().find(|j| j.name == name);
            let enabled = row.get("enabled").and_then(Value::as_i64).unwrap_or(0) != 0;
            let cron_s = row.get("cron").and_then(Value::as_str).unwrap_or("");
            let due = if enabled { cron::parse(cron_s).and_then(|c| c.next_due(t)).ok() } else { None };
            let routine = spec.map(|s| s.routine).unwrap_or("");
            let registered = reg.as_ref().ok().and_then(|m| m.get(routine).copied()).unwrap_or(false);
            out.push(json!({
                "name": name,
                "description": spec.map(|s| s.description).unwrap_or(""),
                "cron": row.get("cron").cloned().unwrap_or(Value::Null),
                "enabled": enabled,
                "catchup_window_s": row.get("catchup_window_s").cloned().unwrap_or(Value::Null),
                "last_fire_at_iso": iso_value(row.get("last_fire_at")),
                "next_due": due,
                "next_due_iso": due.map(iso).unwrap_or_default(),
                "routine": routine,
                "registered": registered,
                "last_run": run_view(last),
            }));
        }
        let mut clock = json!({"owner": "routine", "project": clock::PROJECT, "reachable": reg.is_ok()});
        if let Err(e) = &reg {
            clock["error"] = json!(e);
        }
        if let Err(e) = crate::config::load(&self.paths.awm_dir) {
            clock["unconfigured"] = json!(e.0);
        }
        let watcher = self.watcher.as_ref().map(|w| w.health()).unwrap_or(json!({"watching": 0, "sweeps": 0, "last_sweep_age_s": null, "stopped": true}));
        Ok(json!({"clock": "routine", "routine": clock, "watcher": watcher, "jobs": out}))
    }

    pub async fn schedule(self: &Arc<Self>, a: &Value) -> Result<Value, VerbError> {
        let name = req_str(a, "job")?.trim().to_string();
        let spec = jobs::spec_for(&name).map_err(VerbError::value)?;
        let cron_arg = a.get("cron").filter(|v| !v.is_null()).map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string()));
        if let Some(c) = &cron_arg {
            // Checked here, not at fire time: a bad cron accepted now is a job that silently
            // stops being scheduled.
            cron::to_on_calendar(c).map_err(|e| VerbError::new("CronError", e.0))?;
        }
        let enabled = a.get("enabled").filter(|v| !v.is_null()).map(|v| truthy(Some(v)));
        let me = self.clone();
        let n = name.clone();
        let row = blocking(move || -> crate::runs::Result<_> {
            jobs::ensure_seeded(&me.paths, &me.dao)?;
            me.dao.set_schedule(&n, cron_arg.as_deref(), enabled)
        })
        .await
        .map_err(db_err)?;
        let clock = match (&self.hub, &row) {
            (Some(h), Some(r)) => {
                let unconfigured = crate::config::load(&self.paths.awm_dir).err().map(|e| e.0);
                clock::sync_one(h, &self.plugin_dir, spec, r, unconfigured.as_deref()).await
            }
            _ => json!({"registered": false, "error": "no gateway"}),
        };
        Ok(json!({"job": row, "clock": clock}))
    }

    pub async fn runs(self: &Arc<Self>, a: &Value) -> Result<Value, VerbError> {
        let job = opt_str(a, "job").map(str::to_string);
        let status = opt_str(a, "status").map(str::to_string);
        let limit = int_arg(a, "limit")?.filter(|l| *l != 0).unwrap_or(20);
        let since = crate::to_epoch(a.get("since"));
        let me = self.clone();
        let rows = blocking(move || me.dao.history(job.as_deref(), limit, status.as_deref(), since)).await.map_err(db_err)?;
        let rows: Vec<Value> = rows.into_iter().map(|r| run_view(Some(r))).collect();
        Ok(json!({"count": rows.len(), "runs": rows}))
    }

    pub async fn task(self: &Arc<Self>, a: &Value) -> Result<Value, VerbError> {
        let id = req_str(a, "task_id")?.to_string();
        let wait = int_arg(a, "wait")?.unwrap_or(0);
        let api = self.api.clone();
        blocking(move || if wait > 0 { globus::wait(api.as_ref(), &id, wait as u64, 15) } else { globus::task_status(api.as_ref(), &id) }).await.map_err(globus_err)
    }

    pub async fn gc(self: &Arc<Self>, a: &Value, peer: bool) -> Result<Value, VerbError> {
        let projects = match a.get("projects") {
            None | Some(Value::Null) => return Err(VerbError::value("missing required argument: projects")),
            _ => str_list(a, "projects")?,
        };
        let exclude = str_list(a, "exclude")?;
        if peer {
            for n in projects.iter().chain(exclude.iter()) {
                if privacy::hidden(n, None) {
                    return Err(not_found(format!("Project '{n}' has no bare repo at {}", self.paths.projects().join(n).join(".bare").display())));
                }
            }
        }
        let me = self.clone();
        let args = scopedata::GcArgs {
            projects,
            dry_run: a.get("dry_run").filter(|v| !v.is_null()).map(|v| truthy(Some(v))).unwrap_or(true),
            keep: opt_str(a, "keep").unwrap_or("all-commits").to_string(),
            exclude,
            accept_missing: str_list(a, "accept_missing")?,
        };
        blocking(move || {
            if peer && scopedata::dvc_wired_projects(&me.paths).iter().any(|n| privacy::hidden(n, None)) {
                return Ok(json!({"result": "refused", "detail": "gc runs only from this node while it has private projects"}));
            }
            scopedata::gc(&me.paths, args)
        })
        .await
    }

    pub async fn handle(self: &Arc<Self>, verb: &str, a: Value, peer: bool) -> Result<Value, VerbError> {
        let me = self.clone();
        match verb {
            "status" => {
                let s = self.scope_path(&a, peer)?;
                blocking(move || transfer::status(&me.paths, &s)).await
            }
            "resolve" => {
                let s = self.scope_path(&a, peer)?;
                blocking(move || transfer::resolve(&me.paths, &s)).await
            }
            "pull" | "push" => {
                let s = self.scope_path(&a, peer)?;
                let dry = truthy(a.get("dry_run"));
                let pull = verb == "pull";
                blocking(move || if pull { transfer::pull(&me.paths, me.api.as_ref(), &s, dry) } else { transfer::push(&me.paths, me.api.as_ref(), &s, dry) }).await
            }
            "sync" => self.run_job(jobs::CACHE_SYNC.into(), truthy(a.get("force")), truthy(a.get("dry_run"))).await,
            "run" => self.run_job(req_str(&a, "job")?.trim().to_string(), truthy(a.get("force")), truthy(a.get("dry_run"))).await,
            "coverage" => {
                let all = truthy(a.get("all"));
                blocking(move || Ok(coverage::report(&me.paths, !all, peer))).await
            }
            "task" => self.task(&a).await,
            "jobs" => self.jobs().await,
            "runs" => self.runs(&a).await,
            "schedule" => self.schedule(&a).await,
            "scope_status" => {
                let (p, s) = self.project_scope(&a, peer)?;
                blocking(move || scopedata::scope_status(&me.paths, &p, &s)).await
            }
            "mount" => {
                let (p, s) = self.project_scope(&a, peer)?;
                let chunks = match a.get("chunks") {
                    None | Some(Value::Null) => None,
                    _ => Some(str_list(&a, "chunks")?),
                };
                blocking(move || scopedata::mount(&me.paths, &me.hook_bin, &p, &s, chunks)).await
            }
            "gc" => self.gc(&a, peer).await,
            other => Err(VerbError::value(format!("unknown verb {other}"))),
        }
    }
}

/// Add every verb and the emitter to `service`.
pub fn mount(mut service: Service, dvc: Arc<Dvc>) -> Service {
    for v in verbs() {
        let d = dvc.clone();
        let name = v.name;
        service = service
            .verb(TOOL, name, v.description, v.params, move |call: Call| {
                let d = d.clone();
                async move {
                    let peer = call.from_peer();
                    d.handle(name, call.declared_args(), peer).await
                }
            })
            .timeout(Duration::from_secs_f64(v.timeout));
        if v.cli_http_only {
            service = service.surfaces(CLI_HTTP);
        }
    }
    service.emitter(crate::watcher::TOPIC, Some(EMITTER))
}
