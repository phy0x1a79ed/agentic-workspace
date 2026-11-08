//! The `routine` tool: its verbs, their schemas and their handlers.

use std::path::PathBuf;
use std::sync::Arc;

use awm_plugin_sdk::{Call, Service, VerbError};
use serde_json::{json, Value};

use crate::events::Events;
use crate::runner::{public, RoutineError, Runner};
use crate::timers::{self, Systemctl, Timers};

fn p(name: &str, ty: &str, required: bool, description: &str) -> Value {
    json!({"name": name, "type": ty, "required": required, "description": description})
}

/// Each verb: its name, description and params, as the Python manifest published them.
pub fn functions() -> Vec<(&'static str, &'static str, Value)> {
    let project = || p("project", "string", true, "The project the routine belongs to.");
    let name = || p("name", "string", true, "The routine's name.");
    vec![
        (
            "install",
            "Install a routine on this node from its definition file in a checkout of its project, pinned at the checkout's commit. Reinstall after editing the definition.",
            json!([project(), p("path", "string", true, "The definition file, such as <worktree>/routines/<name>.toml.")]),
        ),
        ("uninstall", "Remove an installed routine and its timer.", json!([project(), name()])),
        ("list", "The routines installed on this node, with their timers, events and last run.", json!([p("project", "string", false, "Only this project.")])),
        (
            "trigger",
            "Run a routine now and return its run record at once. Refused while the routine has a live run. An event starts a routine too, but only the service raises one. A failed run starts a fixing agent on the routine's declared peer.",
            json!([project(), name(), p("wait", "boolean", false, "Return only when the run ends."), p("trigger", "string", false, "`call` (default) or `timer`. `event` is the service's alone.")]),
        ),
        ("cancel", "Stop a routine's live run and every process it started.", json!([project(), name()])),
        (
            "schedule",
            "Set a routine's timer: a systemd user timer that calls `trigger`. Uses the definition's timer unless `timer` is given.",
            json!([
                project(),
                name(),
                p("timer", "string", false, "A systemd OnCalendar expression."),
                p("jitter_s", "integer", false, "The timer's random delay (RandomizedDelaySec), in seconds. Default 30."),
                p("catchup_s", "integer", false, "How late a fire may come after its slot, such as after the node was down, and still run. A later fire, or a second fire for one slot, records a `skipped` run. Unset, every fire runs."),
                p("last_slot", "string", false, "An ISO-8601 time: the last slot another clock already served, such as for a job moving onto this timer. A slot at or before it counts as run. It never moves the slot back."),
            ]),
        ),
        ("unschedule", "Clear a routine's timer.", json!([project(), name()])),
        (
            "runs",
            "Recent runs, newest first, or one run by id.",
            json!([
                p("project", "string", false, "Only this project."),
                p("name", "string", false, "Only this routine."),
                p("id", "string", false, "One run."),
                p("limit", "integer", false, "How many. Default 20.")
            ]),
        ),
    ]
}

/// Who started a run: the call's `as_`, else its session, else its pid.
pub fn caller_label(as_: Option<&str>, session: Option<&str>, pid: Option<u32>) -> String {
    match (as_.filter(|a| !a.is_empty()), session.filter(|s| !s.is_empty()), pid) {
        (Some(a), ..) => a.to_string(),
        (None, Some(s), _) => format!("session {s}"),
        (None, None, Some(pid)) => format!("pid {pid}"),
        _ => String::new(),
    }
}

/// The handlers, apart from the gateway so tests drive them directly.
pub struct RoutineService {
    pub runner: Arc<Runner>,
    pub unit_dir: PathBuf,
    pub systemctl: Box<Systemctl>,
    /// Absent where no hub exists, such as in tests.
    pub events: Option<Arc<Events>>,
}

fn err(e: RoutineError) -> VerbError {
    VerbError::new(e.class(), e.to_string())
}

fn str_arg<'a>(args: &'a Value, key: &str) -> Option<&'a str> {
    args.get(key).and_then(Value::as_str).filter(|s| !s.is_empty())
}

fn req<'a>(args: &'a Value, key: &str) -> Result<&'a str, VerbError> {
    args.get(key).and_then(Value::as_str).ok_or_else(|| VerbError::value(format!("missing required argument: {key}")))
}

fn int_arg(args: &Value, key: &str) -> Result<Option<i64>, VerbError> {
    match args.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.trim().is_empty() => Ok(None),
        Some(Value::String(s)) => s.trim().parse().map(Some).map_err(|_| VerbError::value(format!("{key} must be an integer"))),
        Some(v) => v.as_i64().or_else(|| v.as_f64().filter(|f| f.fract() == 0.0).map(|f| f as i64)).map(Some).ok_or_else(|| VerbError::value(format!("{key} must be an integer"))),
    }
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty() && !matches!(s.to_lowercase().as_str(), "false" | "0" | "no"),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(_) => true,
    }
}

impl RoutineService {
    fn timers(&self) -> Timers<'_> {
        Timers { dir: &self.unit_dir, systemctl: self.systemctl.as_ref() }
    }

    fn reconcile_events(&self) {
        if let Some(e) = &self.events {
            e.reconcile();
        }
    }

    pub fn install(&self, args: &Value) -> Result<Value, VerbError> {
        let row = self.runner.install(req(args, "project")?, req(args, "path")?).map(Value::Object).map_err(err)?;
        self.reconcile_events();
        Ok(row)
    }

    pub fn uninstall(&self, args: &Value) -> Result<Value, VerbError> {
        let (project, name) = (req(args, "project")?, req(args, "name")?);
        let rec = self.runner.store.get_routine(project, name).map_err(|e| err(e.into()))?;
        if rec.is_some_and(|r| r.get("scheduled").and_then(Value::as_i64).unwrap_or(0) != 0) {
            self.unschedule(args)?;
        }
        let removed = self.runner.uninstall(project, name).map_err(err)?;
        self.reconcile_events();
        Ok(json!({"removed": removed}))
    }

    pub fn list(&self, args: &Value) -> Result<Value, VerbError> {
        let store = &self.runner.store;
        let mut out = vec![];
        for rec in store.list_routines(str_arg(args, "project")).map_err(|e| err(e.into()))? {
            let rec = public(rec);
            let d = rec.get("definition").cloned().unwrap_or(json!({}));
            let (project, name) = (rec["project"].as_str().unwrap_or(""), rec["name"].as_str().unwrap_or(""));
            let last = store.list_runs(Some(project), Some(name), 1).map_err(|e| err(e.into()))?;
            let last_run = last.first().map(|r| json!({"id": r["id"], "state": r["state"], "started_at": r["started_at"], "failed_step": r["failed_step"]}));
            let commit: String = rec["commit_sha"].as_str().unwrap_or("").chars().take(12).collect();
            out.push(json!({
                "project": project, "name": name,
                "description": d.get("description").cloned().unwrap_or(json!("")),
                "timer": d.get("timer").cloned().unwrap_or(json!("")),
                "events": d.get("events").cloned().unwrap_or(json!([])),
                "scheduled": rec["scheduled"], "commit": commit, "path": rec["path"], "last_run": last_run,
            }));
        }
        Ok(json!({"routines": out}))
    }

    pub async fn trigger(&self, args: &Value, caller: String) -> Result<Value, VerbError> {
        let trig = str_arg(args, "trigger").unwrap_or("call");
        if trig != "call" && trig != "timer" {
            return Err(VerbError::value("trigger must be `call` or `timer`; an event trigger comes only from a subscription"));
        }
        let run = self.runner.trigger(req(args, "project")?, req(args, "name")?, trig, &caller, truthy(args.get("wait"))).await.map_err(err)?;
        Ok(Value::Object(run))
    }

    pub async fn cancel(&self, args: &Value) -> Result<Value, VerbError> {
        self.runner.cancel(req(args, "project")?, req(args, "name")?).await.map_err(err)
    }

    pub fn schedule(&self, args: &Value) -> Result<Value, VerbError> {
        let (project, name) = (req(args, "project")?, req(args, "name")?);
        let (_, routine) = self.runner.routine(project, name).map_err(err)?;
        let spec = str_arg(args, "timer").map(str::to_string).unwrap_or(routine.timer);
        if spec.is_empty() {
            return Err(VerbError::value("the routine defines no timer. Pass `timer`"));
        }
        let jitter = int_arg(args, "jitter_s")?.unwrap_or(timers::DEFAULT_JITTER_S);
        let catchup = int_arg(args, "catchup_s")?;
        let seed = match str_arg(args, "last_slot") {
            None => None,
            Some(s) => Some(chrono::DateTime::parse_from_rfc3339(s.trim()).map_err(|e| VerbError::value(format!("last_slot must be an ISO-8601 time with an offset: {e}")))?.with_timezone(&chrono::Utc)),
        };
        if jitter < 0 {
            return Err(VerbError::value("jitter_s must not be negative"));
        }
        if catchup.is_some_and(|c| c < jitter.max(1)) {
            return Err(VerbError::value("catchup_s must be at least 1 and at least jitter_s, or every jittered fire is late"));
        }
        let res = self.timers().schedule(project, name, &spec, jitter, catchup).map_err(|e| VerbError::value(format!("{e:#}")))?;
        let store = &self.runner.store;
        store.put_timer(project, name, &spec, jitter, catchup).map_err(|e| err(e.into()))?;
        if let Some(seed) = seed {
            store.raise_last_slot(project, name, &crate::iso(seed)).map_err(|e| err(e.into()))?;
        }
        store.set_scheduled(project, name, true).map_err(|e| err(e.into()))?;
        Ok(res)
    }

    pub fn unschedule(&self, args: &Value) -> Result<Value, VerbError> {
        let (project, name) = (req(args, "project")?, req(args, "name")?);
        let res = self.timers().unschedule(project, name).map_err(VerbError::from_anyhow)?;
        self.runner.store.set_scheduled(project, name, false).map_err(|e| err(e.into()))?;
        Ok(res)
    }

    pub fn runs(&self, args: &Value) -> Result<Value, VerbError> {
        let store = &self.runner.store;
        if let Some(id) = str_arg(args, "id") {
            return store.get_run(id).map_err(|e| err(e.into()))?.map(Value::Object).ok_or_else(|| VerbError::new("KeyError", format!("no run {id}")));
        }
        let limit = int_arg(args, "limit")?.filter(|l| *l != 0).unwrap_or(20);
        Ok(json!({"runs": store.list_runs(str_arg(args, "project"), str_arg(args, "name"), limit).map_err(|e| err(e.into()))?}))
    }

    pub async fn handle(&self, verb: &str, args: &Value, caller: String) -> Result<Value, VerbError> {
        match verb {
            "install" => self.install(args),
            "uninstall" => self.uninstall(args),
            "list" => self.list(args),
            "trigger" => self.trigger(args, caller).await,
            "cancel" => self.cancel(args).await,
            "schedule" => self.schedule(args),
            "unschedule" => self.unschedule(args),
            "runs" => self.runs(args),
            other => Err(VerbError::value(format!("unknown verb {other}"))),
        }
    }
}

/// Add every verb of `svc` to `service`.
pub fn mount(mut service: Service, svc: Arc<RoutineService>) -> Service {
    for (name, description, params) in functions() {
        let svc = svc.clone();
        service = service.verb("routine", name, description, params, move |call: Call| {
            let svc = svc.clone();
            async move {
                let caller = caller_label(call.as_.as_deref(), call.caller_session_id(), call.caller_pid());
                svc.handle(name, &call.declared_args(), caller).await
            }
        });
    }
    service
}

pub fn default_unit_dir() -> PathBuf {
    timers::unit_dir()
}
