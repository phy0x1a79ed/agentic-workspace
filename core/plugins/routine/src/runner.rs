//! Install, trigger, cancel and watch routine runs (routines.md R34 to R38, R47, R48).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use serde_json::{json, Map, Value};
use tokio::sync::watch;

use crate::definitions::{self, Routine, Step};
use crate::escalate::{escalate, notice};
use crate::outbound::{Outbound, Start};
use crate::proc::{self, Halt};
use crate::store::{Store, StoreError};
use crate::{fmt_g, timers};

/// A failure of a routine verb, by the class Python raised.
#[derive(Debug, thiserror::Error)]
pub enum RoutineError {
    #[error("{0}")]
    Value(String),
    #[error("{0}")]
    NotFound(String),
    #[error("{0}")]
    FileNotFound(String),
    #[error("{0}")]
    LiveRunExists(String),
    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl RoutineError {
    pub fn class(&self) -> &'static str {
        match self {
            RoutineError::Value(_) => "ValueError",
            RoutineError::NotFound(_) => "KeyError",
            RoutineError::FileNotFound(_) => "FileNotFoundError",
            RoutineError::LiveRunExists(_) => "LiveRunExists",
            RoutineError::Other(_) => "RuntimeError",
        }
    }
}

impl From<rusqlite::Error> for RoutineError {
    fn from(e: rusqlite::Error) -> Self {
        RoutineError::Other(e.into())
    }
}

impl From<StoreError> for RoutineError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::LiveRunExists(m) => RoutineError::LiveRunExists(m),
            StoreError::Db(e) => e.into(),
        }
    }
}

pub type Result<T> = std::result::Result<T, RoutineError>;
pub type Row = Map<String, Value>;
/// Counts one finished run: its `<project>/<name>`, whether it passed, and its milliseconds.
pub type UsageHook = Arc<dyn Fn(&str, bool, u64) + Send + Sync>;

struct Live {
    halt: watch::Sender<Option<Halt>>,
    done: watch::Receiver<bool>,
}

pub struct Runner {
    pub store: Arc<Store>,
    out: Arc<dyn Outbound>,
    usage: Option<UsageHook>,
    live: Mutex<HashMap<String, Live>>,
    /// The one rerun an event asked for while its routine was busy, by `<project>/<name>`:
    /// the latest event's topic and payload.
    pending: Mutex<HashMap<String, (String, Option<String>)>>,
}

/// The largest event payload a step receives in `AWM_ROUTINE_EVENT`.
pub const MAX_EVENT_BYTES: usize = 64 * 1024;

fn s<'a>(row: &'a Row, key: &str) -> &'a str {
    row.get(key).and_then(Value::as_str).unwrap_or("")
}

fn tail(text: &str, n: usize) -> String {
    let count = text.chars().count();
    text.chars().skip(count.saturating_sub(n)).collect()
}

fn git(root: &Path, args: &[&str]) -> Option<String> {
    let out = Command::new("git").arg("-C").arg(root).args(args).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn resolve(path: &str) -> PathBuf {
    let p = match path.strip_prefix("~/") {
        Some(rest) => PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(rest),
        None => PathBuf::from(path),
    };
    p.canonicalize().unwrap_or_else(|_| std::path::absolute(&p).unwrap_or(p))
}

/// A stored routine row as the verbs answer it, with `scheduled` a bool.
pub fn public(mut rec: Row) -> Row {
    let on = rec.get("scheduled").and_then(Value::as_i64).unwrap_or(0) != 0;
    rec.insert("scheduled".into(), json!(on));
    rec
}

impl Runner {
    pub fn new(store: Arc<Store>, out: Arc<dyn Outbound>, usage: Option<UsageHook>) -> Arc<Self> {
        Arc::new(Self { store, out, usage, live: Mutex::new(HashMap::new()), pending: Mutex::new(HashMap::new()) })
    }

    // -- registry -------------------------------------------------------------

    /// Install the routine defined at `path`, a file inside a checkout of `project`. The
    /// definition is stored as read now. An edit needs a reinstall, so a run never picks up a
    /// half-written file.
    pub fn install(&self, project: &str, path: &str) -> Result<Row> {
        let p = resolve(path);
        if !p.is_file() {
            return Err(RoutineError::FileNotFound(format!("no routine definition at {}", p.display())));
        }
        let routine = definitions::load(&p).map_err(|e| RoutineError::Value(e.0))?;
        let parent = p.parent().unwrap_or(Path::new("/"));
        let found = git(parent, &["rev-parse", "--show-toplevel"]).and_then(|root| {
            let sha = git(Path::new(&root), &["rev-parse", "HEAD"])?;
            let dirty = git(Path::new(&root), &["status", "--porcelain", "--", &p.to_string_lossy()])?;
            Some((root, sha, !dirty.is_empty()))
        });
        let (root, sha, dirty) = found.unwrap_or_else(|| (parent.to_string_lossy().into_owned(), String::new(), false));
        if dirty {
            return Err(RoutineError::Value(format!("{} has uncommitted changes. Commit the definition first", p.display())));
        }
        let def = serde_json::to_value(&routine).map_err(anyhow::Error::from)?;
        Ok(public(self.store.put_routine(project, &routine.name, &root, &p.to_string_lossy(), &sha, &def)?))
    }

    pub fn uninstall(&self, project: &str, name: &str) -> Result<bool> {
        if self.store.live_run(project, name)?.is_some() {
            return Err(RoutineError::LiveRunExists(format!("{project}/{name} has a live run. Cancel it first")));
        }
        Ok(self.store.delete_routine(project, name)?)
    }

    pub fn routine(&self, project: &str, name: &str) -> Result<(Row, Routine)> {
        let rec = self.store.get_routine(project, name)?.ok_or_else(|| RoutineError::NotFound(format!("no routine {project}/{name} is installed on this node")))?;
        let def = rec.get("definition").cloned().unwrap_or(Value::Null);
        let routine = serde_json::from_value(def).map_err(|e| RoutineError::Other(anyhow::anyhow!("{project}/{name}: stored definition: {e}")))?;
        Ok((rec, routine))
    }

    // -- runs -----------------------------------------------------------------

    /// Whether a timer fire may run, as the slot it serves. With `catchup_s`, a fire with no
    /// slot in the last `catchup_s` seconds, or one whose slot already ran, is skipped.
    fn timer_gate(&self, project: &str, name: &str) -> Result<std::result::Result<Option<String>, String>> {
        let Some(t) = self.store.timer(project, name)? else { return Ok(Ok(None)) };
        let Some(catchup) = t.catchup_s else { return Ok(Ok(None)) };
        let now = chrono::Utc::now();
        let Some(slot) = timers::next_elapse(&t.on_calendar, now - chrono::Duration::seconds(catchup)) else {
            tracing::warn!("{project}/{name}: systemd cannot place the slot of {:?}; the fire runs", t.on_calendar);
            return Ok(Ok(None));
        };
        if slot > now {
            return Ok(Err(format!("no slot of {} in the last {catchup}s: the fire came later than catchup_s after its slot", t.on_calendar)));
        }
        let slot = crate::iso(slot);
        if !t.last_slot.is_empty() && slot.as_str() <= t.last_slot.as_str() {
            return Ok(Err(format!("the slot {slot} already ran")));
        }
        Ok(Ok(Some(slot)))
    }

    pub async fn trigger(self: &Arc<Self>, project: &str, name: &str, trigger: &str, caller: &str, wait: bool) -> Result<Row> {
        let (run, done) = self.begin(project, name, trigger, caller, None)?;
        if let (true, Some(mut done)) = (wait, done) {
            let _ = done.wait_for(|d| *d).await;
            return Ok(self.store.get_run(s(&run, "id"))?.unwrap_or(run));
        }
        Ok(run)
    }

    /// Start a run and return its record, with a receiver that turns true when it ends. A
    /// skipped timer fire has no receiver.
    fn begin(self: &Arc<Self>, project: &str, name: &str, trigger: &str, caller: &str, event: Option<String>) -> Result<(Row, Option<watch::Receiver<bool>>)> {
        let (rec, routine) = self.routine(project, name)?;
        if trigger == "call" && !routine.call {
            return Err(RoutineError::Value(format!("{project}/{name} runs on its timer only")));
        }
        let mut slot = None;
        if trigger == "timer" {
            match self.timer_gate(project, name)? {
                Ok(s) => slot = s,
                Err(why) => {
                    tracing::info!("{project}/{name}: timer fire skipped: {why}");
                    return Ok((self.store.skipped_run(project, name, caller, &why)?, None));
                }
            }
        }
        let run = self.store.start_run(project, name, trigger, caller)?;
        if let Some(slot) = slot {
            self.store.set_last_slot(project, name, &slot)?;
        }
        let rid = s(&run, "id").to_string();
        let (halt_tx, halt_rx) = watch::channel(None);
        let (done_tx, done_rx) = watch::channel(false);
        self.live.lock().unwrap_or_else(|p| p.into_inner()).insert(rid.clone(), Live { halt: halt_tx, done: done_rx.clone() });
        let me = self.clone();
        let task_run = run.clone();
        let (project, name) = (project.to_string(), name.to_string());
        tokio::spawn(async move {
            let halted = halt_rx.clone();
            me.execute(&rec, &routine, &task_run, event.as_deref(), halt_rx).await;
            // A cancelled or shut-down run leaves no rerun behind.
            let key = format!("{project}/{name}");
            let next = me.pending.lock().unwrap_or_else(|p| p.into_inner()).remove(&key);
            let unhalted = halted.borrow().is_none();
            if let (Some((topic, payload)), true) = (next, unhalted) {
                if let Err(e) = me.begin(&project, &name, "event", &format!("event:{topic}"), payload) {
                    tracing::warn!("{key}: cannot start the pending event rerun: {e}");
                }
            }
            me.live.lock().unwrap_or_else(|p| p.into_inner()).remove(s(&task_run, "id"));
            let _ = done_tx.send(true);
        });
        Ok((run, Some(done_rx)))
    }

    /// Start every installed routine that names `topic` (`<service>.<topic>`). A routine with a
    /// live run keeps one pending rerun instead; further events replace its payload.
    pub fn on_event(self: &Arc<Self>, topic: &str, event: &Value) {
        let payload = match serde_json::to_string(event) {
            Ok(j) if j.len() <= MAX_EVENT_BYTES => Some(j),
            Ok(j) => {
                tracing::warn!("event {topic}: payload is {} bytes, over {MAX_EVENT_BYTES}; the run gets no AWM_ROUTINE_EVENT", j.len());
                None
            }
            Err(_) => None,
        };
        let rows = match self.store.list_routines(None) {
            Ok(r) => r,
            Err(e) => return tracing::error!("event {topic}: cannot list routines: {e}"),
        };
        for rec in rows {
            let listens = rec.get("definition").and_then(|d| d.get("events")).and_then(Value::as_array).is_some_and(|a| a.iter().any(|e| e.as_str() == Some(topic)));
            if listens {
                self.fire_event(s(&rec, "project"), s(&rec, "name"), topic, payload.clone());
            }
        }
    }

    fn fire_event(self: &Arc<Self>, project: &str, name: &str, topic: &str, payload: Option<String>) {
        let key = format!("{project}/{name}");
        let caller = format!("event:{topic}");
        // The live check and the pending insert share one lock, which a finishing run takes
        // after it records its end, so a rerun is never lost between the two.
        loop {
            {
                let mut pending = self.pending.lock().unwrap_or_else(|p| p.into_inner());
                if matches!(self.store.live_run(project, name), Ok(Some(_))) {
                    pending.insert(key, (topic.to_string(), payload));
                    return;
                }
            }
            match self.begin(project, name, "event", &caller, payload.clone()) {
                Err(RoutineError::LiveRunExists(_)) => continue,
                Err(e) => return tracing::warn!("{key}: event {topic} did not start a run: {e}"),
                Ok(_) => return,
            }
        }
    }

    fn halt(&self, rid: &str, why: Halt) -> Option<watch::Receiver<bool>> {
        let live = self.live.lock().unwrap_or_else(|p| p.into_inner());
        let l = live.get(rid)?;
        let _ = l.halt.send(Some(why));
        Some(l.done.clone())
    }

    pub async fn cancel(&self, project: &str, name: &str) -> Result<Value> {
        let Some(live) = self.store.live_run(project, name)? else {
            return Ok(json!({"cancelled": false, "reason": "no live run"}));
        };
        let rid = s(&live, "id").to_string();
        match self.halt(&rid, Halt::Cancel) {
            // A run no task here holds belongs to a dead process.
            None => {
                self.store.finish_run(&rid, "cancelled", "", "no task held the run")?;
            }
            Some(mut done) => {
                let _ = done.wait_for(|d| *d).await;
                self.store.finish_run(&rid, "cancelled", "", "cancelled by call")?;
            }
        }
        Ok(json!({"cancelled": true, "run": self.store.get_run(&rid)?}))
    }

    /// Stop every live run: kill each step's process group and fail the run.
    pub async fn shutdown(&self) {
        let ids: Vec<String> = self.live.lock().unwrap_or_else(|p| p.into_inner()).keys().cloned().collect();
        for rid in ids {
            if let Some(mut done) = self.halt(&rid, Halt::Shutdown) {
                let _ = done.wait_for(|d| *d).await;
            }
        }
    }

    /// Wait for every live run. For tests.
    pub async fn drain(&self) {
        loop {
            let waits: Vec<watch::Receiver<bool>> = self.live.lock().unwrap_or_else(|p| p.into_inner()).values().map(|l| l.done.clone()).collect();
            if waits.is_empty() {
                return;
            }
            for mut w in waits {
                let _ = w.wait_for(|d| *d).await;
            }
        }
    }

    async fn execute(&self, rec: &Row, routine: &Routine, run: &Row, event: Option<&str>, mut halt: proc::HaltRx) {
        let (rid, project) = (s(run, "id"), s(rec, "project"));
        let mut env: HashMap<String, String> = [
            ("AWM_ROUTINE", routine.name.as_str()),
            ("AWM_ROUTINE_PROJECT", project),
            ("AWM_ROUTINE_RUN", rid),
            ("AWM_ROUTINE_TRIGGER", s(run, "trigger")),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        if let Some(e) = event {
            env.insert("AWM_ROUTINE_EVENT".into(), e.into());
        }
        let t0 = Instant::now();
        let ok = self.steps(rec, routine, run, &env, &mut halt).await;
        if let Some(hook) = &self.usage {
            hook(&format!("{project}/{}", routine.name), ok, t0.elapsed().as_millis() as u64);
        }
    }

    async fn steps(&self, rec: &Row, routine: &Routine, run: &Row, env: &HashMap<String, String>, halt: &mut proc::HaltRx) -> bool {
        let rid = s(run, "id");
        let finish = |state: &str, step: &str, error: &str| {
            if let Err(e) = self.store.finish_run(rid, state, step, error) {
                tracing::error!("run {rid}: cannot record {state}: {e}");
            }
        };
        for step in &routine.steps {
            let res = match proc::run(&step.run, s(rec, "root"), step.timeout, env, halt).await {
                Ok(Some(res)) => res,
                Ok(None) => {
                    match *halt.borrow() {
                        Some(Halt::Shutdown) => finish("failed", &step.name, "the routine service stopped mid-run"),
                        _ => finish("cancelled", "", "cancelled by call"),
                    }
                    return false;
                }
                Err(e) => {
                    tracing::error!("run {rid} crashed: {e}");
                    let err = format!("{e:?}");
                    finish("failed", "(service)", &err);
                    self.dispatch_fixer(rec, routine, run, None, "the runner raised", &err).await;
                    return false;
                }
            };
            let mut outcome = json!({"step": step.name, "kind": step.kind, "code": res.code, "timed_out": res.timed_out, "seconds": res.seconds});
            let record = |o: &Value| {
                if let Err(e) = self.store.record_step(rid, o.clone()) {
                    tracing::error!("run {rid}: cannot record step: {e}");
                }
            };
            if step.kind == "condition" && res.code == Some(0) && !res.timed_out {
                outcome["held"] = json!(true);
                record(&outcome);
                self.dispatch_agent(rec, routine, step, run, &res.output).await;
                continue;
            }
            if step.kind == "condition" && res.code == Some(1) {
                outcome["held"] = json!(false);
                record(&outcome);
                continue;
            }
            record(&outcome);
            if !res.ok() {
                let why = if res.timed_out { format!("timed out after {}s", fmt_g(step.timeout)) } else { format!("exited {}", res.code.map(|c| c.to_string()).unwrap_or_else(|| "None".into())) };
                finish("failed", &step.name, &format!("{why}\n{}", res.output));
                self.dispatch_fixer(rec, routine, run, Some(step), &why, &res.output).await;
                return false;
            }
        }
        finish("ok", "", "");
        true
    }

    fn started_by(rec: &Row, run: &Row) -> String {
        let who = match s(run, "caller") {
            "" => format!("its {} trigger", s(run, "trigger")),
            c => c.to_string(),
        };
        format!("routine {}/{} run {}, started by {who}", s(rec, "project"), s(rec, "name"), s(run, "id"))
    }

    fn record_dispatch(&self, rid: &str, mut entry: Value, esc: Value) {
        if let (Some(e), Some(x)) = (entry.as_object_mut(), esc.as_object()) {
            e.extend(x.clone());
        }
        if let Err(e) = self.store.record_dispatch(rid, entry) {
            tracing::error!("run {rid}: cannot record dispatch: {e}");
        }
    }

    async fn dispatch_agent(&self, rec: &Row, routine: &Routine, step: &Step, run: &Row, output: &str) {
        let (project, rid) = (s(rec, "project"), s(run, "id"));
        let brief = if step.prompt.is_empty() { &routine.agent.prompt } else { &step.prompt };
        let prompt = format!(
            "{brief}\n\nThe routine `{project}/{}` (run {rid}) found that the condition `{}` holds. Its output:\n\n{}",
            routine.name,
            step.name,
            tail(output, 2000)
        );
        let scope = if routine.agent.scope.is_empty() { routine.name.clone() } else { routine.agent.scope.clone() };
        let start = Start {
            peer: routine.agent.peer.clone(),
            project: project.into(),
            scope,
            prompt,
            started_by: Self::started_by(rec, run),
            name: format!("{}-{rid}", routine.name),
        };
        let summary = format!("Routine {project}/{}: condition {} holds.", routine.name, step.name);
        let esc = escalate(self.out.as_ref(), start, &summary).await;
        self.record_dispatch(rid, json!({"for": step.name, "role": "agent"}), esc.to_json());
    }

    async fn dispatch_fixer(&self, rec: &Row, routine: &Routine, run: &Row, step: Option<&Step>, why: &str, output: &str) {
        let (project, rid) = (s(rec, "project"), s(run, "id"));
        let sname = step.map(|s| s.name.as_str()).unwrap_or("(service)");
        let summary = format!("Routine {project}/{} failed at {sname} (run {rid}): {why}.", routine.name);
        let Some(fixer) = &routine.fixer else {
            let esc = notice(self.out.as_ref(), format!("{summary}\n```\n{}\n```", tail(output, 1500)), "", vec![]).await;
            self.record_dispatch(rid, json!({"for": sname, "role": "notice"}), esc.to_json());
            return;
        };
        let sha: String = s(rec, "commit_sha").chars().take(12).collect();
        let mut prompt = format!(
            "The routine `{project}/{name}` failed (run {rid}). Step `{sname}` {why}.\n\nIts output:\n\n{out}\n\n\
             The definition is `{path}`, installed at commit {sha}. Find the cause and fix it. Then run \
             `routine trigger` with project={project} name={name} to confirm the fix. Read the run with `routine runs`.",
            name = routine.name,
            out = tail(output, 3000),
            path = s(rec, "path"),
            sha = if sha.is_empty() { "unknown" } else { &sha },
        );
        if !fixer.prompt.is_empty() {
            prompt += &format!("\n\n{}", fixer.prompt);
        }
        let start = Start {
            peer: fixer.peer.clone(),
            project: project.into(),
            scope: if fixer.scope.is_empty() { format!("fix-{}", routine.name) } else { fixer.scope.clone() },
            prompt,
            started_by: Self::started_by(rec, run),
            name: format!("fix-{}-{rid}", routine.name),
        };
        let esc = escalate(self.out.as_ref(), start, &summary).await;
        self.record_dispatch(rid, json!({"for": sname, "role": "fixer"}), esc.to_json());
    }
}
