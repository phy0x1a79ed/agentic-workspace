//! The five Claude Code hooks, run as `cch hook <name>`: blocking work with no tokio runtime, and
//! an exit 0 on every path, a panic included, so a hook never costs a session a turn.

use std::fs::{self, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use awm_harness::claude::ClaudeHome;
use serde_json::{json, Map, Value};

use crate::ledger::{self, Stage};
use crate::paths::Paths;

/// The hub a hook calls when `AWM_HUB_URL` is unset.
pub const DEFAULT_HUB_URL: &str = "http://127.0.0.1:7819";

/// The brief's journal call gets 4 s of the SessionStart hook's 6.
pub const BRIEF_TIMEOUT: Duration = Duration::from_millis(4000);
/// The clear's report shares the hook's 6-second budget with the brief.
pub const CLEAR_TIMEOUT: Duration = Duration::from_millis(1500);

/// The tool whose approval the plan-mode hook acts on.
pub const PLAN_TOOL: &str = "ExitPlanMode";
/// The TUI is still repainting the moment the tool returns, and the mode is read off that paint.
/// A beat here is cheaper than a retry later.
pub const SETTLE_S: f64 = 0.6;
/// Every attempt fits inside the settings entry's 20 s timeout, or Claude Code kills the hook
/// mid-POST.
pub const HTTP_TIMEOUT_S: f64 = 15.0;
/// The whole hook's budget, retries included.
pub const DEADLINE_S: f64 = 17.0;
/// The pause between two attempts.
pub const RETRY_GAP_S: f64 = 1.5;
/// No attempt starts with less than this left.
pub const MIN_ATTEMPT_S: f64 = 3.0;

/// What vendor Claude Code truncates an intent to.
pub const INTENT_MAX: usize = 500;

/// The most subagents a session below trusted runs at a time (channels.md R125 item 7).
pub const CAP: usize = 5;
/// A reservation whose subagent never starts, because another hook refused the call, lapses.
pub const RESERVE_S: f64 = 60.0;
/// A running subagent whose stop never comes, because the session was killed, lapses.
pub const RUNNING_S: f64 = 4.0 * 3600.0;
/// The trust lookup's timeout.
pub const WHOAMI_TIMEOUT: Duration = Duration::from_millis(3000);
/// The tools that start a subagent.
pub const AGENT_TOOLS: [&str; 2] = ["Agent", "Task"];

/// Why a gateway call gave no answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PostError {
    /// The call ran out of time: it may still be running on the server.
    Timeout,
    /// The gateway answered with this non-2xx status.
    Status(u16),
    /// Nothing reached the gateway, or its answer was not JSON.
    Failed(String),
}

impl std::fmt::Display for PostError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Timeout => f.write_str("timed out"),
            Self::Status(code) => write!(f, "HTTP {code}"),
            Self::Failed(why) => f.write_str(why),
        }
    }
}

/// The gateway's flat `/invoke` surface. The CLI sends no headers, so the gateway would strip
/// `_caller_pid` and every caller-scoped call would refuse.
pub trait Hub {
    /// POSTs `{name, args}` with `X-Awm-Caller-Pid: caller_pid`, and answers the response body.
    fn invoke(&self, name: &str, args: &Value, caller_pid: u32, timeout: Duration) -> Result<Value, PostError>;
}

/// [`Hub`] over a raw HTTP/1.1 POST on a blocking `TcpStream`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpHub {
    base: String,
}

impl HttpHub {
    /// The hub at `url`, a plain `http://` URL.
    pub fn new(url: &str) -> Self {
        Self { base: url.trim_end_matches('/').to_string() }
    }

    /// The hub `AWM_HUB_URL` names, else [`DEFAULT_HUB_URL`].
    pub fn from_env() -> Self {
        Self::new(&std::env::var("AWM_HUB_URL").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| DEFAULT_HUB_URL.into()))
    }
}

fn io_error(e: io::Error) -> PostError {
    match e.kind() {
        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => PostError::Timeout,
        _ => PostError::Failed(e.to_string()),
    }
}

fn remaining(deadline: Instant) -> Result<Duration, PostError> {
    deadline.checked_duration_since(Instant::now()).filter(|d| !d.is_zero()).ok_or(PostError::Timeout)
}

fn connect(authority: &str, deadline: Instant) -> Result<TcpStream, PostError> {
    let has_port = match authority.rfind(']') {
        Some(end) => authority[end..].contains(':'),
        None => authority.contains(':'),
    };
    let target = if has_port { authority.to_string() } else { format!("{authority}:80") };
    let addrs = target.to_socket_addrs().map_err(|e| PostError::Failed(e.to_string()))?;
    let mut last = PostError::Failed(format!("{authority} resolves to no address"));
    for addr in addrs {
        match TcpStream::connect_timeout(&addr, remaining(deadline)?) {
            Ok(stream) => return Ok(stream),
            Err(e) => last = io_error(e),
        }
    }
    Err(last)
}

fn dechunk(mut body: &[u8]) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    loop {
        let end = body.windows(2).position(|w| w == b"\r\n")?;
        let size = std::str::from_utf8(&body[..end]).ok()?.split(';').next()?.trim();
        let size = usize::from_str_radix(size, 16).ok()?;
        body = &body[end + 2..];
        if size == 0 {
            return Some(out);
        }
        out.extend_from_slice(body.get(..size)?);
        body = body.get(size + 2..)?;
    }
}

fn parse_response(raw: &[u8]) -> Result<Value, PostError> {
    let bad = || PostError::Failed("the gateway's answer is not HTTP".into());
    let split = raw.windows(4).position(|w| w == b"\r\n\r\n").ok_or_else(bad)?;
    let head = std::str::from_utf8(&raw[..split]).map_err(|_| bad())?;
    let mut lines = head.split("\r\n");
    let status: u16 = lines.next().and_then(|l| l.split_whitespace().nth(1)).and_then(|c| c.parse().ok()).ok_or_else(bad)?;
    if !(200..300).contains(&status) {
        return Err(PostError::Status(status));
    }
    let chunked = lines.any(|l| {
        l.split_once(':')
            .is_some_and(|(k, v)| k.trim().eq_ignore_ascii_case("transfer-encoding") && v.to_ascii_lowercase().contains("chunked"))
    });
    let body = &raw[split + 4..];
    let body = if chunked { dechunk(body).ok_or_else(bad)? } else { body.to_vec() };
    let text = String::from_utf8(body).map_err(|e| PostError::Failed(e.to_string()))?;
    let text = if text.is_empty() { "{}" } else { text.as_str() };
    serde_json::from_str(text).map_err(|e| PostError::Failed(e.to_string()))
}

impl Hub for HttpHub {
    fn invoke(&self, name: &str, args: &Value, caller_pid: u32, timeout: Duration) -> Result<Value, PostError> {
        let deadline = Instant::now() + timeout;
        let rest = self
            .base
            .strip_prefix("http://")
            .ok_or_else(|| PostError::Failed(format!("{} is not an http:// URL", self.base)))?;
        let (authority, prefix) = match rest.split_once('/') {
            Some((a, p)) => (a, format!("/{p}")),
            None => (rest, String::new()),
        };
        let mut stream = connect(authority, deadline)?;
        let body = json!({"name": name, "args": args}).to_string();
        let request = format!(
            "POST {prefix}/invoke HTTP/1.1\r\nHost: {authority}\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\nX-Awm-Caller-Pid: {caller_pid}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream.set_write_timeout(Some(remaining(deadline)?)).map_err(io_error)?;
        stream.write_all(request.as_bytes()).map_err(io_error)?;
        let mut raw = Vec::new();
        let mut buf = [0u8; 8192];
        loop {
            stream.set_read_timeout(Some(remaining(deadline)?)).map_err(io_error)?;
            match stream.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => raw.extend_from_slice(&buf[..n]),
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => return Err(io_error(e)),
            }
        }
        parse_response(&raw)
    }
}

/// The `result` of an `/invoke` answer, parsed once more when the gateway sent it as a string.
pub(crate) fn result_of(payload: &Value) -> Option<Value> {
    match payload.get("result")? {
        Value::String(s) => serde_json::from_str(s).ok(),
        other => Some(other.clone()),
    }
}

/// Python's truthiness of a JSON value.
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

fn parent_pid() -> u32 {
    // SAFETY: getppid has no preconditions and cannot fail.
    u32::try_from(unsafe { libc::getppid() }).unwrap_or(0)
}

fn object(input: &[u8]) -> Option<Map<String, Value>> {
    match serde_json::from_slice(input).ok()? {
        Value::Object(m) => Some(m),
        _ => None,
    }
}

fn state_home() -> PathBuf {
    std::env::var_os("XDG_STATE_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_else(|| "/".into())).join(".local").join("state"))
}

/// `(project, scope)` of the git worktree under a `projects/` folder that holds `cwd`, or None.
pub fn scope_of(cwd: &str) -> Option<(String, String)> {
    let path = fs::canonicalize(cwd).unwrap_or_else(|_| PathBuf::from(cwd));
    let text = path.to_string_lossy().into_owned();
    let parts: Vec<&str> = text.split('/').collect();
    let root = parts.iter().rposition(|p| *p == "projects")? + 1;
    let mut here = path;
    loop {
        let here_text = here.to_string_lossy().into_owned();
        let rel: Vec<&str> = here_text.split('/').skip(root).collect();
        if rel.len() < 2 || rel[0].starts_with('.') {
            return None;
        }
        if here.join(".git").exists() {
            return Some((rel[0].to_string(), rel[1..].join("/")));
        }
        here = here.parent()?.to_path_buf();
    }
}

/// The brief line journal gives the scope, or empty when it gives none or does not answer.
pub fn brief_line(hub: &dyn Hub, project: &str, scope: &str) -> Value {
    let args = json!({"project": project, "scope": scope});
    let result = hub.invoke("journal_brief_read", &args, parent_pid(), BRIEF_TIMEOUT).ok().and_then(|p| result_of(&p));
    match result.as_ref().and_then(|r| r.get("brief")) {
        Some(Value::Object(got)) if truthy(got.get("line")) => got["line"].clone(),
        _ => Value::String(String::new()),
    }
}

/// The Claude Code process that ran this hook: the nearest ancestor with a session record.
pub fn claude_pid(records: &Path) -> Option<u32> {
    nearest_with_record(records, parent_pid())
}

/// `pid` or the nearest of its first five ancestors with a record in `records`.
fn nearest_with_record(records: &Path, mut pid: u32) -> Option<u32> {
    for _ in 0..6 {
        if pid <= 1 {
            return None;
        }
        if records.join(format!("{pid}.json")).exists() {
            return Some(pid);
        }
        pid = awm_plugin_sdk::procfs::ppid(pid).unwrap_or(0);
    }
    None
}

/// `$XDG_STATE_HOME/awm/cch/session-start`: the last session id each Claude Code process had.
pub fn seen_dir() -> PathBuf {
    state_home().join("awm").join("cch").join("session-start")
}

/// Records `session_id` as the last id of `pid` and answers the one before it. Drops the records
/// of processes that are gone, so the folder stays small.
pub fn swap_seen(folder: &Path, pid: u32, session_id: &str) -> String {
    let path = folder.join(pid.to_string());
    let before = fs::read_to_string(&path).map(|t| t.trim().to_string()).unwrap_or_default();
    let _ = (|| -> io::Result<()> {
        fs::create_dir_all(folder)?;
        fs::write(&path, session_id)?;
        for entry in fs::read_dir(folder)? {
            let name = entry?.file_name().to_string_lossy().into_owned();
            if !name.is_empty() && name.bytes().all(|b| b.is_ascii_digit()) && !Path::new("/proc").join(&name).exists() {
                fs::remove_file(folder.join(&name))?;
            }
        }
        Ok(())
    })();
    before
}

/// `cch hook brief` on a SessionStart payload: what it prints, if anything.
pub fn brief(input: &[u8], hub: &dyn Hub, seen: &Path, claude_pid: impl FnOnce() -> Option<u32>) -> Option<String> {
    let payload = object(input)?;
    let session_id = payload.get("session_id").and_then(Value::as_str).filter(|s| !s.is_empty());
    if let Some(sid) = session_id {
        if let Some(pid) = claude_pid() {
            let before = swap_seen(seen, pid, sid);
            let cleared = payload.get("source").and_then(Value::as_str) == Some("clear") && before != sid;
            if let (true, Some(id), Some(parent)) = (cleared, ledger::key(sid), ledger::key(&before)) {
                let args = json!({"session_id": id, "kind": "clear", "parent": parent});
                let _ = hub.invoke("reflection_harness_event", &args, parent_pid(), CLEAR_TIMEOUT);
                let ops = json!({"session_id": id, "parent": parent});
                let _ = hub.invoke("operations_hook_session_clear", &ops, parent_pid(), CLEAR_TIMEOUT);
            }
        }
    }
    let cwd = payload.get("cwd").and_then(Value::as_str).filter(|s| !s.is_empty())?;
    let (project, scope) = scope_of(cwd)?;
    let line = brief_line(hub, &project, &scope);
    truthy(Some(&line)).then(|| {
        json!({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": line}}).to_string()
    })
}

/// The plan-mode hook's clock: monotonic seconds and a sleep.
pub trait Pace {
    /// Seconds since an arbitrary fixed start.
    fn monotonic(&self) -> f64;
    /// Blocks for `secs`.
    fn sleep(&self, secs: f64);
}

/// [`Pace`] on the real clock.
pub struct RealPace(Instant);

impl Default for RealPace {
    fn default() -> Self {
        Self(Instant::now())
    }
}

impl Pace for RealPace {
    fn monotonic(&self) -> f64 {
        self.0.elapsed().as_secs_f64()
    }

    fn sleep(&self, secs: f64) {
        std::thread::sleep(Duration::try_from_secs_f64(secs).unwrap_or(Duration::ZERO));
    }
}

/// Whether the payload is an approved plan in a session not already in bypass. A rejection would
/// be cycled out of plan mode, worse than doing nothing; `plan` means something unmodelled
/// happened, since ExitPlanMode restores the earlier mode before this runs.
pub fn should_act(payload: &Map<String, Value>) -> bool {
    if payload.get("tool_name").and_then(Value::as_str) != Some(PLAN_TOOL) {
        return false;
    }
    if !payload.get("tool_response").and_then(Value::as_object).is_some_and(|r| r.contains_key("plan")) {
        return false;
    }
    !matches!(payload.get("permission_mode").and_then(Value::as_str), Some("bypassPermissions" | "plan"))
}

/// One `reflection_mode` call. Sends the hook's own pid: the gateway walks its ancestry to the
/// session, and `expect_session` makes reflection refuse a walk that climbed past it.
pub fn call_mode(hub: &dyn Hub, session_id: &str, timeout: f64) -> Result<Value, PostError> {
    let args = json!({"expect_session": session_id});
    let timeout = Duration::try_from_secs_f64(timeout.max(0.0)).unwrap_or(Duration::ZERO);
    let payload = hub.invoke("reflection_mode", &args, std::process::id(), timeout)?;
    let result = payload.get("result").cloned();
    Ok(if truthy(result.as_ref()) { result.unwrap_or_default() } else { json!({}) })
}

/// `~/.claude/awm-reflection-mode.log`: the whole answer to "it silently didn't happen".
pub fn log_path(home: &ClaudeHome) -> PathBuf {
    home.root.join("awm-reflection-mode.log")
}

/// Appends one stamped line to the log at `path`.
pub fn note(path: &Path, message: &str) {
    let stamp = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
    if let Ok(mut fh) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(fh, "{stamp} {message}");
    }
}

/// Calls `reflection_mode` until it switches, within [`DEADLINE_S`]. Retries a gateway that could
/// not be reached and a footer that read `unknown` (a modal, transient after an approval), never a
/// timeout: the walk a timed-out call started is still pressing Shift+Tab, and a second walk on
/// top of it steps past bypass.
pub fn ensure_bypass(
    session_id: &str,
    call: &mut dyn FnMut(&str, f64) -> Result<Value, PostError>,
    pace: &dyn Pace,
    note: &mut dyn FnMut(&str),
) {
    let deadline = pace.monotonic() + DEADLINE_S;
    let mut last: String;
    loop {
        let budget = HTTP_TIMEOUT_S.min(deadline - pace.monotonic());
        match call(session_id, budget) {
            Err(PostError::Status(code)) => last = format!("HTTP {code} from the gateway"),
            Err(PostError::Timeout) => {
                note(&format!(
                    "session {session_id}: the mode call timed out after {budget:.0}s and was not retried, \
                     because its walk may still be running"
                ));
                return;
            }
            Err(PostError::Failed(why)) => last = format!("could not reach the gateway: {why}"),
            Ok(result) => {
                let Some(result) = result.as_object() else { return };
                if truthy(result.get("ok")) {
                    return;
                }
                let why = [result.get("error"), result.get("reason")].into_iter().flatten().find(|v| truthy(Some(v)));
                last = match why {
                    Some(Value::String(s)) => s.clone(),
                    Some(other) => other.to_string(),
                    None => Value::Object(result.clone()).to_string(),
                };
                if result.get("mode").and_then(Value::as_str) != Some("unknown") {
                    break;
                }
            }
        }
        if deadline - pace.monotonic() < RETRY_GAP_S + MIN_ATTEMPT_S {
            break;
        }
        pace.sleep(RETRY_GAP_S);
    }
    note(&format!("session {session_id} not switched to bypass: {last}"));
}

/// `cch hook plan-mode` on a PostToolUse payload. Prints nothing: a PostToolUse hook's stdout is
/// parsed as a JSON control object. Runs synchronously, since detaching would reparent the hook
/// away from the session and break the ancestry walk.
pub fn plan_mode(input: &[u8], pace: &dyn Pace, ensure: impl FnOnce(&str)) -> Option<String> {
    let payload = object(input)?;
    let session_id = payload.get("session_id").and_then(Value::as_str).filter(|s| !s.is_empty())?;
    if !should_act(&payload) {
        return None;
    }
    pace.sleep(SETTLE_S);
    ensure(session_id);
    None
}

/// The part of the prompt the user typed, without injected context.
pub fn spoken(prompt: Option<&Value>) -> String {
    let Some(Value::String(prompt)) = prompt else { return String::new() };
    const TAG: &str = "</system-reminder>";
    let tail = prompt.rfind(TAG).map_or(prompt.as_str(), |cut| &prompt[cut + TAG.len()..]);
    tail.trim().to_string()
}

/// Drops the name and records the intent in the job record at `path`, or leaves it exactly as it
/// is. Read, modify, write with no lock: the session writes the same file, so only the two keys
/// change between the read and the replace.
fn clear_name(path: &Path, intent: &str) {
    let Ok(bytes) = fs::read(path) else { return };
    let Ok(Value::Object(mut state)) = serde_json::from_slice::<Value>(&bytes) else { return };
    state.shift_remove("name");
    state.shift_remove("nameSource");
    // `??`, not `||`, merges this field back on every later write, so an empty string would
    // survive the whole session and the namer would never run.
    state.insert("intent".into(), Value::String(intent.into()));
    let dir = path.parent().unwrap_or(Path::new("."));
    let tmp = dir.join(format!(".state-{}.tmp", uuid::Uuid::new_v4().simple()));
    let written = (|| -> io::Result<()> {
        let mut fh = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&tmp)?;
        fh.write_all(&serde_json::to_vec(&state).map_err(io::Error::other)?)?;
        drop(fh);
        fs::rename(&tmp, path)
    })();
    if written.is_err() {
        let _ = fs::remove_file(&tmp);
    }
}

/// `cch hook name` on a UserPromptSubmit payload. Acts only while the pool ledger at `ledger`
/// says the session is `claimed`, and only on a person's first real request; it then marks the
/// session `kept`, so it fires once.
pub fn name(input: &[u8], job_dir: Option<&Path>, ledger: &Path) -> Option<String> {
    let payload = object(input)?;
    // Only a person at the composer, not a wake-up, a schedule or an SDK caller, titles a session.
    let source = payload.get("source");
    if truthy(source) && source.and_then(Value::as_str) != Some("user") {
        return None;
    }
    let prompt = spoken(payload.get("prompt"));
    // A slash command is not a request; Claude Code's own intent capture skips them too.
    if prompt.is_empty() || prompt.starts_with('/') {
        return None;
    }
    let job = job_dir.filter(|j| !j.as_os_str().is_empty())?;
    let id = ledger::key(payload.get("session_id")?.as_str()?)?;
    if ledger::load(ledger).get(&id) != Some(&Stage::Claimed) {
        return None;
    }
    let intent: String = prompt.chars().take(INTENT_MAX).collect();
    clear_name(&job.join("state.json"), &intent);
    let _ = ledger::set(ledger, &id, Stage::Kept);
    None
}

/// `$XDG_STATE_HOME/awm/cch/subagents`: one `<session id>.json` counter per session.
pub fn subagents_dir() -> PathBuf {
    state_home().join("awm").join("cch").join("subagents")
}

/// The calling session's trust level, or untrusted when reflection cannot say (fleet.md R103
/// item 2).
pub fn level(hub: &dyn Hub) -> String {
    let who = hub.invoke("reflection_whoami", &json!({}), parent_pid(), WHOAMI_TIMEOUT).ok().and_then(|p| result_of(&p));
    match who.as_ref().and_then(|w| w.get("trust")).and_then(Value::as_str) {
        Some(t @ ("trusted" | "cooperative" | "untrusted")) => t.to_string(),
        _ => "untrusted".to_string(),
    }
}

/// One session's subagents: the running ones by agent id with their start, and the reservations.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct CapState {
    /// Agent id to start, in epoch seconds.
    pub running: Map<String, Value>,
    /// When each reservation was made, oldest first.
    pub reserved: Vec<f64>,
}

/// Applies `change` to the session's counter under `flock`, dropping lapsed entries first, and
/// saves it.
fn update<T>(dir: &Path, session_id: &str, now: f64, change: impl FnOnce(&mut CapState, f64) -> T) -> io::Result<T> {
    fs::create_dir_all(dir)?;
    let mut fh = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(dir.join(format!("{session_id}.json")))?;
    // SAFETY: flock on an fd this function owns; the lock drops with the file.
    if unsafe { libc::flock(fh.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let mut text = String::new();
    fh.read_to_string(&mut text)?;
    let old: Value = serde_json::from_str(if text.is_empty() { "{}" } else { &text }).unwrap_or_else(|_| json!({}));
    let running = match old.get("running") {
        Some(Value::Object(r)) => r
            .iter()
            .filter(|(_, t)| t.as_f64().is_some_and(|t| now - t < RUNNING_S))
            .map(|(a, t)| (a.clone(), t.clone()))
            .collect(),
        _ => Map::new(),
    };
    let mut reserved: Vec<f64> = old
        .get("reserved")
        .and_then(Value::as_array)
        .map(|r| r.iter().filter_map(Value::as_f64).filter(|t| now - t < RESERVE_S).collect())
        .unwrap_or_default();
    reserved.sort_by(f64::total_cmp);
    let mut state = CapState { running, reserved };
    let out = change(&mut state, now);
    fh.seek(SeekFrom::Start(0))?;
    fh.set_len(0)?;
    fh.write_all(json!({"running": state.running, "reserved": state.reserved}).to_string().as_bytes())?;
    Ok(out)
}

fn started(state: &mut CapState, agent_id: Option<&str>, now: f64) {
    let id = match agent_id.filter(|a| !a.is_empty()) {
        Some(a) => a.to_string(),
        None => format!("anon-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]),
    };
    state.running.insert(id, json!(now));
    if !state.reserved.is_empty() {
        state.reserved.remove(0);
    }
}

/// A stop with no agent id stands for the oldest running subagent.
fn stopped(state: &mut CapState, agent_id: Option<&str>) {
    match agent_id {
        None => {
            let oldest = state
                .running
                .iter()
                .filter_map(|(a, t)| Some((a, t.as_f64()?)))
                .fold(None::<(&String, f64)>, |min, (a, t)| match min {
                    Some((_, m)) if m <= t => min,
                    _ => Some((a, t)),
                })
                .map(|(a, _)| a.clone());
            if let Some(a) = oldest {
                state.running.shift_remove(&a);
            }
        }
        Some(a) => {
            state.running.shift_remove(a);
        }
    }
}

/// Reserves a slot, or answers why not. The level is asked only when the cap is full.
fn reserve(state: &mut CapState, now: f64, level: impl FnOnce() -> String) -> Option<String> {
    let busy = state.running.len() + state.reserved.len();
    if busy >= CAP {
        let lv = level();
        if lv != "trusted" {
            return Some(format!(
                "A {lv} session runs at most {CAP} subagents at a time, and {busy} are running or starting. \
                 Wait for one to finish (channels.md R125 item 7)."
            ));
        }
    }
    state.reserved.push(now);
    None
}

/// `cch hook subagent-cap` on a PreToolUse, SubagentStart or SubagentStop payload: the deny it
/// prints, if any. `now` is the wall clock in epoch seconds.
pub fn subagent_cap(input: &[u8], dir: &Path, now: impl FnOnce() -> f64, level: impl FnOnce() -> String) -> Option<String> {
    let payload = object(input)?;
    let session_id = payload.get("session_id").and_then(Value::as_str).filter(|s| !s.is_empty() && !s.contains('/'))?;
    let agent_id = payload.get("agent_id").and_then(Value::as_str);
    match payload.get("hook_event_name").and_then(Value::as_str) {
        Some("SubagentStart") => {
            let _ = update(dir, session_id, now(), |s, t| started(s, agent_id, t));
            None
        }
        Some("SubagentStop") => {
            let _ = update(dir, session_id, now(), |s, _| stopped(s, agent_id));
            None
        }
        Some("PreToolUse") if payload.get("tool_name").and_then(Value::as_str).is_some_and(|t| AGENT_TOOLS.contains(&t)) => {
            let why = update(dir, session_id, now(), |s, t| reserve(s, t, level)).ok()??;
            Some(
                json!({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": why}})
                .to_string(),
            )
        }
        _ => None,
    }
}

/// The hook names `cch hook <name>` accepts.
pub const HOOKS: [&str; 5] = ["brief", "plan-mode", "name", "subagent-cap", "report"];

fn epoch_now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// Runs hook `name` on `input` against the real environment, and answers what it prints.
pub fn dispatch(name: &str, input: &[u8]) -> Option<String> {
    match name {
        "brief" => {
            let home = ClaudeHome::from_env();
            brief(input, &HttpHub::from_env(), &seen_dir(), || claude_pid(&home.sessions()))
        }
        "plan-mode" => {
            let pace = RealPace::default();
            let log = log_path(&ClaudeHome::from_env());
            let hub = HttpHub::from_env();
            plan_mode(input, &pace, |sid| {
                ensure_bypass(sid, &mut |s, t| call_mode(&hub, s, t), &pace, &mut |m| note(&log, m))
            })
        }
        "name" => {
            let job = std::env::var_os("CLAUDE_JOB_DIR").map(PathBuf::from);
            self::name(input, job.as_deref(), &Paths::from_env().ledger_path())
        }
        "subagent-cap" => subagent_cap(input, &subagents_dir(), epoch_now, || level(&HttpHub::from_env())),
        "report" => {
            let payload = serde_json::from_slice(input).ok()?;
            crate::hook_report::handle(&HttpHub::from_env(), &ClaudeHome::from_env().root, &payload);
            None
        }
        _ => None,
    }
}

/// `cch hook <name>`: reads the payload from stdin, runs the hook, prints its output. Always
/// answers exit code 0 and prints nothing on failure, a panic included.
pub fn main(name: Option<&str>) -> i32 {
    std::panic::set_hook(Box::new(|_| {}));
    let _ = std::panic::catch_unwind(|| {
        let mut input = Vec::new();
        if io::stdin().read_to_end(&mut input).is_err() {
            return;
        }
        if let Some(out) = name.and_then(|n| dispatch(n, &input)) {
            let mut stdout = io::stdout().lock();
            let _ = writeln!(stdout, "{out}");
            let _ = stdout.flush();
        }
    });
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::net::TcpListener;

    type Answer = Box<dyn Fn(&str, &Value) -> Result<Value, PostError>>;

    /// A [`Hub`] that records each call and answers from `answer`.
    struct FakeHub {
        calls: RefCell<Vec<(String, Value, u32, Duration)>>,
        answer: Answer,
    }

    impl FakeHub {
        fn new(answer: impl Fn(&str, &Value) -> Result<Value, PostError> + 'static) -> Self {
            Self { calls: RefCell::new(Vec::new()), answer: Box::new(answer) }
        }

    }

    impl Hub for FakeHub {
        fn invoke(&self, name: &str, args: &Value, caller_pid: u32, timeout: Duration) -> Result<Value, PostError> {
            self.calls.borrow_mut().push((name.into(), args.clone(), caller_pid, timeout));
            (self.answer)(name, args)
        }
    }

    /// A gateway on a local port that answers one request with `response` and hands back what it got.
    fn serve_once(response: &'static str) -> (String, std::thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            let mut got = Vec::new();
            let mut buf = [0u8; 4096];
            loop {
                let n = stream.read(&mut buf).unwrap();
                got.extend_from_slice(&buf[..n]);
                let text = String::from_utf8_lossy(&got).into_owned();
                if let Some(split) = text.find("\r\n\r\n") {
                    let length = text[..split]
                        .lines()
                        .find_map(|l| l.to_ascii_lowercase().strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().unwrap()))
                        .unwrap_or(0);
                    if got.len() >= split + 4 + length {
                        break;
                    }
                }
                if n == 0 {
                    break;
                }
            }
            let body = response.to_string();
            let reply = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len());
            stream.write_all(reply.as_bytes()).unwrap();
            String::from_utf8_lossy(&got).into_owned()
        });
        (url, handle)
    }

    fn tree() -> (tempfile::TempDir, PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let wt = tmp.path().join("ws/projects/awm/redux/harness");
        fs::create_dir_all(wt.join("src")).unwrap();
        fs::write(wt.join(".git"), "gitdir: x").unwrap();
        fs::create_dir_all(tmp.path().join("ws/projects/awm/.bare")).unwrap();
        (tmp, wt)
    }

    fn brief_answer(line: &'static str) -> FakeHub {
        FakeHub::new(move |name, _| match name {
            "journal_brief_read" => Ok(json!({"result": {"brief": {"line": line}}})),
            _ => Ok(json!({})),
        })
    }

    fn start(hub: &FakeHub, cwd: &Path) -> Option<String> {
        let seen = tempfile::tempdir().unwrap();
        let input = json!({"cwd": cwd, "source": "startup"}).to_string();
        brief(input.as_bytes(), hub, seen.path(), || None)
    }

    #[test]
    fn test_a_session_in_a_scope_gets_its_brief() {
        let (_tmp, wt) = tree();
        let line = "The brief of awm/redux/harness: For the harness lane.";
        let hub = brief_answer(line);
        let out: Value = serde_json::from_str(&start(&hub, &wt).unwrap()).unwrap();
        assert_eq!(out["hookSpecificOutput"], json!({"hookEventName": "SessionStart", "additionalContext": line}));
        let calls = hub.calls.borrow();
        assert_eq!(calls[0].1, json!({"project": "awm", "scope": "redux/harness"}));
        assert_eq!(calls[0].2, parent_pid());
        assert_eq!(calls[0].3, BRIEF_TIMEOUT);
    }

    fn fire(hub: &FakeHub, seen: &Path, source: &str, session_id: &str) {
        let input = json!({"cwd": "/tmp", "source": source, "session_id": session_id}).to_string();
        assert_eq!(brief(input.as_bytes(), hub, seen, || Some(std::process::id())), None);
    }

    fn clears(hub: &FakeHub) -> Vec<Value> {
        hub.calls.borrow().iter().filter(|c| c.0 == "reflection_harness_event").map(|c| c.1.clone()).collect()
    }

    #[test]
    fn test_a_clear_reports_the_id_the_process_had_before() {
        let state = tempfile::tempdir().unwrap();
        let seen = state.path().join("awm/cch/session-start");
        let hub = FakeHub::new(|_, _| Ok(json!({})));
        let (old, new) = ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002");
        fire(&hub, &seen, "startup", old);
        fire(&hub, &seen, "compact", old);
        assert!(clears(&hub).is_empty());
        fire(&hub, &seen, "clear", new);
        let parent = "cc00000000000040008000000000000001";
        assert_eq!(clears(&hub), vec![json!({"session_id": "cc00000000000040008000000000000002", "kind": "clear", "parent": parent})]);
        assert_eq!(hub.calls.borrow()[0].3, CLEAR_TIMEOUT);
    }

    fn payload(over: Value) -> Map<String, Value> {
        let mut base = json!({"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode",
            "session_id": "sid-1", "permission_mode": "acceptEdits",
            "tool_response": {"plan": "# a plan", "isAgent": false}});
        for (k, v) in over.as_object().unwrap() {
            base[k] = v.clone();
        }
        base.as_object().unwrap().clone()
    }

    #[test]
    fn test_an_approved_plan_is_acted_on() {
        assert!(should_act(&payload(json!({}))));
        assert!(should_act(&payload(json!({"permission_mode": "auto"}))));
        assert!(should_act(&payload(json!({"permission_mode": "default"}))));
    }

    #[test]
    fn test_a_rejected_plan_is_left_alone() {
        // Cycling the mode after a rejection would kick the session out of plan mode.
        assert!(!should_act(&payload(json!({"tool_response": {"status": "denied"}}))));
        assert!(!should_act(&payload(json!({"tool_response": "user rejected"}))));
        assert!(!should_act(&payload(json!({"tool_response": null}))));
    }

    #[test]
    fn test_the_call_carries_the_hooks_own_pid_and_expected_session() {
        let (url, got) = serve_once(r#"{"result": {"ok": true, "mode": "bypassPermissions"}}"#);
        let result = call_mode(&HttpHub::new(&url), "sid-1", HTTP_TIMEOUT_S).unwrap();
        assert_eq!(result, json!({"ok": true, "mode": "bypassPermissions"}));
        let request = got.join().unwrap();
        assert!(request.starts_with("POST /invoke HTTP/1.1\r\n"), "{request}");
        // Without the header the gateway strips `_caller_pid` and the call refuses.
        assert!(request.contains(&format!("\r\nX-Awm-Caller-Pid: {}\r\n", std::process::id())));
        let body: Value = serde_json::from_str(request.split("\r\n\r\n").nth(1).unwrap()).unwrap();
        assert_eq!(body, json!({"name": "reflection_mode", "args": {"expect_session": "sid-1"}}));
    }

    /// A clock that moves only when slept on.
    #[derive(Default)]
    struct FakePace(Cell<f64>);

    impl Pace for FakePace {
        fn monotonic(&self) -> f64 {
            self.0.get()
        }

        fn sleep(&self, secs: f64) {
            self.0.set(self.0.get() + secs);
        }
    }

    fn covered_footer_is_retried(hosting: &str) {
        let pace = FakePace::default();
        let calls = RefCell::new(Vec::new());
        let results = [json!({"ok": false, "mode": "unknown", "hosting": hosting}), json!({"ok": true})];
        let mut call = |sid: &str, _t: f64| {
            calls.borrow_mut().push(sid.to_string());
            Ok(results[calls.borrow().len() - 1].clone())
        };
        ensure_bypass("sid-1", &mut call, &pace, &mut |m| panic!("logged: {m}"));
        assert_eq!(*calls.borrow(), ["sid-1", "sid-1"]);
    }

    #[test]
    fn test_a_covered_footer_is_retried_tmux() {
        covered_footer_is_retried("tmux");
    }

    fn timed_out_call_is_never_retried(call_once: &dyn Fn() -> Result<Value, PostError>) {
        // The walk a timed-out call started is still pressing keys on the server; a second walk
        // on top of it steps past bypass.
        let pace = FakePace::default();
        let (mut calls, mut logged) = (0, Vec::new());
        let mut call = |_: &str, _: f64| {
            calls += 1;
            call_once()
        };
        ensure_bypass("sid-1", &mut call, &pace, &mut |m| logged.push(m.to_string()));
        assert_eq!(calls, 1);
        assert_eq!(logged.len(), 1);
        assert!(logged[0].contains("not retried"));
    }

    #[test]
    fn test_a_timed_out_call_is_never_retried_socket() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let hub = HttpHub::new(&format!("http://{}", listener.local_addr().unwrap()));
        timed_out_call_is_never_retried(&|| call_mode(&hub, "sid-1", 0.2));
    }

    #[test]
    fn test_every_attempt_fits_inside_the_hook_budget() {
        // Claude Code kills the hook at its settings timeout, so a late attempt gets less.
        let pace = FakePace::default();
        let timeouts = RefCell::new(Vec::new());
        let mut call = |_: &str, t: f64| {
            timeouts.borrow_mut().push(t);
            pace.sleep(t);
            Err(PostError::Failed("connection refused".into()))
        };
        ensure_bypass("sid-1", &mut call, &pace, &mut |_| {});
        let timeouts = timeouts.borrow();
        assert!(timeouts.iter().sum::<f64>() + RETRY_GAP_S * (timeouts.len() as f64 - 1.0) <= DEADLINE_S);
    }

    #[test]
    fn test_main_writes_nothing_to_stdout() {
        // A PostToolUse hook's stdout is parsed as a JSON control object.
        let pace = FakePace::default();
        let ensured = RefCell::new(Vec::new());
        let input = Value::Object(payload(json!({}))).to_string();
        assert_eq!(plan_mode(input.as_bytes(), &pace, |sid| ensured.borrow_mut().push(sid.to_string())), None);
        assert_eq!(*ensured.borrow(), ["sid-1"]);
        assert_eq!(pace.monotonic(), SETTLE_S);
    }

    #[test]
    fn test_main_survives_junk_on_stdin() {
        let pace = FakePace::default();
        assert_eq!(plan_mode(b"not json", &pace, |_| panic!("acted on junk")), None);
        assert_eq!(dispatch("plan-mode", b"not json"), None);
    }

    struct Cap {
        dir: tempfile::TempDir,
        clock: Cell<f64>,
        level: &'static str,
    }

    impl Cap {
        fn new(level: &'static str) -> Self {
            Self { dir: tempfile::tempdir().unwrap(), clock: Cell::new(epoch_now()), level }
        }

        fn fire(&self, event: &str, extra: Value) -> Option<Value> {
            let mut input = json!({"session_id": "s1", "hook_event_name": event});
            for (k, v) in extra.as_object().unwrap() {
                input[k] = v.clone();
            }
            let dir = self.dir.path().join("awm/cch/subagents");
            let out = subagent_cap(input.to_string().as_bytes(), &dir, || self.clock.get(), || self.level.to_string())?;
            Some(serde_json::from_str::<Value>(&out).unwrap()["hookSpecificOutput"].clone())
        }

        fn agent(&self) -> Option<Value> {
            self.fire("PreToolUse", json!({"tool_name": "Agent", "tool_input": {}}))
        }

        fn advance(&self, secs: f64) {
            self.clock.set(self.clock.get() + secs);
        }
    }

    #[test]
    fn test_a_cooperative_session_gets_five_at_a_time() {
        let cap = Cap::new("cooperative");
        for _ in 0..5 {
            assert_eq!(cap.agent(), None);
            cap.fire("SubagentStart", json!({}));
        }
        let denied = cap.agent().unwrap();
        assert_eq!(denied["permissionDecision"], "deny");
        assert!(denied["permissionDecisionReason"].as_str().unwrap().contains("at most 5"));
        cap.fire("SubagentStop", json!({}));
        assert_eq!(cap.agent(), None);
    }

    #[test]
    fn test_a_reservation_that_never_starts_lapses() {
        let cap = Cap::new("cooperative");
        cap.clock.set(1000.0);
        for _ in 0..5 {
            cap.agent();
        }
        assert!(cap.agent().is_some());
        cap.advance(RESERVE_S + 1.0);
        assert_eq!(cap.agent(), None);
    }

    #[test]
    fn test_a_trusted_session_is_not_capped() {
        let cap = Cap::new("trusted");
        for _ in 0..7 {
            assert_eq!(cap.agent(), None);
            cap.fire("SubagentStart", json!({}));
        }
    }

    #[test]
    fn test_an_unreachable_gateway_reads_as_untrusted() {
        assert_eq!(level(&HttpHub::new("http://127.0.0.1:9")), "untrusted");
    }
}
