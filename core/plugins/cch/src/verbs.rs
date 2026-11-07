//! Tool `harness-cc`, the harness contract over Claude Code sessions: its 29 verbs as one table of
//! specs, and the dispatcher that maps each verb's arguments onto the module that does the work.
//!
//! A verb that acts on a session names it with `session_id` (R9) or `ref` (a job id, a name or a
//! session UUID). With neither it acts on the session at `pid`, the caller pid reflection passes.

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use awm_harness::claude::{live, records, ClaudeHome};
use awm_harness::contract::args::{bool_lenient, int_lenient};
use awm_harness::contract::{self, target_params};
use awm_plugin_sdk::{procfs, Call, Service, VerbError};
use serde_json::{json, Map, Value};

use crate::app::{Backend, Op, PeerCall};
use crate::control::{ControlError, Target};
use crate::lane::LaneError;
use crate::resume::{r9, PLATFORM};
use crate::sender::{SendError, SendRequest};
use crate::target::ResolveError;
use crate::{transcript, watcher};

pub const TOOL: &str = "harness-cc";
pub const NAME: &str = "Claude Code";
/// The tool's description in the manifest.
pub const DESCRIPTION: &str = "The Claude Code harness: finds, starts, stops, resumes and types into Claude Code \
                               sessions on this node, and watches a command with a follow-up through to its end. \
                               reflection calls it; it is not on MCP.";
/// The verbs every harness@1 provider serves.
pub const CONTRACT_VERBS: [&str; 12] =
    ["platform", "resolve", "find", "status", "list", "send", "interrupt", "start", "stop", "resume", "set", "compact"];
/// The optional verbs `platform` lists as capabilities: every verb outside the contract.
pub const OPTIONAL_VERBS: [&str; 17] = [
    "mode",
    "remote_control",
    "mcp",
    "link",
    "adopt",
    "revive_pinned",
    "pending",
    "probe",
    "live_session_ids",
    "caller_of_token",
    "pool_claim",
    "pool_seed",
    "pool_remove",
    "daemon_status",
    "daemon_ensure",
    "daemon_hold",
    "daemon_release",
];
/// The verbs part 2 of the port serves; until then each answers [`not_yet_ported`].
pub const PART_TWO: [&str; 13] = [
    "list",
    "start",
    "resume",
    "adopt",
    "revive_pinned",
    "caller_of_token",
    "pool_claim",
    "pool_seed",
    "pool_remove",
    "daemon_status",
    "daemon_ensure",
    "daemon_hold",
    "daemon_release",
];
/// Shorter than a seat keeper's own call to the witness, so a slow peer still gets an answer
/// back to the keeper.
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(20);

const REF_TEXT: &str = "A job id, a session name or a Claude session UUID.";

/// One published verb.
pub struct Spec {
    pub verb: &'static str,
    pub description: &'static str,
    pub params: Value,
    /// Seconds the gateway waits for the reply, when not its default.
    pub timeout: Option<u64>,
}

fn param(name: &str, ty: &str, description: &str, required: bool) -> Value {
    json!({"name": name, "type": ty, "required": required, "description": description})
}

fn opt(name: &str, ty: &str, description: &str) -> Value {
    param(name, ty, description, false)
}

fn req(name: &str, ty: &str, description: &str) -> Value {
    param(name, ty, description, true)
}

fn targeted(extra: Vec<Value>) -> Value {
    let mut out = target_params(REF_TEXT);
    out.extend(extra);
    Value::Array(out)
}

fn spec(verb: &'static str, description: &'static str, params: Value, timeout: Option<u64>) -> Spec {
    Spec { verb, description, params, timeout }
}

fn start_params() -> Value {
    json!([
        opt("project", "string", "The project the scope belongs to. Without a scope it is only a hint in the brief."),
        opt("scope", "string", "The scope whose worktree is the session's directory. Omit it to start in the lobby."),
        opt("suggest", "boolean", "Treat project and scope as a suggested route, as for a follow-up session (sessions.md R18): start in the lobby and let the session choose."),
        opt("task_id", "string", "The task the session joins (sessions.md R57 step 3). Pass your own from reflection whoami when you start a session for your task."),
        opt("role", "string", "main or worker on the task. Defaults to worker when a session calls, else main."),
        opt("prompt", "string", "The first task. Without one the session starts empty."),
        opt("system_prompt", "string", "Text appended to the system prompt (claude --append-system-prompt). Unlike the prompt, a compaction never rewrites it."),
        opt("autocompact", "integer", "The auto-compact window in tokens, 100000 to 1000000. Claude Code compacts about 33k below it. Omitted means the model's own window."),
        opt("name", "string", "The session's name, <task>/<descriptor> for the sessions of one task (sessions.md R56). Defaults to the scope, or lobby-<hex> in the lobby."),
        req("model", "string", "A model alias or full name, chosen to fit the task. A call without one is refused."),
        req("effort", "string", "low, medium or high, chosen to fit the task. High is the upper limit; xhigh and max are refused."),
        opt("permission", "string", "A permission mode (plan, acceptEdits, auto, manual, dontAsk, bypassPermissions). Omitted means skip-permissions. The session starts in this mode and can always switch to bypass, so a plan-mode start runs in bypass once its plan is approved."),
        opt("mode", "string", "A label recorded with the session, which a gateway gate can key on. Defaults to worker."),
        opt("disallowed_tools", "array", "Tool names the session may not use."),
        opt("allowed_tools", "array", "Tool names approved without a prompt, which keeps a dontAsk session from having them denied (for example mcp__awm__*)."),
        opt("tools", "array", "The only built-in tools the session holds (claude --tools). Omitted means all."),
        opt("restricted", "boolean", "Confine file tools to the worktree and ignore user settings (claude --restricted)."),
        opt("strict_mcp", "boolean", "Load only the awm MCP server, from the workspace config."),
        opt("remote_control", "boolean", "Remote Control, on unless false, named for the session with each `/` as `-`; a plain label names it instead. The result's `link` is the claude.ai link, null with a `link_reason` if it has not connected within a few seconds."),
        opt("pin", "boolean", "Pin the job, so the daemon's idle retirement passes it over. A pinned job is a kept session: stopping it unpins it and removes it with `claude rm`."),
        opt("brief", "boolean", "Append the worker lifecycle brief to the system prompt: work in the right scope, ask Tony through the board, sign off and stop when done. Default true. A pinned start never gets it."),
        opt("started_by", "string", "Who the brief says started the session when no session called, such as 'Tony from Discord'. A calling session is named instead."),
        opt("replaces", "string", "The job this session replaces. Its last turns are appended to the prompt, it is stopped (and removed when pinned) if cx started it, and the new job's lineage records it."),
        opt("tags", "array", "Tags to set on the scope (scope tag), such as ['lab', '<person>'] for work a lab member asked for. A tagging failure is returned as a warning and does not stop the start."),
        opt("pid", "integer", "The starting session's pid."),
        opt("trust", "string", ""),
        opt("isolated", "boolean", "Run the session in a rootless container with a whitelist of awm verbs (fleet.md R109)."),
    ])
}

/// Every verb `harness-cc` publishes, in the Python manifest's order.
pub fn specs() -> Vec<Spec> {
    vec![
        spec("platform", "The platform code, name and optional verbs.", json!([]), None),
        spec(
            "resolve",
            "Whether the session at pid is a Claude Code session, and which.",
            json!([req("pid", "integer", "")]),
            Some(30),
        ),
        spec("find", "The sessions a ref names.", json!([req("ref", "string", "")]), Some(30)),
        spec("status", "A session's live state.", targeted(vec![]), Some(30)),
        spec(
            "list",
            "Every Claude Code session on this node, with lineage and the warm pool.",
            json!([
                opt("project", "string", "Only sessions in this project."),
                opt("scope", "string", "Only sessions in this scope."),
                opt("include_stopped", "boolean", ""),
            ]),
            None,
        ),
        spec(
            "send",
            "Type text or a slash command into a session and submit it.",
            targeted(vec![
                req("text", "string", ""),
                opt("submit", "boolean", "Press Enter (default true)."),
                opt("confirm", "boolean", "Allow a destructive command."),
                opt("followup", "string", "For the caller's own slash command: the prompt to type once it finishes."),
                opt("delay_ms", "integer", ""),
            ]),
            Some(60),
        ),
        spec("interrupt", "Press Escape in a session.", targeted(vec![]), Some(30)),
        spec("start", "Start a named background session in a scope's worktree.", start_params(), Some(1800)),
        spec(
            "stop",
            "Stop a session and remove its job. With keep, the job stays and a resume brings it back: only a wait keeps it.",
            targeted(vec![opt("keep", "boolean", ""), opt("confirm", "boolean", "")]),
            Some(30),
        ),
        spec(
            "resume",
            "Bring back a stopped session, or a retired one in a new job, from its transcript.",
            targeted(vec![
                opt("prompt", "string", ""),
                opt("adopt_lineage", "boolean", "Resume a job that start did not create."),
            ]),
            Some(90),
        ),
        spec(
            "set",
            "Set a session's model, effort, name or folder.",
            targeted(vec![
                opt("model", "string", ""),
                opt("effort", "string", ""),
                opt("name", "string", ""),
                opt("cwd", "string", ""),
                opt("brief", "string", "With cwd: the line the session gets once the move applied, such as the new scope's brief: the caller's own in its follow-up, another session as a message."),
            ]),
            Some(120),
        ),
        spec(
            "compact",
            "Compact the caller's own session, then type followup.",
            targeted(vec![opt("followup", "string", ""), opt("delay_ms", "integer", "")]),
            Some(60),
        ),
        spec(
            "mode",
            "Put the caller's own session back into bypass permissions.",
            json!([opt("pid", "integer", ""), opt("expect_session", "string", "")]),
            Some(60),
        ),
        spec(
            "remote_control",
            "Turn Remote Control on or off.",
            targeted(vec![req("on", "boolean", ""), opt("name", "string", "")]),
            Some(60),
        ),
        spec(
            "mcp",
            "Reconnect, enable or disable a session's MCP servers.",
            targeted(vec![
                req("action", "string", "reconnect, enable or disable."),
                req("server", "string", "An MCP server name, or all."),
            ]),
            Some(60),
        ),
        spec("link", "A session's claude.ai link.", targeted(vec![]), Some(30)),
        spec("adopt", "Take on a job started outside start.", targeted(vec![req("job", "string", "The job id.")]), Some(30)),
        spec("revive_pinned", "Resume each pinned job whose process is gone.", json!([]), Some(600)),
        spec("pending", "The follow-ups cch still owes.", json!([]), None),
        spec(
            "probe",
            "Whether cch on `node` answers `list` from this node, and its daemon state. Returns no sessions. A seat \
             keeper on another node asks this to tell its own lost contact from the node being down.",
            json!([req("node", "string", "")]),
            Some(30),
        ),
        spec(
            "live_session_ids",
            "The Claude session UUIDs a running Claude Code process holds. Refuses when a record cannot be read, so \
             a caller never acts on a shorter list than the truth.",
            json!([]),
            Some(30),
        ),
        spec(
            "caller_of_token",
            "The isolated session that holds a caller token, and the verbs it may call.",
            json!([req("token", "string", "")]),
            Some(10),
        ),
        spec(
            "pool_claim",
            "Claim the warm session for a folder.",
            json!([req("cwd", "string", "The directory to move the session to.")]),
            Some(9),
        ),
        spec("pool_seed", "Seed one warm session.", json!([]), Some(60)),
        spec(
            "pool_remove",
            "Plan or apply removal of stale pool sessions.",
            json!([opt("apply", "boolean", "Carry out the plan (default false).")]),
            Some(120),
        ),
        spec(
            "daemon_status",
            "Show the Claude Code daemon on this node: pid, origin, unit, uptime, the unit's restart count and result, \
             why the daemon last stopped, recent starts, whether it is held, and linger.",
            json!([]),
            None,
        ),
        spec(
            "daemon_ensure",
            "Start the Claude Code daemon now if none runs, and wait until it answers. Returns ok=false with \
             reason=daemon_down when it does not come up in time, is held, or cannot run here. Pass peer=<node> to \
             bring another node's daemon back with no ssh. When this verb itself does not answer, the node's keep \
             service is down: run `awm services restart cch` on that node.",
            json!([opt("timeout", "number", "Seconds to wait for the daemon. Default 30.")]),
            Some(60),
        ),
        spec(
            "daemon_hold",
            "Stop the Claude Code daemon and keep it stopped until daemon_release or the hold expires. Its sessions \
             keep running for 20 minutes without the daemon, then die, so a hold lasts at most that long. Nothing \
             can start, resume or message a session while it is held.",
            json!([
                req("reason", "string", "Why, shown in daemon_status."),
                opt("seconds", "number", "How long the hold lasts. Default 600, at most 1200."),
            ]),
            Some(60),
        ),
        spec("daemon_release", "End a daemon_hold and start the daemon again.", json!([]), Some(60)),
    ]
}

/// Every verb of [`specs`] bound to `app`, under the tool's description.
pub fn serve<B: Backend>(svc: Service, app: Arc<App<B>>) -> Service {
    let svc = svc.api("description", json!(DESCRIPTION));
    specs().into_iter().fold(svc, |svc, spec| {
        let (verb, app) = (spec.verb, app.clone());
        let svc = svc.verb(TOOL, verb, spec.description, spec.params, move |c: Call| {
            let app = app.clone();
            async move { app.handle(verb, &c.args, c.as_.as_deref()).await }
        });
        match spec.timeout {
            Some(t) => svc.timeout(Duration::from_secs(t)),
            None => svc,
        }
    })
}

/// The one answer of a verb, or a branch of one, that part 2 of the port has not landed yet.
pub fn not_yet_ported(what: &str) -> Value {
    json!({"ok": false, "code": "not_yet_ported", "error": format!("{what} is not ported to the Rust cch yet")})
}

// --- arguments ---------------------------------------------------------------

/// Python's `repr` of a string, as the refusal texts quote one.
pub(crate) fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// `str(v or "")`: a falsy value is empty.
fn text_or_empty(v: Option<&Value>) -> String {
    match v {
        _ if !truthy(v) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(true)) => "True".into(),
        Some(other) => other.to_string(),
        None => String::new(),
    }
}

/// `v or None` for a string argument.
fn opt_text(v: Option<&Value>) -> Option<String> {
    Some(text_or_empty(v)).filter(|s| !s.is_empty())
}

fn pid_int(args: &Value) -> Option<i64> {
    match args.get("pid")? {
        Value::String(s) => s.trim().parse().ok(),
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f.trunc() as i64)),
        Value::Bool(b) => Some(i64::from(*b)),
        _ => None,
    }
}

/// The caller pid a verb acts for, or None.
fn pid_arg(args: &Value) -> Option<u32> {
    pid_int(args).and_then(|p| u32::try_from(p).ok())
}

fn delay_ms(args: &Value) -> u64 {
    u64::try_from(int_lenient(args.get("delay_ms"), 0)).unwrap_or(0)
}

fn flag(args: &Value, key: &str, default: bool) -> bool {
    bool_lenient(args.get(key), default)
}

/// A Claude session UUID from the digits of an R9 id; digits of any other length pass as they are.
fn uuid_of(session_id: &str) -> String {
    let digits: Vec<char> = session_id.chars().skip(2).collect();
    if digits.len() != 32 {
        return digits.into_iter().collect();
    }
    let part = |a: usize, b: usize| digits[a..b].iter().collect::<String>();
    [part(0, 8), part(8, 12), part(12, 16), part(16, 20), part(20, 32)].join("-")
}

/// The `job` control takes: the session `session_id` or `ref` names, or None for the caller's own.
fn session_ref(args: &Value) -> Result<Option<String>, ResolveError> {
    let sid = text_or_empty(args.get("session_id")).trim().to_lowercase();
    if !sid.is_empty() {
        if !sid.starts_with(PLATFORM) {
            return Err(ResolveError(format!(
                "{} is not a Claude Code session id (it does not start with {})",
                py_repr(&sid),
                py_repr(PLATFORM)
            )));
        }
        return Ok(Some(uuid_of(&sid)));
    }
    Ok(Some(text_or_empty(args.get("ref")).trim().to_string()).filter(|r| !r.is_empty()))
}

/// A raw Claude session id in `session_id` moves to `claude_session_id`, its R9 id taking its place.
fn with_ids(mut out: Value) -> Value {
    let raw = out.get("session_id").and_then(Value::as_str).filter(|s| !s.is_empty() && !awm_plugin_sdk::ids::is_r9(s));
    if let Some(sid) = raw.map(str::to_string) {
        out["claude_session_id"] = json!(sid);
        out["session_id"] = json!(r9(&sid));
    }
    out
}

fn row(t: &Target) -> Value {
    with_ids(json!({
        "session_id": t.session_id, "name": t.name, "job": t.job, "pid": t.pid, "live": t.alive, "kind": t.kind.as_str(),
    }))
}

/// A job's own name. The session record keeps the name it was seeded with.
fn job_name(home: &ClaudeHome, job: &str) -> Option<String> {
    opt_text(records::read_json(&home.job_state(job)).get("name"))
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    match tokio::task::spawn_blocking(f).await {
        Ok(v) => v,
        Err(e) => std::panic::resume_unwind(e.into_panic()),
    }
}

// --- failures ----------------------------------------------------------------

/// How a verb failed: a refusal the caller reads, or a caller bug the gateway answers as an error.
#[derive(Debug)]
enum Failure {
    Refused { error: String, unknown_ref: Option<String> },
    Caller(String),
}

impl Failure {
    fn refused(e: impl ToString) -> Self {
        Self::Refused { error: e.to_string(), unknown_ref: None }
    }
}

impl From<ResolveError> for Failure {
    fn from(e: ResolveError) -> Self {
        Self::refused(e)
    }
}

impl From<LaneError> for Failure {
    fn from(e: LaneError) -> Self {
        Self::refused(e)
    }
}

impl From<ControlError> for Failure {
    fn from(e: ControlError) -> Self {
        match e {
            ControlError::Unknown(u) => Self::Refused { error: u.to_string(), unknown_ref: Some(u.unknown_ref) },
            ControlError::Empty(e) => Self::Caller(e.to_string()),
            other => Self::refused(other),
        }
    }
}

impl From<SendError> for Failure {
    fn from(e: SendError) -> Self {
        match e {
            SendError::Empty(e) => Self::Caller(e.to_string()),
            other => Self::refused(other),
        }
    }
}

/// The verb's answer with every failure an `ok: false` reply. An unknown ref carries
/// `unknown_ref` on every verb, and on `send` also `code: "gone"`.
fn guarded(verb: &str, args: &Value, out: Result<Value, Failure>) -> Result<Value, VerbError> {
    let pid = args.get("pid").cloned().unwrap_or(Value::Null);
    match out {
        Ok(v) => {
            if v.get("ok") == Some(&Value::Bool(false)) {
                let why = v.get("reason").filter(|r| truthy(Some(r))).or_else(|| v.get("error")).cloned().unwrap_or(Value::Null);
                tracing::info!("cch: {verb} refused for pid {pid}: {why}");
            }
            Ok(v)
        }
        Err(Failure::Refused { error, unknown_ref: Some(unknown) }) => {
            tracing::info!("cch: {verb}: {error}");
            let mut out = json!({"ok": false, "error": error, "unknown_ref": unknown});
            if verb == "send" {
                out["code"] = json!("gone");
            }
            Ok(out)
        }
        Err(Failure::Refused { error, unknown_ref: None }) => {
            tracing::warn!("cch: {verb} failed for pid {pid}: {error}");
            Ok(contract::refusal(error))
        }
        Err(Failure::Caller(m)) => Err(VerbError::value(m)),
    }
}

// --- probe -------------------------------------------------------------------

fn clipped(text: &str) -> String {
    text.chars().take(300).collect()
}

/// Whether cch on `node` answers `list`, seen from `here`. `here` lists itself through `local`,
/// never through the gateway; another node through `peer`. No session rows travel back.
pub async fn probe<L, P>(
    node: &str,
    here: &str,
    local: impl FnOnce() -> L,
    peer: impl FnOnce(PeerCall) -> P,
) -> Value
where
    L: Future<Output = Result<Value, String>>,
    P: Future<Output = Result<Value, String>>,
{
    let reply = if node == here {
        local().await
    } else {
        let call = PeerCall { node: node.into(), tool: TOOL, verb: "list", args: json!({}), timeout: PROBE_TIMEOUT };
        peer(call).await
    };
    let reply = match reply {
        Err(e) => return json!({"ok": true, "node": node, "from": here, "answers": false, "reason": clipped(&e)}),
        Ok(r) => r,
    };
    if !reply.is_object() || reply.get("sessions").is_none() {
        let reason = reply.get("reason").filter(|r| truthy(Some(r))).map(|r| text_or_empty(Some(r)));
        let reason = format!("refused: {}", reason.unwrap_or_else(|| reply.to_string()));
        return json!({"ok": true, "node": node, "from": here, "answers": false, "reason": clipped(&reason)});
    }
    let daemon = reply.get("daemon").filter(|d| d.is_object()).and_then(|d| d.get("state")).cloned();
    json!({"ok": true, "node": node, "from": here, "answers": true, "daemon": daemon})
}

// --- the verbs ---------------------------------------------------------------

/// The verb table over its effects.
pub struct App<B> {
    pub backend: B,
}

impl<B: Backend> App<B> {
    pub fn new(backend: B) -> Self {
        Self { backend }
    }

    /// Run one verb. `as_` is the principal the gateway stamped, `peer:<node>` from another node.
    pub async fn handle(&self, verb: &str, args: &Value, as_: Option<&str>) -> Result<Value, VerbError> {
        let out = match verb {
            "platform" => Ok(contract::platform(PLATFORM, NAME, &OPTIONAL_VERBS)),
            "resolve" => self.resolve(args).await,
            "find" => self.find(args).await,
            "status" => self.status(args).await,
            "send" => self.send(args).await,
            "interrupt" => self.control(args, Op::Interrupt).await,
            "stop" => self.stop(args).await,
            "set" => self.set(args).await,
            "compact" => self.compact(args).await,
            "mode" => self.mode(args).await,
            "remote_control" => {
                let op = Op::RemoteControl { on: flag(args, "on", false), name: opt_text(args.get("name")) };
                self.control(args, op).await
            }
            "mcp" => {
                let op =
                    Op::Mcp { action: text_or_empty(args.get("action")), server: text_or_empty(args.get("server")) };
                self.control(args, op).await
            }
            "link" => self.control(args, Op::Link).await,
            "pending" => Ok(self.pending().await),
            "probe" => Ok(self.probe(args, as_).await),
            "live_session_ids" => Ok(self.live_session_ids().await),
            "list" => Ok(self.list(args, as_).await),
            v if PART_TWO.contains(&v) => Ok(not_yet_ported(v)),
            other => return Err(VerbError::value(format!("{TOOL} has no verb {other}"))),
        };
        guarded(verb, args, out)
    }

    async fn control(&self, args: &Value, op: Op) -> Result<Value, Failure> {
        let job = session_ref(args)?;
        Ok(self.backend.control(op, job, pid_arg(args)).await?)
    }

    async fn resolve(&self, args: &Value) -> Result<Value, Failure> {
        let pid = pid_int(args);
        let who = match self.backend.describe_caller(pid.and_then(|p| u32::try_from(p).ok())).await {
            Ok(who) => who,
            Err(LaneError::Resolve(e)) => {
                let mode = self.backend.mode_of(pid).await;
                return Ok(json!({"ok": true, "mine": false, "error": e.to_string(), "mode": mode.as_wire()}));
            }
            Err(e) => return Err(e.into()),
        };
        let at = who.get("pid").and_then(Value::as_u64).and_then(|p| u32::try_from(p).ok()).unwrap_or_default();
        let home = self.backend.home().clone();
        let cwd = transcript::session_and_cwd_for(&home, at).await.1;
        let (job, name, proc_start) = blocking(move || {
            let job = records::job_of_pid(&home, at);
            let name = job.as_deref().and_then(|j| job_name(&home, j));
            (job, name, procfs::start_ticks(at))
        })
        .await;
        let lane = who.get("hosting").cloned().unwrap_or(Value::Null);
        let hosting = match lane.as_str() {
            Some("tmux") => json!("terminal"),
            Some("daemon") => json!("background"),
            _ => lane.clone(),
        };
        let mode = self.backend.mode_of(pid).await;
        Ok(with_ids(json!({
            "ok": true,
            "mine": true,
            "name": name.map_or_else(|| who.get("session").cloned().unwrap_or(Value::Null), Value::from),
            "session_id": who.get("session_id"),
            "pid": at,
            "lane": lane,
            "hosting": hosting,
            "pane": who.get("pane"),
            "job": job,
            "proc_start": proc_start,
            "cwd": cwd,
            "mode": mode.as_wire(),
        })))
    }

    async fn find(&self, args: &Value) -> Result<Value, Failure> {
        let job = session_ref(args)?;
        match self.backend.resolve(job, None).await {
            Ok(t) => Ok(json!({"ok": true, "matches": [row(&t)]})),
            Err(ControlError::Unknown(_)) => Ok(json!({"ok": true, "matches": []})),
            Err(e) => Err(e.into()),
        }
    }

    async fn status(&self, args: &Value) -> Result<Value, Failure> {
        let t = self.backend.resolve(session_ref(args)?, pid_arg(args)).await?;
        let home = self.backend.home();
        let (state, cwd) = if t.alive {
            let status = watcher::read_status(home, t.pid).await.map(|(s, _)| s);
            (status.unwrap_or_else(|| "unknown".into()), transcript::session_and_cwd_for(home, t.pid).await.1)
        } else {
            ("stopped".into(), None)
        };
        let mut out = json!({"ok": true});
        if let (Some(o), Value::Object(r)) = (out.as_object_mut(), row(&t)) {
            o.extend(r);
            o.insert("state".into(), json!(state));
            o.insert("cwd".into(), json!(cwd));
        }
        Ok(out)
    }

    async fn send(&self, args: &Value) -> Result<Value, Failure> {
        let (job, pid) = (session_ref(args)?, pid_arg(args));
        let text = match args.get("text") {
            Some(Value::String(s)) => s.clone(),
            other => text_or_empty(other),
        };
        let confirm = flag(args, "confirm", false);
        if job.is_some() {
            return Ok(self.backend.control(Op::Message { text, confirm }, job, pid).await?);
        }
        let req = SendRequest {
            enter: flag(args, "submit", true),
            delay_ms: delay_ms(args),
            confirm,
            followup: args.get("followup").and_then(Value::as_str).map(str::to_string),
            ..SendRequest::new(&text, pid)
        };
        Ok(self.backend.send(req).await?)
    }

    async fn stop(&self, args: &Value) -> Result<Value, Failure> {
        let (job, pid) = (session_ref(args)?, pid_arg(args));
        if flag(args, "keep", false) {
            return Ok(not_yet_ported("stop with keep"));
        }
        let t = self.backend.resolve(job.clone(), pid).await.ok();
        let mut out = self.backend.control(Op::Stop { confirm: flag(args, "confirm", false) }, job, pid).await?;
        // reflection wakes the sessions that wait on this one by its id (sessions.md R51).
        if let Some(sid) = t.and_then(|t| t.session_id).filter(|s| !s.is_empty()) {
            if out.is_object() && out.get("session_id").is_none() {
                out["session_id"] = json!(r9(&sid));
            }
        }
        Ok(out)
    }

    async fn set(&self, args: &Value) -> Result<Value, Failure> {
        let (job, pid) = (session_ref(args)?, pid_arg(args));
        let asked: Vec<(&str, String)> = ["model", "effort", "name", "cwd"]
            .into_iter()
            .filter(|k| truthy(args.get(*k)))
            .map(|k| (k, text_or_empty(args.get(k))))
            .collect();
        if asked.is_empty() {
            return Ok(json!({"ok": false, "error": "set needs one of model, effort, name or cwd"}));
        }
        let brief = text_or_empty(args.get("brief"));
        let mut applied = Map::new();
        for (key, value) in &asked {
            let op = match *key {
                "model" => Op::Model(value.clone()),
                "effort" => Op::Effort(value.clone()),
                "name" => Op::Rename(value.clone()),
                _ => Op::Cd { path: value.clone(), brief: brief.clone() },
            };
            applied.insert((*key).into(), self.backend.control(op, job.clone(), pid).await?);
        }
        if let [(only, _)] = asked.as_slice() {
            let mut out = applied[*only].clone();
            if out.is_object() {
                out["applied"] = Value::Object(applied);
            }
            return Ok(out);
        }
        let ok = applied.values().all(|r| truthy(r.get("ok")));
        Ok(json!({"ok": ok, "applied": applied}))
    }

    async fn compact(&self, args: &Value) -> Result<Value, Failure> {
        if session_ref(args)?.is_some() {
            return Ok(contract::unsupported("cch compacts only the caller's own session"));
        }
        let req = SendRequest {
            delay_ms: delay_ms(args),
            followup: args.get("followup").and_then(Value::as_str).map(str::to_string),
            ..SendRequest::new("/compact", pid_arg(args))
        };
        Ok(self.backend.send(req).await?)
    }

    async fn mode(&self, args: &Value) -> Result<Value, Failure> {
        let out = self.backend.ensure_bypass(pid_arg(args), opt_text(args.get("expect_session"))).await?;
        Ok(serde_json::to_value(out).unwrap_or_default())
    }

    async fn pending(&self) -> Value {
        let now = watcher::now_ms();
        let rows: Vec<Value> = self
            .backend
            .pending()
            .await
            .into_iter()
            .map(|p| {
                json!({
                    "session": p.name.clone().filter(|n| !n.is_empty()).unwrap_or_else(|| p.session_id.clone()),
                    "session_id": if p.session_id.is_empty() { String::new() } else { r9(&p.session_id) },
                    "pid": p.repl_pid,
                    "hosting": p.hosting,
                    "command": p.text,
                    "resume": p.followup,
                    "ticket": p.ticket,
                    "waiting_for_s": (now - p.injected_at_ms).max(0) / 1000,
                    "holds": p.holds,
                    "nudges": p.nudges,
                    "last_outcome": p.last_outcome,
                    "phase": p.phase,
                    "reissues": p.reissues,
                    "state": "pending",
                })
            })
            .collect();
        json!({"ok": true, "pending": rows})
    }

    async fn list(&self, _args: &Value, _as: Option<&str>) -> Value {
        not_yet_ported("list")
    }

    async fn probe(&self, args: &Value, as_: Option<&str>) -> Value {
        if let Some(reason) = self.backend.caller_refusal(as_.map(str::to_string)).await {
            return json!({"ok": false, "reason": reason});
        }
        let node = text_or_empty(args.get("node")).trim().to_string();
        if node.is_empty() {
            return json!({"ok": false, "reason": "probe needs a node"});
        }
        let here = self.backend.node_name();
        let local = || async { Ok(self.list(&json!({}), None).await) };
        probe(&node, &here, local, |call| self.backend.call_peer(call)).await
    }

    async fn live_session_ids(&self) -> Value {
        let home = self.backend.home().clone();
        match blocking(move || live::live_session_ids(&home)).await {
            Ok(ids) => json!({"ok": true, "session_ids": ids}),
            Err(e) => {
                tracing::error!("cch: live_session_ids: {e}");
                json!({"ok": false, "reason": format!("unreadable {}: {e}", e.path.display())})
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::path::Path;
    use std::sync::Mutex;

    use tempfile::TempDir;

    use super::*;
    use crate::control::{SessionKind, UnknownSession};
    use crate::lineage::Mode;
    use crate::paths::Paths;
    use crate::permission_mode::ModeResult;
    use crate::promises::Pending;

    const SID: &str = "00000000-0000-4000-8000-000000000001";
    const R9: &str = "cc00000000000040008000000000000001";
    const JOB: &str = "abcd1234";

    type Caller = Box<dyn Fn(Option<u32>) -> Result<Value, LaneError> + Send + Sync>;
    type Answer = Box<dyn Fn(&Op) -> Value + Send + Sync>;
    type Called = (Op, Option<String>, Option<u32>);

    /// The effects as a test scripts them: who the caller is, what a ref resolves to, what each
    /// control verb answers, and a record of every call.
    struct Fake {
        _dir: TempDir,
        paths: Paths,
        caller: Caller,
        modes: HashMap<i64, Mode>,
        target: Option<Target>,
        missing: bool,
        answer: Answer,
        refusal: Option<String>,
        ops: Mutex<Vec<Called>>,
        sends: Mutex<Vec<SendRequest>>,
        principals: Mutex<Vec<Option<String>>>,
    }

    impl Fake {
        fn new() -> Self {
            let dir = TempDir::new().unwrap();
            let paths = Paths::under(dir.path());
            Self {
                _dir: dir,
                paths,
                caller: Box::new(|_| Err(ResolveError("no caller".into()).into())),
                modes: HashMap::new(),
                target: None,
                missing: false,
                answer: Box::new(|_| json!({"ok": true})),
                refusal: None,
                ops: Mutex::default(),
                sends: Mutex::default(),
                principals: Mutex::default(),
            }
        }

        fn write(path: &Path, value: Value) {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, value.to_string()).unwrap();
        }

        fn record(&self, pid: u32, record: Value) {
            Self::write(&self.paths.home.session_record(pid), record);
        }

        fn ops(&self) -> Vec<Called> {
            self.ops.lock().unwrap().clone()
        }
    }

    impl Backend for Fake {
        fn home(&self) -> &ClaudeHome {
            &self.paths.home
        }

        async fn describe_caller(&self, pid: Option<u32>) -> Result<Value, LaneError> {
            (self.caller)(pid)
        }

        async fn mode_of(&self, pid: Option<i64>) -> Mode {
            pid.and_then(|p| self.modes.get(&p).cloned()).unwrap_or(Mode::Ungated)
        }

        async fn resolve(&self, job: Option<String>, _pid: Option<u32>) -> Result<Target, ControlError> {
            self.target.clone().ok_or_else(|| UnknownSession { unknown_ref: job.unwrap_or_default() }.into())
        }

        async fn control(&self, op: Op, job: Option<String>, pid: Option<u32>) -> Result<Value, ControlError> {
            self.ops.lock().unwrap().push((op.clone(), job.clone(), pid));
            if self.missing {
                return Err(UnknownSession { unknown_ref: job.unwrap_or_default() }.into());
            }
            Ok((self.answer)(&op))
        }

        async fn send(&self, req: SendRequest) -> Result<Value, SendError> {
            self.sends.lock().unwrap().push(req);
            Ok(json!({"ok": true}))
        }

        async fn ensure_bypass(&self, _pid: Option<u32>, _expect: Option<String>) -> Result<ModeResult, LaneError> {
            Err(ResolveError("no screen in a test".into()).into())
        }

        async fn pending(&self) -> Vec<Pending> {
            vec![]
        }

        async fn caller_refusal(&self, as_: Option<String>) -> Option<String> {
            self.principals.lock().unwrap().push(as_);
            self.refusal.clone()
        }

        fn node_name(&self) -> String {
            "deneb".into()
        }

        async fn call_peer(&self, call: PeerCall) -> Result<Value, String> {
            Err(format!("no peer {} in a test", call.node))
        }
    }

    fn target() -> Target {
        Target {
            pid: 4242,
            kind: SessionKind::Background,
            job: Some(JOB.into()),
            name: Some("me".into()),
            alive: true,
            is_self: false,
            session_id: Some(SID.into()),
        }
    }

    async fn run(app: &App<Fake>, verb: &str, args: Value) -> Value {
        app.handle(verb, &args, None).await.unwrap()
    }

    fn not_ported(out: &Value) -> bool {
        out["ok"] == false && out["code"] == "not_yet_ported"
    }

    // --- tests/test_cch_hub.py ---

    #[tokio::test]
    async fn test_platform_names_claude_code_and_the_contract() {
        let out = run(&App::new(Fake::new()), "platform", json!({})).await;
        assert_eq!((&out["code"], &out["contract"]), (&json!("cc"), &json!("harness@1")));
        assert!(out["capabilities"].as_array().unwrap().contains(&json!("revive_pinned")));
    }

    #[tokio::test]
    async fn test_resolve_answers_mine_false_for_a_pid_that_is_no_session() {
        let fake = Fake { caller: Box::new(|_| Err(ResolveError("no record".into()).into())), ..Fake::new() };
        let out = run(&App::new(fake), "resolve", json!({"pid": 7})).await;
        assert_eq!((&out["ok"], &out["mine"], &out["error"]), (&json!(true), &json!(false), &json!("no record")));
        assert!(out.get("mode").is_some());
    }

    #[tokio::test]
    async fn test_resolve_gives_the_r9_id_and_the_raw_one() {
        let me = std::process::id();
        let fake = Fake {
            caller: Box::new(|pid| Ok(json!({"session": "me", "session_id": SID, "pid": pid, "hosting": "daemon"}))),
            ..Fake::new()
        };
        fake.record(me, json!({"sessionId": SID, "cwd": "/w/projects/awm/x", "kind": "bg", "jobId": JOB}));
        let out = run(&App::new(fake), "resolve", json!({"pid": me})).await;
        assert_eq!((&out["session_id"], &out["claude_session_id"]), (&json!(R9), &json!(SID)));
        assert_eq!(
            (&out["hosting"], &out["job"], &out["cwd"]),
            (&json!("background"), &json!(JOB), &json!("/w/projects/awm/x"))
        );
    }

    #[tokio::test]
    async fn test_resolve_names_a_job_by_its_state_file_not_its_seed_name() {
        let me = std::process::id();
        let fake = Fake {
            caller: Box::new(|pid| {
                Ok(json!({"session": "stale seed name", "session_id": SID, "pid": pid, "hosting": "daemon"}))
            }),
            ..Fake::new()
        };
        fake.record(me, json!({"sessionId": SID, "kind": "bg", "jobId": JOB}));
        Fake::write(&fake.paths.home.job_state(JOB), json!({"name": "made-up job name"}));
        let out = run(&App::new(fake), "resolve", json!({"pid": me})).await;
        assert_eq!(out["name"], "made-up job name");
    }

    #[tokio::test]
    async fn test_an_r9_id_reaches_control_as_the_session_uuid() {
        let app = App::new(Fake::new());
        run(&app, "interrupt", json!({"session_id": R9, "pid": 9})).await;
        assert_eq!(app.backend.ops(), [(Op::Interrupt, Some(SID.to_string()), Some(9))]);
    }

    #[tokio::test]
    async fn test_another_platforms_id_is_refused() {
        let out = run(&App::new(Fake::new()), "interrupt", json!({"session_id": format!("ds{}", "a".repeat(32))})).await;
        assert_eq!(out["ok"], false);
        assert!(out["error"].as_str().unwrap().contains("not a Claude Code session id"), "{out}");
    }

    #[tokio::test]
    async fn test_set_with_one_key_answers_as_that_verb() {
        let answer: Answer = Box::new(|op| match op {
            Op::Model(v) => json!({"ok": true, "model": v}),
            _ => json!({"ok": true}),
        });
        let out = run(&App::new(Fake { answer, ..Fake::new() }), "set", json!({"pid": 1, "model": "opus"})).await;
        assert!(out["ok"] == true && out["model"] == "opus" && out["applied"].get("model").is_some(), "{out}");
    }

    /// Part 2: a kept stop goes through `stop_keeping` and the lifecycle.
    #[tokio::test]
    async fn test_stop_with_keep_keeps_the_job_and_stops_the_caller_later() {
        let app = App::new(Fake { target: Some(Target { is_self: true, ..target() }), ..Fake::new() });
        let out = run(&app, "stop", json!({"ref": JOB, "pid": 4242, "confirm": true, "keep": true})).await;
        assert!(not_ported(&out), "{out}");
        assert!(app.backend.ops().is_empty());
    }

    /// Part 2: a kept stop of a job with lineage goes through the lifecycle.
    #[tokio::test]
    async fn test_a_kept_stop_of_a_started_job_goes_through_its_lineage() {
        let app = App::new(Fake { target: Some(target()), ..Fake::new() });
        let out = run(&app, "stop", json!({"ref": JOB, "pid": 9, "keep": true})).await;
        assert!(not_ported(&out), "{out}");
    }

    #[tokio::test]
    async fn test_stop_without_keep_removes_the_job() {
        let app = App::new(Fake::new());
        let out = run(&app, "stop", json!({"ref": JOB, "keep": false})).await;
        assert_eq!(out["ok"], true);
        assert_eq!(app.backend.ops(), [(Op::Stop { confirm: false }, Some(JOB.to_string()), None)]);
    }

    #[tokio::test]
    async fn test_stop_removes_the_job_by_default_and_names_the_session() {
        let sid = "00000000-0000-4000-8000-000000000002";
        let app = App::new(Fake { target: Some(Target { session_id: Some(sid.into()), ..target() }), ..Fake::new() });
        let out = run(&app, "stop", json!({"ref": JOB})).await;
        assert_eq!(app.backend.ops(), [(Op::Stop { confirm: false }, Some(JOB.to_string()), None)]);
        assert_eq!(out["session_id"], "cc00000000000040008000000000000002");
    }

    /// Part 2: `stop_keeping` and its lineage record.
    #[tokio::test]
    async fn test_a_job_that_already_stopped_counts_as_stopped() {
        let out = run(&App::new(Fake::new()), "stop", json!({"ref": JOB, "keep": true})).await;
        assert!(not_ported(&out), "{out}");
    }

    /// Part 2: `resume` and `adopt_lineage` go through the lifecycle.
    #[tokio::test]
    async fn test_resume_can_adopt_a_job_with_no_lineage() {
        let out = run(&App::new(Fake::new()), "resume", json!({"ref": JOB, "adopt_lineage": true})).await;
        assert!(not_ported(&out), "{out}");
    }

    /// Part 2: the pins and the lifecycle.
    #[tokio::test]
    async fn test_revive_pinned_resumes_only_the_dead_pins() {
        let out = run(&App::new(Fake::new()), "revive_pinned", json!({})).await;
        assert!(not_ported(&out), "{out}");
    }

    #[tokio::test]
    async fn test_compact_refuses_another_session() {
        let app = App::new(Fake::new());
        let out = run(&app, "compact", json!({"ref": JOB})).await;
        assert_eq!(out["unsupported"], true);
        assert!(app.backend.sends.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_resolve_reports_the_mode_for_the_gateways_gate() {
        let fake = Fake {
            modes: HashMap::from([(77, Mode::Declared("reader".into()))]),
            caller: Box::new(|_| Err(ResolveError("not a session".into()).into())),
            ..Fake::new()
        };
        let out = run(&App::new(fake), "resolve", json!({"pid": 77})).await;
        assert_eq!((&out["mine"], &out["mode"]), (&json!(false), &json!("reader")));
    }

    #[tokio::test]
    async fn an_unknown_ref_names_itself_and_a_send_to_it_is_gone() {
        let app = App::new(Fake { missing: true, ..Fake::new() });
        let out = run(&app, "interrupt", json!({"ref": "nobody"})).await;
        assert_eq!((&out["ok"], &out["unknown_ref"]), (&json!(false), &json!("nobody")));
        assert!(out.get("code").is_none());
        let out = run(&app, "send", json!({"ref": "nobody", "text": "made-up text"})).await;
        assert_eq!((&out["unknown_ref"], &out["code"]), (&json!("nobody"), &json!("gone")));
    }

    #[tokio::test]
    async fn a_send_with_no_ref_types_into_the_caller_with_its_options() {
        let app = App::new(Fake::new());
        let args = json!({"pid": "5", "text": "/compact", "submit": "false", "delay_ms": "300", "followup": "next"});
        run(&app, "send", args).await;
        let sent = app.backend.sends.lock().unwrap().clone();
        let want = SendRequest {
            enter: false,
            delay_ms: 300,
            followup: Some("next".into()),
            ..SendRequest::new("/compact", Some(5))
        };
        assert_eq!(sent, [want]);
        assert!(app.backend.ops().is_empty());
    }

    // --- tests/test_manifest_surface.py, the halves that need a handler ---

    #[tokio::test]
    async fn test_every_declared_function_has_a_handler() {
        let app = App::new(Fake::new());
        for s in specs() {
            if let Err(e) = app.handle(s.verb, &json!({}), None).await {
                assert!(!e.message.contains("has no verb"), "{}: {}", s.verb, e.message);
            }
        }
    }

    /// Every handler takes the principal; `probe`, the one part 1 serves, hands it to the gate.
    #[tokio::test]
    async fn test_a_handler_that_takes_the_principal_still_gets_it() {
        let app = App::new(Fake::new());
        app.handle("probe", &json!({"node": "altair"}), Some("peer:altair")).await.unwrap();
        assert_eq!(*app.backend.principals.lock().unwrap(), [Some("peer:altair".to_string())]);
    }

    // --- tests/test_probe.py ---

    fn no_local() -> std::future::Ready<Result<Value, String>> {
        std::future::ready(Err("listed itself".into()))
    }

    #[tokio::test]
    async fn test_a_node_that_lists_answers_and_no_rows_travel() {
        let peer = |call: PeerCall| {
            assert_eq!((call.node.as_str(), call.tool, call.verb), ("altair", "harness-cc", "list"));
            assert!(call.timeout < Duration::from_secs(30));
            std::future::ready(Ok(json!({"sessions": [{"job": "secret"}], "daemon": {"state": "up"}})))
        };
        let out = probe("altair", "deneb", no_local, peer).await;
        assert_eq!(out, json!({"ok": true, "node": "altair", "from": "deneb", "answers": true, "daemon": "up"}));
    }

    #[tokio::test]
    async fn test_a_node_that_does_not_answer() {
        let out = probe("altair", "deneb", no_local, |_| std::future::ready(Err("timed out".into()))).await;
        assert_eq!(out["answers"], false);
        assert!(out["reason"].as_str().unwrap().contains("timed out"));
    }

    #[tokio::test]
    async fn test_a_refused_list_does_not_count_as_an_answer() {
        let refused = |_| std::future::ready(Ok(json!({"ok": false, "reason": "nope"})));
        assert_eq!(probe("altair", "deneb", no_local, refused).await["answers"], false);
    }

    #[tokio::test]
    async fn test_the_probing_node_lists_itself_locally() {
        let local = || std::future::ready(Ok(json!({"sessions": [], "daemon": {"state": "down"}})));
        let out = probe("deneb", "deneb", local, |_| std::future::ready(Err("went through the gateway".into()))).await;
        assert_eq!(out["daemon"], "down");
    }

    #[tokio::test]
    async fn test_a_foreign_caller_is_refused() {
        let app = App::new(Fake { refusal: Some("foreign".into()), ..Fake::new() });
        let out = app.handle("probe", &json!({"node": "altair"}), Some("peer:x")).await.unwrap();
        assert_eq!(out, json!({"ok": false, "reason": "foreign"}));
    }

    #[tokio::test]
    async fn test_a_probe_needs_a_node() {
        assert_eq!(run(&App::new(Fake::new()), "probe", json!({})).await["ok"], false);
    }

    // --- tests/test_live_sessions.py, at the verb ---

    #[tokio::test]
    async fn test_a_torn_roster_stops_the_sweep_rather_than_emptying_the_list() {
        let fake = Fake::new();
        let roster = fake.paths.home.roster();
        std::fs::create_dir_all(roster.parent().unwrap()).unwrap();
        std::fs::write(&roster, r#"{"workers": "#).unwrap();
        let out = run(&App::new(fake), "live_session_ids", json!({})).await;
        assert_eq!(out["ok"], false);
        assert!(out["reason"].as_str().unwrap().contains(&roster.display().to_string()), "{out}");
    }

    #[tokio::test]
    async fn test_a_corrupt_job_record_is_named_in_the_error_and_the_log() {
        let fake = Fake::new();
        let bad = fake.paths.home.job_state("j1");
        std::fs::create_dir_all(bad.parent().unwrap()).unwrap();
        std::fs::write(&bad, "{torn").unwrap();
        let out = run(&App::new(fake), "live_session_ids", json!({})).await;
        assert_eq!(out["ok"], false);
        assert!(out["reason"].as_str().unwrap().contains(&bad.display().to_string()), "{out}");
    }
}
