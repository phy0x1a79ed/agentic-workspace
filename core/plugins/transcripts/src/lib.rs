//! transcripts: Claude Code session logs swept from `~/.claude/projects` into a gzipped mirror
//! at `~/.claude/projects-archive`, restored and pruned, and served as the opt-in live search
//! source `transcripts`.
//!
//! `prune` is the one verb that destroys data, so it stays off the MCP surface and dry-runs
//! unless told otherwise. A peer may not sweep or prune, and sees nothing of a privately
//! marked project, nor of a session that ever worked under a marked path.

pub mod daily;
pub mod layout;
pub mod runs;
pub mod search;
pub mod sweep;

use std::collections::HashSet;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::privacy;
use awm_plugin_sdk::{Call, CallOpts, Context, Service, VerbError};
use awm_store::Store;
use serde_json::{json, Value};

use crate::layout::{sessions, Side, Tree};

const TOOL: &str = "transcripts";

/// A verb's failure as the Python raised it: the exception's class and its text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fail {
    pub class: &'static str,
    pub message: String,
}

impl Fail {
    pub fn new(class: &'static str, message: impl Into<String>) -> Self {
        Self { class, message: message.into() }
    }
}

impl From<Fail> for VerbError {
    fn from(f: Fail) -> Self {
        VerbError::new(f.class, f.message)
    }
}

impl From<awm_store::StoreError> for Fail {
    fn from(e: awm_store::StoreError) -> Self {
        Fail::new("OperationalError", e.to_string())
    }
}

pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Which Claude sessions a running Claude Code process holds. Unreadable means live: an error
/// stops the sweep.
pub trait Live: Send + Sync {
    fn session_ids(&self) -> BoxFut<'_, Result<HashSet<String>, String>>;
}

/// cch's answer through the gateway.
pub struct Cch(pub Context);

impl Live for Cch {
    fn session_ids(&self) -> BoxFut<'_, Result<HashSet<String>, String>> {
        Box::pin(async move {
            let opts = CallOpts { timeout: Duration::from_secs(30), ..CallOpts::default() };
            let out = match self.0.hub().call_with("harness-cc", "live_session_ids", json!({}), &opts).await {
                Ok(v) => v,
                Err(e) => {
                    tracing::error!("transcripts: sweep blocked, cch did not answer: {e}");
                    return Err(format!("cch did not answer live_session_ids: {e}"));
                }
            };
            live_ids(&out)
        })
    }
}

/// The ids in cch's `live_session_ids` answer, or why it is no list.
pub fn live_ids(out: &Value) -> Result<HashSet<String>, String> {
    if !out.get("ok").is_some_and(truthy) {
        let reason = match out {
            Value::Object(m) => m.get("reason").map_or("None".to_string(), py_str),
            other => py_str(other),
        };
        tracing::error!("transcripts: sweep blocked: {reason}");
        return Err(format!("cch cannot list the live sessions: {reason}"));
    }
    Ok(out["session_ids"].as_array().into_iter().flatten().map(py_str).collect())
}

/// The plugin's state: the trees, the run history and cch.
pub struct App {
    pub tree: Tree,
    pub store: Store,
    pub live: Box<dyn Live>,
}

/// Python's `str()` of a JSON value.
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => awm_pycompat::repr::repr(other),
    }
}

/// Python truthiness.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn py_float(v: &Value) -> Result<f64, Fail> {
    match v {
        Value::Number(n) => Ok(n.as_f64().unwrap_or(0.0)),
        Value::Bool(b) => Ok(*b as i64 as f64),
        Value::String(s) => awm_pycompat::pystr::strip(s).replace('_', "").parse::<f64>().map_err(|_| Fail::new("ValueError", format!("could not convert string to float: {}", awm_pycompat::repr::str_repr(s)))),
        other => Err(Fail::new("TypeError", format!("float() argument must be a string or a real number, not '{}'", type_name(other)))),
    }
}

fn py_int(v: &Value) -> Result<i64, Fail> {
    match v {
        Value::Number(n) => Ok(n.as_i64().unwrap_or_else(|| n.as_f64().unwrap_or(0.0).trunc() as i64)),
        Value::Bool(b) => Ok(*b as i64),
        Value::String(s) => awm_pycompat::pystr::strip(s).parse::<i64>().map_err(|_| Fail::new("ValueError", format!("invalid literal for int() with base 10: {}", awm_pycompat::repr::str_repr(s)))),
        other => Err(Fail::new("TypeError", format!("int() argument must be a string, a bytes-like object or a real number, not '{}'", type_name(other)))),
    }
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) => "float",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// `a.get(key) or default`, as a float.
fn float_or(a: &Value, key: &str, default: f64) -> Result<f64, Fail> {
    match a.get(key) {
        Some(v) if truthy(v) => py_float(v),
        _ => Ok(default),
    }
}

fn int_or(a: &Value, key: &str, default: i64) -> Result<i64, Fail> {
    match a.get(key) {
        Some(v) if truthy(v) => py_int(v),
        _ => Ok(default),
    }
}

/// `a[key]`, which raises `KeyError` when absent.
fn required<'a>(a: &'a Value, key: &str) -> Result<&'a Value, Fail> {
    a.get(key).ok_or_else(|| Fail::new("KeyError", awm_pycompat::repr::str_repr(key)))
}

fn str_arg(a: &Value, key: &str) -> Option<String> {
    a.get(key).filter(|v| truthy(v)).map(py_str)
}

type Test = fn(&str) -> bool;

/// For a peer caller, the test that leaves out a marked project's sessions.
fn hide(as_: Option<&str>) -> Option<Test> {
    privacy::from_peer(as_).then_some(privacy::hidden_slug as Test)
}

/// For a peer caller, the test that leaves out a session that ever worked under a marked path.
fn hide_path(as_: Option<&str>) -> Option<Test> {
    privacy::from_peer(as_).then_some(privacy::hidden_path as Test)
}

fn dynref(t: &Option<Test>) -> Option<&dyn Fn(&str) -> bool> {
    t.as_ref().map(|f| f as &dyn Fn(&str) -> bool)
}

/// Refuse a peer the maintenance verbs, in the words used for a verb that does not exist.
fn local_only(name: &str, as_: Option<&str>) -> Result<(), Fail> {
    if privacy::from_peer(as_) {
        return Err(Fail::new("RuntimeError", format!("unknown function {}", awm_pycompat::repr::str_repr(name))));
    }
    Ok(())
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Result<T, Fail> {
    tokio::task::spawn_blocking(f).await.map_err(|e| Fail::new("RuntimeError", e.to_string()))
}

fn tally(root: &std::path::Path, side: Side, days: f64, hide: Option<&dyn Fn(&str) -> bool>) -> serde_json::Map<String, Value> {
    let all = sessions(root, side, hide);
    let old = sweep::aged(root, side, days, hide);
    let mut m = serde_json::Map::new();
    m.insert("sessions".into(), json!(all.len()));
    m.insert("orphans".into(), json!(all.iter().filter(|s| s.transcript.is_none()).count()));
    m.insert("bytes".into(), json!(all.iter().map(layout::Session::bytes).sum::<u64>()));
    m.insert("past_retention".into(), json!(old.len()));
    m.insert("past_retention_bytes".into(), json!(old.iter().map(layout::Session::bytes).sum::<u64>()));
    m
}

/// What the live tree and the archive hold. `past_retention` is the backlog: a sweep that runs
/// while it grows is not working.
pub fn status_report(tree: &Tree, days: f64, prune_days: Option<f64>, hide: Option<&dyn Fn(&str) -> bool>) -> Value {
    let mut live = serde_json::Map::new();
    live.insert("path".into(), json!(tree.projects.to_string_lossy()));
    live.insert("retention_days".into(), json!(days));
    live.extend(tally(&tree.projects, Side::Live, days, hide));
    let mut archive = serde_json::Map::new();
    archive.insert("path".into(), json!(tree.archive.to_string_lossy()));
    archive.insert("retention_days".into(), json!(prune_days));
    archive.extend(tally(&tree.archive, Side::Archive, prune_days.unwrap_or(1e9), hide));
    json!({"live": live, "archive": archive})
}

impl App {
    pub async fn status(&self, a: &Value, as_: Option<&str>) -> Result<Value, Fail> {
        let days = float_or(a, "days", sweep::DEFAULT_RETENTION_DAYS)?;
        let prune_days = match a.get("prune_days") {
            Some(v) if truthy(v) => Some(py_float(v)?),
            _ => None,
        };
        let (tree, h) = (self.tree.clone(), hide(as_));
        blocking(move || status_report(&tree, days, prune_days, dynref(&h))).await
    }

    /// Sweep with the live list fetched first; no list, no sweep.
    pub async fn sweep(&self, a: &Value, as_: Option<&str>, trigger: &str) -> Result<Value, Fail> {
        local_only("sweep", as_)?;
        let days = float_or(a, "days", sweep::DEFAULT_RETENTION_DAYS)?;
        let dry_run = a.get("dry_run").is_some_and(truthy);
        let live = self.live.session_ids().await.map_err(|m| Fail::new("Unknown", m))?;
        let tree = self.tree.clone();
        let mut result = blocking(move || sweep::archive(&tree, days, dry_run, &live, None)).await?;
        let trigger = a.get("_trigger").and_then(Value::as_str).unwrap_or(trigger).to_string();
        result["run"] = json!(runs::record(&self.store, "sweep", &result, &trigger).await?);
        Ok(result)
    }

    pub async fn prune(&self, a: &Value, as_: Option<&str>) -> Result<Value, Fail> {
        local_only("prune", as_)?;
        let days = py_float(required(a, "days")?)?;
        let dry_run = a.get("dry_run") != Some(&json!(false));
        let tree = self.tree.clone();
        let mut result = blocking(move || sweep::prune(&tree, days, dry_run, None)).await?;
        let trigger = a.get("_trigger").and_then(Value::as_str).unwrap_or("manual").to_string();
        result["run"] = json!(runs::record(&self.store, "prune", &result, &trigger).await?);
        Ok(result)
    }

    pub async fn search(&self, a: &Value, as_: Option<&str>) -> Result<Value, Fail> {
        let pattern = py_str(required(a, "pattern")?);
        let rx = search::compile(&pattern).map_err(|e| Fail::new("error", e))?;
        let (project, limit) = (str_arg(a, "project"), int_or(a, "limit", search::MAX_HITS)?);
        let (tree, h, hp) = (self.tree.clone(), hide(as_), hide_path(as_));
        blocking(move || search::find(&tree, &pattern, &rx, project.as_deref(), limit, dynref(&h), dynref(&hp))).await
    }

    pub async fn query(&self, a: &Value, as_: Option<&str>) -> Result<Value, Fail> {
        let words = py_str(required(a, "query")?);
        let (project, limit) = (str_arg(a, "project"), int_or(a, "limit", search::QUERY_MAX_HITS)?);
        let (tree, h, hp) = (self.tree.clone(), hide(as_), hide_path(as_));
        blocking(move || search::query(&tree, &words, project.as_deref(), limit, dynref(&h), dynref(&hp))).await
    }

    /// A peer gets the answer for a session that was never archived, for a marked project or a
    /// session that entered a marked path.
    pub async fn restore(&self, a: &Value, as_: Option<&str>) -> Result<Value, Fail> {
        let project = py_str(required(a, "project")?);
        let session = py_str(required(a, "session")?);
        let (tree, h, hp) = (self.tree.clone(), hide(as_), hide_path(as_));
        blocking(move || {
            if let (Some(h), Some(hp)) = (h, hp) {
                let transcript = tree.archive.join(&project).join(format!("{session}.jsonl.gz"));
                let wandered = sweep::plain_name(&project) && sweep::plain_name(&session) && transcript.exists() && search::entered(&transcript, &hp);
                if h(&project) || wandered {
                    return Err(sweep::not_archived(&project, &session));
                }
            }
            sweep::restore(&tree, &project, &session)
        })
        .await?
    }

    pub async fn runs(&self, a: &Value) -> Result<Value, Fail> {
        let limit = int_or(a, "limit", 20)?;
        Ok(json!({"runs": runs::recent(&self.store, limit).await?}))
    }
}

/// A param as describe lists it: the type only when it is not a string.
fn p(name: &str, ty: &str, required: bool, description: &str) -> Value {
    let mut v = json!({"name": name, "type": ty, "required": required, "description": description});
    if ty == "string" {
        v.as_object_mut().map(|m| m.remove("type"));
    }
    v
}

fn days_param() -> Value {
    p("days", "number", false, "Retention in days. A session is aged when its newest file, transcript or sidecar, is older than this. Default 7.")
}

macro_rules! verb {
    ($svc:expr, $app:expr, $name:literal, $desc:expr, $params:expr, $secs:expr, |$a:ident, $call:ident| $body:expr) => {{
        let app = $app.clone();
        $svc.verb(TOOL, $name, $desc, $params, move |$call: Call| {
            let $a = app.clone();
            async move { Ok::<Value, VerbError>($body.await?) }
        })
        .timeout(Duration::from_secs($secs))
    }};
}

/// transcripts' verbs on `svc`.
pub fn service(svc: Service, app: Arc<App>) -> Service {
    let svc = verb!(svc, app, "status",
        "Report the live session tree and the archive: session count, orphaned sidecars, bytes, and how many sessions are past the retention and still unswept. past_retention is the backlog — a sweep that runs while that number grows is not working.",
        json!([days_param(), p("prune_days", "number", false, "Also report how much of the archive is older than this many days.")]), 120,
        |a, call| a.status(&call.args, call.as_.as_deref()));
    let svc = verb!(svc, app, "sweep",
        "Gzip every session older than the retention into the archive and remove it from the live tree. The unit is the session, so a transcript and its sidecar directory move together. Reversible with restore.",
        json!([days_param(), p("dry_run", "boolean", false, "Report what would move, and move nothing.")]), 900,
        |a, call| a.sweep(&call.args, call.as_.as_deref(), "manual"));
    let svc = verb!(svc, app, "search",
        "Regex-search archived transcripts without unpacking them. Returns at most 200 matching lines.",
        json!([
            p("pattern", "string", true, "Python regular expression."),
            p("project", "string", false, "Restrict to project directories containing this substring."),
            p("limit", "number", false, "Maximum hits to return. Default 200."),
        ]), 300,
        |a, call| a.search(&call.args, call.as_.as_deref()));
    let svc = verb!(svc, app, "restore",
        "Put one archived session back into the live tree, transcript and sidecar together. Refuses rather than overwrites when the live copy already exists.",
        json!([
            p("project", "string", true, "Project directory name, as status reports it."),
            p("session", "string", true, "Session id."),
        ]), 300,
        |a, call| a.restore(&call.args, call.as_.as_deref()));
    let svc = verb!(svc, app, "runs", "Recent sweeps and prunes, newest first.", json!([p("limit", "number", false, "Rows to return. Default 20.")]), 60,
        |a, call| a.runs(&call.args));
    let svc = verb!(svc, app, "prune",
        "Delete archived sessions older than the retention. This is the only irreversible operation here, so it dry-runs unless dry_run is explicitly false.",
        json!([
            p("days", "number", true, "Archive retention in days. There is no default: deleting needs a number somebody chose."),
            p("dry_run", "boolean", false, "Defaults to true. Pass false to delete."),
        ]), 900,
        |a, call| a.prune(&call.args, call.as_.as_deref()))
    .hidden();
    verb!(svc, app, "query",
        "Search archived transcripts for words, ignoring case, and return the lines as search hits. This is the archive's live `transcripts` source: the search service calls it at query time and only when asked for the source by name. Only `query`, `project` and `limit` narrow it; the other filters are accepted and ignored.",
        json!([
            p("query", "string", true, "Words to find, matched literally."),
            p("project", "string", false, "Restrict to project directories containing this substring."),
            p("scope", "string", false, "Ignored."),
            p("kind", "string", false, "Ignored."),
            p("after", "string", false, "Ignored."),
            p("before", "string", false, "Ignored."),
            p("limit", "number", false, "Maximum hits. Default 20."),
        ]), 300,
        |a, call| a.query(&call.args, call.as_.as_deref()))
    .hidden()
}
