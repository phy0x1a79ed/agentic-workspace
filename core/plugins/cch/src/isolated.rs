//! Isolated sessions (fleet.md R109): Claude Code in a rootless Docker container, running the
//! host's own binary read only inside tmux, seeing only its worktree and the project's bare repo.
//!
//! Its login is a copy of the host's without the refresh token, so the host stays the only
//! refresher and a fault signs out the container, never the host. It reaches awm over HTTP MCP
//! with a per-session caller token, since it has no host pid the gateway could resolve.

use std::collections::BTreeMap;
use std::future::Future;
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::io::AsRawFd;
use std::path::{Component, Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use tokio::io::AsyncWriteExt;

use crate::paths::{IsolatedKnobs, Paths};
use crate::resume::r9;
use crate::tmux::RunOutput;
use crate::verbs::py_repr;

/// The docker label every isolated container carries, valued with its session id.
pub const LABEL: &str = "awm.cch.isolated";
/// The container name prefix.
pub const PREFIX: &str = "awm-iso-";
pub const HOME_IN: &str = "/home/agent";
pub const CONFIG_IN: &str = "/home/agent/.claude";
/// Where the host's Claude binary is mounted, read only.
pub const CLAUDE_IN: &str = "/opt/claude/claude";
/// The tmux session inside the container that cch types into.
pub const TMUX_SESSION: &str = "main";
/// The image's recipe, built on the first start when the node lacks the image.
pub const DOCKERFILE: &str = include_str!("../isolated/Dockerfile");

/// What a container copy of the login keeps. `refreshToken` is left out on purpose.
pub const LOGIN_KEYS: [&str; 5] = ["accessToken", "expiresAt", "scopes", "subscriptionType", "rateLimitTier"];

/// The awm verbs an isolated session holds for its normal work. The gateway refuses every other
/// verb for a caller token, and a scope other than the session's own where a verb takes one.
/// Its reflection verbs must stay equal to the gateway's `OWN_SESSION_VERBS` ceiling.
pub const WHITELIST: [(&str, &[&str]); 5] = [
    ("journal", &["render", "inbox", "fetch", "ack", "post"]),
    ("search", &["query", "get"]),
    ("reflection", &["whoami", "compact", "message_self", "pending", "mode"]),
    ("operations", &["track", "status", "request_human", "done"]),
    // Rooms and machine channels for its own task (channels.md R111). A machine channel acts on
    // the other machine; nothing on it reaches this one.
    ("channel", &["open", "join", "write", "drain", "status", "close", "encrypt"]),
];

/// The rootful Docker daemon's sockets, which an isolated session never runs on.
pub const ROOTFUL_SOCKETS: [&str; 2] = ["/var/run/docker.sock", "/run/docker.sock"];

/// How long one docker command may run.
pub const DOCKER_TIMEOUT: Duration = Duration::from_secs(120);
/// How long the first start's image build may run: it fetches packages.
pub const BUILD_TIMEOUT: Duration = Duration::from_secs(900);
/// How often [`Isolated::watch`] checks the host login's stamp.
pub const WATCH_EVERY: Duration = Duration::from_secs(30);

const SINGLE_LINE: &str = "an isolated session takes one line at a time";
const NO_LOGIN: &str = "the host has no live Claude login to copy; run /login on the host first";
const NEEDS_SCOPE: &str = "an isolated session needs a project and a scope; it cannot start in the lobby";
const KEEPS_SETTINGS: &str = "an isolated session keeps the model, effort, name and folder it started with";

/// An isolated session could not be started or reached.
#[derive(Debug, thiserror::Error)]
pub enum IsolationError {
    #[error("{0}")]
    Refused(String),
    #[error("{0}")]
    Io(#[from] io::Error),
}

/// The whitelist as the token record and `caller_of_token` carry it.
pub fn whitelist_json() -> Value {
    Value::Object(WHITELIST.iter().map(|(tool, verbs)| (tool.to_string(), json!(verbs))).collect())
}

// --- files --------------------------------------------------------------------

fn random_bytes<const N: usize>() -> io::Result<[u8; N]> {
    let mut buf = [0u8; N];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut buf)?;
    Ok(buf)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Python's `base64.urlsafe_b64encode(...).rstrip(b"=")`, which `secrets.token_urlsafe` returns.
fn base64url(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = chunk.iter().enumerate().fold(0u32, |n, (i, b)| n | (u32::from(*b) << (16 - 8 * i)));
        for i in 0..=chunk.len() {
            out.push(char::from(ALPHABET[((n >> (18 - 6 * i)) & 63) as usize]));
        }
    }
    out
}

/// Write `text` to `path` readable by its owner only: a fresh 0600 temp file beside it, then an
/// atomic rename, so no reader ever sees a torn or wider-open file.
pub fn write_private(path: &Path, text: &str) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let tmp = path.with_file_name(format!("{name}.tmp.{}.{}", std::process::id(), hex(&random_bytes::<4>()?)));
    let mut file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&tmp)?;
    if let Err(e) = file.write_all(text.as_bytes()) {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }
    drop(file);
    std::fs::rename(&tmp, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&tmp);
    })
}

fn read_value(path: &Path) -> Option<Value> {
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}

fn pretty(v: &impl Serialize) -> String {
    serde_json::to_string_pretty(v).unwrap_or_default()
}

fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64)
}

/// Python's `Path.resolve()`: the real path where it exists, else the absolute path with `.` and
/// `..` folded.
fn resolve(path: &Path) -> PathBuf {
    if let Ok(real) = std::fs::canonicalize(path) {
        return real;
    }
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/")).join(path)
    };
    let mut out = PathBuf::new();
    for part in abs.components() {
        match part {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

fn clip(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

/// Python's `(out.stderr or out.stdout).strip()`.
fn complaint(out: &RunOutput) -> &str {
    if out.stderr.is_empty() { out.stdout.trim() } else { out.stderr.trim() }
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

// --- the login copy -----------------------------------------------------------

/// The host login without its refresh token, or None when it holds no access token.
pub fn strip_login(host: &Value) -> Option<Value> {
    let oauth = host.get("claudeAiOauth")?.as_object()?;
    if !oauth.get("accessToken").is_some_and(truthy) {
        return None;
    }
    let kept: Map<String, Value> =
        LOGIN_KEYS.iter().filter_map(|k| oauth.get(*k).map(|v| (k.to_string(), v.clone()))).collect();
    Some(json!({"claudeAiOauth": kept}))
}

/// What [`sync_login`] did to one copy.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LoginSync {
    Written,
    Unchanged,
    /// The host login is missing, unreadable, wiped or expired, so the copy was deleted.
    SignedOut,
}

impl LoginSync {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Written => "written",
            Self::Unchanged => "unchanged",
            Self::SignedOut => "signed_out",
        }
    }
}

fn expired(expires: Option<&Value>, now_ms: i64) -> bool {
    match expires {
        Some(Value::Number(n)) => n.as_f64().is_some_and(|e| e <= now_ms as f64),
        Some(Value::Bool(b)) => i64::from(*b) <= now_ms,
        _ => false,
    }
}

/// Bring the container copy at `dest` up to date with the host login at `host`, as of `now_ms`.
pub fn sync_login(dest: &Path, host: &Path, now_ms: i64) -> io::Result<LoginSync> {
    let copy = read_value(host).as_ref().and_then(strip_login);
    let live = copy.filter(|c| !expired(c["claudeAiOauth"].get("expiresAt"), now_ms));
    let Some(copy) = live else {
        match std::fs::remove_file(dest) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e),
            _ => return Ok(LoginSync::SignedOut),
        }
    };
    let text = pretty(&copy);
    if std::fs::read_to_string(dest).is_ok_and(|old| old == text) {
        return Ok(LoginSync::Unchanged);
    }
    write_private(dest, &text)?;
    Ok(LoginSync::Written)
}

// --- caller tokens ------------------------------------------------------------

fn digest(token: &str) -> String {
    hex(&Sha256::digest(token.as_bytes()))
}

/// `tokens.json` under the isolated dir: `{sha256 hex of a token: the holder's record}`, 0600.
/// Only digests are kept, so the file names no usable token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenStore {
    pub dir: PathBuf,
}

/// An exclusive `flock` on `tokens.json.lock`, held until dropped, so two cch copies never
/// interleave a read and a rewrite of the tokens file.
struct TokensLock {
    _file: std::fs::File,
}

impl TokensLock {
    fn take(dir: &Path) -> io::Result<Self> {
        std::fs::create_dir_all(dir)?;
        let file = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .mode(0o600)
            .open(dir.join("tokens.json.lock"))?;
        // SAFETY: flock on a descriptor this function owns; it is released when the file closes.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self { _file: file })
    }
}

impl TokenStore {
    pub fn path(&self) -> PathBuf {
        self.dir.join("tokens.json")
    }

    /// Every digest and its record; empty when the file is missing or torn.
    pub fn read(&self) -> Map<String, Value> {
        match read_value(&self.path()) {
            Some(Value::Object(map)) => map,
            _ => Map::new(),
        }
    }

    /// A new caller token for `record`: 32 random bytes, URL-safe base64. Only its digest is kept.
    pub fn mint(&self, record: Map<String, Value>) -> io::Result<String> {
        let token = base64url(&random_bytes::<32>()?);
        let _lock = TokensLock::take(&self.dir)?;
        let mut data = self.read();
        data.insert(digest(&token), Value::Object(record));
        write_private(&self.path(), &pretty(&data))?;
        Ok(token)
    }

    /// Drop every token held for `claude_session_id`. Returns how many went.
    pub fn revoke(&self, claude_session_id: &str) -> io::Result<usize> {
        let _lock = TokensLock::take(&self.dir)?;
        let data = self.read();
        let before = data.len();
        let kept: Map<String, Value> = data
            .into_iter()
            .filter(|(_, r)| r.get("claude_session_id").and_then(Value::as_str) != Some(claude_session_id))
            .collect();
        if kept.len() != before {
            write_private(&self.path(), &pretty(&kept))?;
        }
        Ok(before - kept.len())
    }

    /// The record of whoever holds `token`. Read without the lock: the rename keeps it whole.
    pub fn caller_of_token(&self, token: Option<&str>) -> Option<Map<String, Value>> {
        let token = token.filter(|t| !t.is_empty())?;
        match self.read().remove(&digest(token)) {
            Some(Value::Object(rec)) => Some(rec),
            _ => None,
        }
    }
}

// --- the container ------------------------------------------------------------

/// The rootless Docker socket: `knob`, else the user's own. A rootful one is refused (R101).
pub fn docker_host(knob: Option<&str>, uid: u32) -> Result<String, IsolationError> {
    let host = knob.map_or_else(|| format!("unix:///run/user/{uid}/docker.sock"), str::to_string);
    let path = host.strip_prefix("unix://").unwrap_or(&host);
    if ROOTFUL_SOCKETS.contains(&path) {
        return Err(IsolationError::Refused(format!(
            "{host} is the rootful Docker daemon; an isolated session runs only on rootless Docker"
        )));
    }
    Ok(host)
}

/// Runs one docker command line; tests pass a fake CLI.
pub trait Docker: Send + Sync + 'static {
    /// Run `argv` (the binary first) with exactly `env`, feeding `stdin` when given and killing it
    /// after `timeout`. An error means it did not start or did not finish.
    fn run(
        &self,
        argv: Vec<String>,
        env: Vec<(String, String)>,
        stdin: Option<Vec<u8>>,
        timeout: Duration,
    ) -> impl Future<Output = io::Result<RunOutput>> + Send;
}

/// The live docker CLI: a child process per command.
#[derive(Debug, Clone, Copy, Default)]
pub struct DockerCli;

impl Docker for DockerCli {
    async fn run(
        &self,
        argv: Vec<String>,
        env: Vec<(String, String)>,
        stdin: Option<Vec<u8>>,
        timeout: Duration,
    ) -> io::Result<RunOutput> {
        let mut cmd = tokio::process::Command::new(&argv[0]);
        cmd.args(&argv[1..])
            .env_clear()
            .envs(env)
            .stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::null() })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        let mut child = cmd.spawn()?;
        if let (Some(data), Some(mut pipe)) = (stdin, child.stdin.take()) {
            pipe.write_all(&data).await?;
        }
        let out = tokio::time::timeout(timeout, child.wait_with_output()).await.map_err(|_| {
            io::Error::new(io::ErrorKind::TimedOut, format!("docker {} timed out after {timeout:?}", argv.get(1).map_or("", String::as_str)))
        })??;
        Ok(RunOutput {
            success: out.status.success(),
            stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
        })
    }
}

/// The container name of a session: the prefix and the first 12 hex digits of its id.
pub fn container_name(claude_session_id: &str) -> String {
    let digits: String = claude_session_id.replace('-', "").chars().take(12).collect();
    format!("{PREFIX}{digits}")
}

/// The repository a worktree's commits land in, which the container must also see. None for a
/// plain checkout or a worktree with no readable `gitdir:` line.
pub fn git_common_dir(worktree: &Path) -> Option<PathBuf> {
    let marker = worktree.join(".git");
    if marker.is_dir() {
        return None;
    }
    let line = std::fs::read_to_string(&marker).ok()?;
    let rest = line.trim().strip_prefix("gitdir:")?;
    let mut gitdir = PathBuf::from(rest.trim());
    if !gitdir.is_absolute() {
        gitdir = resolve(&worktree.join(gitdir));
    }
    match std::fs::read_to_string(gitdir.join("commondir")) {
        Ok(rel) => Some(resolve(&gitdir.join(rel.trim()))),
        Err(_) => gitdir.parent().and_then(Path::parent).map(Path::to_path_buf),
    }
}

/// How to run one isolated session's container.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Spec {
    pub claude_session_id: String,
    pub name: String,
    pub worktree: PathBuf,
    pub claude_bin: PathBuf,
    pub prompt: String,
    pub model: Option<String>,
    pub effort: Option<String>,
    /// Resume the session's transcript instead of starting it fresh.
    pub resume: bool,
    pub image: String,
    pub network: String,
    pub cpus: String,
    pub memory: String,
    pub pids: u64,
}

impl Spec {
    /// A fresh-start spec with no prompt, model or effort, and the node's limits from `knobs`.
    pub fn new(knobs: &IsolatedKnobs, claude_session_id: &str, name: &str, worktree: &Path, claude_bin: &Path) -> Self {
        Self {
            claude_session_id: claude_session_id.into(),
            name: name.into(),
            worktree: worktree.into(),
            claude_bin: claude_bin.into(),
            prompt: String::new(),
            model: None,
            effort: None,
            resume: false,
            image: knobs.image.clone(),
            network: knobs.network.clone(),
            cpus: knobs.cpus.clone(),
            memory: knobs.memory.clone(),
            pids: knobs.pids,
        }
    }
}

/// A session's folder under the isolated dir `root`.
pub fn session_dir(root: &Path, claude_session_id: &str) -> PathBuf {
    root.join(claude_session_id)
}

/// The container's whole `~/.claude`, which it writes its transcripts into.
pub fn config_dir(root: &Path, claude_session_id: &str) -> PathBuf {
    session_dir(root, claude_session_id).join("claude")
}

/// The `docker run` arguments for one isolated session whose isolated dir is `root`.
pub fn run_argv(spec: &Spec, root: &Path) -> Vec<String> {
    let sid = spec.claude_session_id.as_str();
    let wt = spec.worktree.display().to_string();
    let mut mounts = vec![
        "-v".to_string(),
        format!("{}:{CLAUDE_IN}:ro", spec.claude_bin.display()),
        "-v".into(),
        format!("{}:{CONFIG_IN}", config_dir(root, sid).display()),
        "-v".into(),
        format!("{wt}:{wt}"),
    ];
    if let Some(common) = git_common_dir(&spec.worktree) {
        mounts.extend(["-v".into(), format!("{0}:{0}", common.display())]);
    }
    let mut claude: Vec<String> = [CLAUDE_IN, "--dangerously-skip-permissions", "--mcp-config"]
        .into_iter()
        .map(String::from)
        .chain([format!("{CONFIG_IN}/awm-mcp.json"), "--strict-mcp-config".into()])
        .collect();
    claude.extend([if spec.resume { "--resume" } else { "--session-id" }.to_string(), sid.to_string()]);
    if let Some(model) = spec.model.as_deref().filter(|m| !m.is_empty()) {
        claude.extend(["--model".into(), model.into()]);
    }
    if let Some(effort) = spec.effort.as_deref().filter(|e| !e.is_empty()) {
        claude.extend(["--effort".into(), effort.into()]);
    }
    if !spec.prompt.is_empty() {
        claude.extend(["--".into(), spec.prompt.clone()]);
    }
    let mut argv: Vec<String> = ["run", "-d", "-t", "--name"].into_iter().map(String::from).collect();
    argv.extend([
        container_name(sid),
        "--label".into(),
        format!("{LABEL}={sid}"),
        "--cpus".into(),
        spec.cpus.clone(),
        "--memory".into(),
        spec.memory.clone(),
        "--memory-swap".into(),
        spec.memory.clone(),
        "--pids-limit".into(),
        spec.pids.to_string(),
        "--cap-drop".into(),
        "ALL".into(),
        "--security-opt".into(),
        "no-new-privileges".into(),
        "--network".into(),
        spec.network.clone(),
        "--add-host".into(),
        "host.docker.internal:host-gateway".into(),
    ]);
    argv.extend(mounts);
    argv.extend([
        "-w".into(),
        wt,
        "-e".into(),
        format!("HOME={HOME_IN}"),
        "-e".into(),
        format!("CLAUDE_CONFIG_DIR={CONFIG_IN}"),
        "-e".into(),
        "IS_SANDBOX=1".into(),
        spec.image.clone(),
        "tmux".into(),
        "new-session".into(),
        "-s".into(),
        TMUX_SESSION.into(),
    ]);
    argv.extend(claude);
    argv
}

/// The container's own Claude config under the isolated dir `root`: the login copy, the MCP
/// config with its caller token, and onboarding and the worktree's trust dialog marked done.
pub fn prepare(spec: &Spec, root: &Path, host_login: &Path, hub_url: &str, token: &str) -> Result<(), IsolationError> {
    let cfg = config_dir(root, &spec.claude_session_id);
    std::fs::create_dir_all(&cfg)?;
    if sync_login(&cfg.join(".credentials.json"), host_login, now_ms())? == LoginSync::SignedOut {
        return Err(IsolationError::Refused(NO_LOGIN.into()));
    }
    let mcp = json!({"mcpServers": {"awm": {"type": "http", "url": format!("{hub_url}/mcp"),
                                            "headers": {"X-Awm-Caller-Token": token}}}});
    write_private(&cfg.join("awm-mcp.json"), &pretty(&mcp))?;
    let state = cfg.join(".claude.json");
    let mut known = match read_value(&state) {
        Some(Value::Object(map)) => map,
        _ => Map::new(),
    };
    known.insert("hasCompletedOnboarding".into(), Value::Bool(true));
    let projects = known.entry("projects").or_insert_with(|| json!({}));
    if !projects.is_object() {
        *projects = json!({});
    }
    let entry = projects
        .as_object_mut()
        .map(|p| p.entry(spec.worktree.display().to_string()).or_insert_with(|| json!({})));
    if let Some(entry) = entry {
        if !entry.is_object() {
            *entry = json!({});
        }
        entry["hasTrustDialogAccepted"] = Value::Bool(true);
    }
    write_private(&state, &pretty(&known))?;
    Ok(())
}

// --- per-session records ------------------------------------------------------

/// One isolated session's `record.json`. Keys the port does not name ride in `extra`, so a token
/// record's `trust` and `tools` survive a round trip.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Record {
    pub claude_session_id: String,
    #[serde(default)]
    pub container: Option<String>,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub project: Option<String>,
    #[serde(default)]
    pub scope: Option<String>,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub effort: Option<String>,
    #[serde(default)]
    pub claude_bin: Option<String>,
    #[serde(default)]
    pub started_ms: Option<i64>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Record {
    fn from_map(map: Map<String, Value>) -> Option<Self> {
        serde_json::from_value::<Self>(Value::Object(map)).ok().filter(|r| !r.claude_session_id.is_empty())
    }

    fn to_map(&self) -> Map<String, Value> {
        match serde_json::to_value(self) {
            Ok(Value::Object(map)) => map,
            _ => Map::new(),
        }
    }

    fn container(&self) -> String {
        self.container.clone().unwrap_or_else(|| container_name(&self.claude_session_id))
    }
}

/// Every isolated session on this node with a readable record, by folder name.
pub fn records(root: &Path) -> Vec<Record> {
    let Ok(entries) = std::fs::read_dir(root) else { return Vec::new() };
    let mut dirs: Vec<PathBuf> = entries.filter_map(Result::ok).map(|e| e.path()).filter(|p| p.is_dir()).collect();
    dirs.sort();
    dirs.iter()
        .filter_map(|d| match read_value(&d.join("record.json")) {
            Some(Value::Object(map)) => Record::from_map(map),
            _ => None,
        })
        .collect()
}

/// The isolated session `reference` names: its session id (dashed or not), container or name.
pub fn find(root: &Path, reference: &str) -> Option<Record> {
    let text = reference.trim().to_lowercase();
    if text.is_empty() {
        return None;
    }
    records(root).into_iter().find(|rec| {
        let sid = &rec.claude_session_id;
        text == *sid
            || text == sid.replace('-', "")
            || rec.container.as_deref() == Some(text.as_str())
            || rec.name.as_deref().unwrap_or("").to_lowercase() == text
    })
}

/// A parsed isolated `start`, as lifecycle's argument parse leaves it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StartRequest {
    pub project: Option<String>,
    pub scope: Option<String>,
    pub name: String,
    pub prompt: String,
    pub model: Option<String>,
    pub effort: Option<String>,
}

/// One node's isolated sessions, run on Docker through `docker`.
pub struct Isolated<D = DockerCli> {
    pub knobs: IsolatedKnobs,
    /// The host login, `<claude home>/.credentials.json`.
    pub host_login: PathBuf,
    pub claude_bin: PathBuf,
    /// The uid whose rootless socket is the default Docker host.
    pub uid: u32,
    /// The environment docker runs in, before `DOCKER_HOST` is set and `SSH_AUTH_SOCK` dropped.
    pub base_env: Vec<(String, String)>,
    pub docker: D,
    image_ready: AtomicBool,
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> io::Result<T> {
    tokio::task::spawn_blocking(f).await.map_err(io::Error::other)
}

impl Isolated<DockerCli> {
    /// This node's isolated sessions under `paths`, on the docker CLI in this process's environment.
    pub fn live(paths: &Paths) -> Self {
        let env = std::env::vars_os().filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?))).collect();
        // SAFETY: getuid cannot fail.
        let uid = unsafe { libc::getuid() };
        Self::new(paths, DockerCli, uid, env)
    }
}

impl<D: Docker> Isolated<D> {
    /// The sessions under `paths`' isolated dir, with the host login under its Claude home.
    pub fn new(paths: &Paths, docker: D, uid: u32, base_env: Vec<(String, String)>) -> Self {
        Self {
            knobs: paths.isolated.clone(),
            host_login: paths.home.root.join(".credentials.json"),
            claude_bin: paths.claude_bin.clone(),
            uid,
            base_env,
            docker,
            image_ready: AtomicBool::new(false),
        }
    }

    pub fn tokens(&self) -> TokenStore {
        TokenStore { dir: self.knobs.dir.clone() }
    }

    pub fn session_dir(&self, claude_session_id: &str) -> PathBuf {
        session_dir(&self.knobs.dir, claude_session_id)
    }

    pub fn config_dir(&self, claude_session_id: &str) -> PathBuf {
        config_dir(&self.knobs.dir, claude_session_id)
    }

    /// Run `docker <args>` on the rootless daemon, without the host's ssh agent.
    pub async fn docker(&self, args: Vec<String>, stdin: Option<Vec<u8>>, timeout: Duration) -> Result<RunOutput, IsolationError> {
        let host = docker_host(self.knobs.docker_host.as_deref(), self.uid)?;
        let mut env: Vec<(String, String)> =
            self.base_env.iter().filter(|(k, _)| k != "SSH_AUTH_SOCK" && k != "DOCKER_HOST").cloned().collect();
        env.push(("DOCKER_HOST".into(), host));
        let argv = std::iter::once("docker".to_string()).chain(args).collect();
        Ok(self.docker.run(argv, env, stdin, timeout).await?)
    }

    async fn docker_args(&self, args: &[&str]) -> Result<RunOutput, IsolationError> {
        self.docker(args.iter().map(|a| a.to_string()).collect(), None, DOCKER_TIMEOUT).await
    }

    /// Build the image from [`DOCKERFILE`] unless docker already has it. Checked once per process.
    pub async fn ensure_image(&self) -> Result<(), IsolationError> {
        if self.image_ready.load(Ordering::Relaxed) {
            return Ok(());
        }
        let image = self.knobs.image.as_str();
        if !self.docker_args(&["image", "inspect", image]).await?.success {
            let args = ["build", "-t", image, "-"].map(String::from).to_vec();
            let out = self.docker(args, Some(DOCKERFILE.as_bytes().to_vec()), BUILD_TIMEOUT).await?;
            if !out.success {
                return Err(IsolationError::Refused(format!("docker build of {image} failed: {}", clip(complaint(&out), 400))));
            }
        }
        self.image_ready.store(true, Ordering::Relaxed);
        Ok(())
    }

    /// [`records`] off the async path.
    pub async fn records(&self) -> Vec<Record> {
        let root = self.knobs.dir.clone();
        blocking(move || records(&root)).await.unwrap_or_default()
    }

    /// [`find`] off the async path.
    pub async fn find(&self, reference: &str) -> Option<Record> {
        let (root, reference) = (self.knobs.dir.clone(), reference.to_string());
        blocking(move || find(&root, &reference)).await.ok().flatten()
    }

    /// [`TokenStore::mint`] off the async path.
    pub async fn mint(&self, record: Map<String, Value>) -> io::Result<String> {
        let store = self.tokens();
        blocking(move || store.mint(record)).await?
    }

    /// [`TokenStore::revoke`] off the async path.
    pub async fn revoke(&self, claude_session_id: &str) -> io::Result<usize> {
        let (store, sid) = (self.tokens(), claude_session_id.to_string());
        blocking(move || store.revoke(&sid)).await?
    }

    /// [`TokenStore::caller_of_token`] off the async path.
    pub async fn caller_of_token(&self, token: Option<&str>) -> Option<Map<String, Value>> {
        let (store, token) = (self.tokens(), token.map(str::to_string));
        blocking(move || store.caller_of_token(token.as_deref())).await.ok().flatten()
    }

    /// Sync the login copy of every isolated session on this node, by session id.
    pub async fn sync_all(&self) -> io::Result<Vec<(String, LoginSync)>> {
        let (root, host) = (self.knobs.dir.clone(), self.host_login.clone());
        blocking(move || {
            let now = now_ms();
            records(&root)
                .into_iter()
                .map(|rec| {
                    let dest = config_dir(&root, &rec.claude_session_id).join(".credentials.json");
                    Ok((rec.claude_session_id, sync_login(&dest, &host, now)?))
                })
                .collect()
        })
        .await?
    }

    /// One pass of [`Isolated::watch`]: sync every copy when the host login's stamp differs from
    /// `seen`, which it then updates. Returns what changed.
    pub async fn watch_once(&self, seen: &mut Option<Option<(u64, i128, u64)>>) -> Vec<(String, LoginSync)> {
        let stamp = tokio::fs::metadata(&self.host_login)
            .await
            .ok()
            .map(|m| (m.ino(), i128::from(m.mtime()) * 1_000_000_000 + i128::from(m.mtime_nsec()), m.size()));
        if seen.as_ref() == Some(&stamp) {
            return Vec::new();
        }
        *seen = Some(stamp);
        match self.sync_all().await {
            Ok(all) => all.into_iter().filter(|(_, what)| *what != LoginSync::Unchanged).collect(),
            Err(e) => {
                tracing::warn!("cch: isolated login sync failed: {e}");
                Vec::new()
            }
        }
    }

    /// Sync every copy whenever the host login file changes, checking every `every`. Never returns.
    pub async fn watch(&self, every: Duration) {
        let mut seen = None;
        loop {
            for (sid, what) in self.watch_once(&mut seen).await {
                tracing::info!("cch: isolated {sid} login {}", what.as_str());
            }
            tokio::time::sleep(every).await;
        }
    }

    async fn launch(&self, spec: Spec, rec: &Record) -> Result<(), IsolationError> {
        let sid = spec.claude_session_id.clone();
        self.revoke(&sid).await?;
        let mut held = rec.to_map();
        held.insert("trust".into(), json!("untrusted"));
        held.insert("tools".into(), whitelist_json());
        let token = self.mint(held).await?;
        let out = match self.run_container(spec, token).await {
            Ok(out) => out,
            Err(e) => {
                let _ = self.revoke(&sid).await;
                return Err(e);
            }
        };
        if !out.success {
            let _ = self.revoke(&sid).await;
            return Err(IsolationError::Refused(format!("docker run failed: {}", clip(complaint(&out), 400))));
        }
        Ok(())
    }

    async fn run_container(&self, spec: Spec, token: String) -> Result<RunOutput, IsolationError> {
        let name = container_name(&spec.claude_session_id);
        let (root, host, hub) = (self.knobs.dir.clone(), self.host_login.clone(), self.knobs.hub_url.clone());
        let argv = blocking(move || prepare(&spec, &root, &host, &hub, &token).map(|()| run_argv(&spec, &root))).await??;
        self.ensure_image().await?;
        self.docker_args(&["rm", "-f", &name]).await?;
        self.docker(argv, None, DOCKER_TIMEOUT).await
    }

    /// Start one isolated session in `worktree`. Returns its record.
    pub async fn start(&self, req: &StartRequest, worktree: &Path) -> Result<Record, IsolationError> {
        let sid = uuid::Uuid::new_v4().to_string();
        let (root, bin, wt) = (self.knobs.dir.clone(), self.claude_bin.clone(), worktree.to_path_buf());
        let rec = Record {
            claude_session_id: sid.clone(),
            container: Some(container_name(&sid)),
            name: Some(req.name.clone()),
            project: req.project.clone(),
            scope: req.scope.clone(),
            cwd: Some(worktree.display().to_string()),
            model: req.model.clone(),
            effort: req.effort.clone(),
            claude_bin: None,
            started_ms: Some(now_ms()),
            extra: Map::new(),
        };
        let (rec, binary) = blocking(move || -> Result<(Record, PathBuf), IsolationError> {
            let binary = resolve(&bin);
            if !binary.is_file() {
                return Err(IsolationError::Refused(format!("the Claude binary {} is not a file", binary.display())));
            }
            let rec = Record { claude_bin: Some(binary.display().to_string()), ..rec };
            std::fs::create_dir_all(session_dir(&root, &sid))?;
            write_private(&session_dir(&root, &sid).join("record.json"), &pretty(&rec))?;
            Ok((rec, binary))
        })
        .await??;
        let spec = Spec {
            prompt: req.prompt.clone(),
            model: req.model.clone(),
            effort: req.effort.clone(),
            ..Spec::new(&self.knobs, &rec.claude_session_id, &req.name, &wt, &binary)
        };
        self.launch(spec, &rec).await?;
        Ok(rec)
    }

    /// Start `rec`'s container again on its own transcript.
    pub async fn resume(&self, rec: &Record, prompt: &str) -> Result<(), IsolationError> {
        let missing = |key: &str| IsolationError::Refused(format!("the isolated record {} has no {key}", rec.claude_session_id));
        let cwd = rec.cwd.as_deref().ok_or_else(|| missing("cwd"))?;
        let bin = rec.claude_bin.as_deref().ok_or_else(|| missing("claude_bin"))?;
        let spec = Spec {
            prompt: prompt.into(),
            model: rec.model.clone(),
            effort: rec.effort.clone(),
            resume: true,
            ..Spec::new(&self.knobs, &rec.claude_session_id, rec.name.as_deref().unwrap_or(""), Path::new(cwd), Path::new(bin))
        };
        self.launch(spec, rec).await
    }

    /// Whether `rec`'s container runs.
    pub async fn running(&self, rec: &Record) -> Result<bool, IsolationError> {
        let out = self.docker_args(&["inspect", "-f", "{{.State.Running}}", &rec.container()]).await?;
        Ok(out.success && out.stdout.trim() == "true")
    }

    /// Remove the container and its caller token. With `keep` the folder, and so the transcript,
    /// stays.
    pub async fn stop(&self, rec: &Record, keep: bool) -> Result<Value, IsolationError> {
        self.docker_args(&["rm", "-f", &rec.container()]).await?;
        self.revoke(&rec.claude_session_id).await?;
        if !keep {
            let dir = self.session_dir(&rec.claude_session_id);
            let _ = tokio::fs::remove_dir_all(dir).await;
        }
        Ok(json!({"ok": true, "stopped": true, "removed": !keep, "kept": keep}))
    }

    /// Type one line into the session, and Enter when `submit`.
    pub async fn send(&self, rec: &Record, text: &str, submit: bool) -> Result<Value, IsolationError> {
        if text.contains(['\n', '\r']) {
            return Ok(json!({"ok": false, "reason": SINGLE_LINE}));
        }
        let target = rec.container();
        let mut out = self.docker_args(&["exec", &target, "tmux", "send-keys", "-t", TMUX_SESSION, "-l", text]).await?;
        if out.success && submit {
            out = self.docker_args(&["exec", &target, "tmux", "send-keys", "-t", TMUX_SESSION, "Enter"]).await?;
        }
        if !out.success {
            return Ok(json!({"ok": false, "reason": format!("could not type into {target}: {}", clip(complaint(&out), 300))}));
        }
        Ok(json!({"ok": true, "submitted": submit, "text": text}))
    }

    /// Press Escape in the session.
    pub async fn interrupt(&self, rec: &Record) -> Result<Value, IsolationError> {
        let out = self.docker_args(&["exec", &rec.container(), "tmux", "send-keys", "-t", TMUX_SESSION, "Escape"]).await?;
        Ok(json!({"ok": out.success}))
    }

    // --- the verbs' answers ---------------------------------------------------

    /// Whether `rec` runs, any failure to ask counting as no.
    pub async fn iso_live(&self, rec: &Record) -> bool {
        self.running(rec).await.unwrap_or(false)
    }

    /// `rec` as a row of `list`, `find` and `status`; `live` skips asking docker.
    pub async fn iso_row(&self, rec: &Record, live: Option<bool>) -> Value {
        let live = match live {
            Some(live) => live,
            None => self.iso_live(rec).await,
        };
        let sid = &rec.claude_session_id;
        json!({"session_id": r9(sid), "claude_session_id": sid, "name": rec.name, "job": null, "pid": null,
               "live": live, "kind": "isolated", "container": rec.container, "project": rec.project,
               "scope": rec.scope, "cwd": rec.cwd})
    }

    /// The `caller_of_token` answer the gateway's isolated door reads.
    pub async fn caller_answer(&self, token: &Value) -> Value {
        let Some(map) = self.caller_of_token(token.as_str()).await else { return json!({"ok": true, "known": false}) };
        let trust = map.get("trust").cloned().unwrap_or(Value::Null);
        let tools = map.get("tools").filter(|t| truthy(t)).cloned().unwrap_or_else(|| json!({}));
        let Some(rec) = Record::from_map(map) else { return json!({"ok": true, "known": false}) };
        let mut out = json!({"ok": true, "known": true});
        merge(&mut out, self.iso_row(&rec, Some(true)).await);
        out["trust"] = trust;
        out["tools"] = tools;
        out
    }

    /// The `start` verb with `isolated=true`. `refusal` is the station or caller refusal already
    /// worked out; `resolve` gives the scope's worktree, or the reason it cannot. It runs in no
    /// daemon and has no job.
    pub async fn start_isolated<F, Fut>(&self, req: &StartRequest, refusal: Option<String>, node: &str, resolve: F) -> Value
    where
        F: FnOnce(String, String) -> Fut,
        Fut: Future<Output = Result<PathBuf, String>>,
    {
        if let Some(reason) = refusal {
            return json!({"ok": false, "reason": reason});
        }
        let (Some(project), Some(scope)) = (req.project.clone().filter(|p| !p.is_empty()), req.scope.clone().filter(|s| !s.is_empty()))
        else {
            return json!({"ok": false, "reason": NEEDS_SCOPE});
        };
        let cwd = match resolve(project, scope).await {
            Ok(cwd) => cwd,
            Err(reason) => return json!({"ok": false, "reason": reason}),
        };
        for r in self.records().await {
            if r.name.as_deref() == Some(req.name.as_str()) && self.iso_live(&r).await {
                return json!({"ok": false, "reason": format!("a live isolated session is already named {}", py_repr(&req.name))});
            }
        }
        match self.start(req, &cwd).await {
            Ok(rec) => {
                let mut out = json!({"ok": true, "isolated": true, "node": node});
                merge(&mut out, self.iso_row(&rec, Some(true)).await);
                out
            }
            Err(e) => json!({"ok": false, "reason": format!("isolated start failed: {e}")}),
        }
    }

    /// The `find` answer for an isolated session.
    pub async fn find_answer(&self, rec: &Record) -> Value {
        json!({"ok": true, "matches": [self.iso_row(rec, None).await]})
    }

    /// The `status` answer for an isolated session.
    pub async fn status_answer(&self, rec: &Record) -> Value {
        let row = self.iso_row(rec, None).await;
        let state = if row["live"] == Value::Bool(true) { "running" } else { "stopped" };
        let mut out = json!({"ok": true});
        merge(&mut out, row);
        out["state"] = json!(state);
        out
    }

    /// The isolated rows of `list`, filtered by `project` and `scope`; stopped ones only with
    /// `include_stopped`.
    pub async fn list_rows(&self, project: Option<&str>, scope: Option<&str>, include_stopped: bool) -> Vec<Value> {
        let mut rows = Vec::new();
        for rec in self.records().await {
            let row = self.iso_row(&rec, None).await;
            let matches = |key: &str, want: Option<&str>| want.is_none_or(|w| w.is_empty() || row[key].as_str() == Some(w));
            if matches("project", project) && matches("scope", scope) && (row["live"] == Value::Bool(true) || include_stopped) {
                rows.push(row);
            }
        }
        rows
    }

    /// The `stop` answer: the container goes either way, the transcript stays, so a retired
    /// isolated session is still resumable (sessions.md R10).
    pub async fn stop_answer(&self, rec: &Record) -> Result<Value, IsolationError> {
        self.stop(rec, true).await
    }

    /// The `resume` answer: refused while it still runs.
    pub async fn resume_answer(&self, rec: &Record, prompt: &str) -> Result<Value, IsolationError> {
        if self.iso_live(rec).await {
            let name = rec.name.as_deref().unwrap_or("None");
            return Ok(json!({"ok": false, "reason": format!("{name} is already running")}));
        }
        self.resume(rec, prompt).await?;
        let mut out = json!({"ok": true, "resumed": true});
        merge(&mut out, self.iso_row(rec, Some(true)).await);
        Ok(out)
    }
}

/// The `set` answer for an isolated session.
pub fn set_refusal() -> Value {
    awm_harness::contract::unsupported(KEEPS_SETTINGS)
}

fn merge(into: &mut Value, from: Value) {
    if let (Some(into), Value::Object(from)) = (into.as_object_mut(), from) {
        into.extend(from);
    }
}

/// The whitelist as a map, for comparing against another list.
pub fn whitelist() -> BTreeMap<&'static str, &'static [&'static str]> {
    WHITELIST.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;
    use std::os::unix::fs::PermissionsExt;
    use std::sync::{Arc, Mutex};

    use tempfile::TempDir;

    use super::*;

    const HOUR_MS: i64 = 3_600_000;

    fn login(expires_ms: i64) -> Value {
        json!({"claudeAiOauth": {"accessToken": "fake-access", "refreshToken": "fake-refresh",
                                 "expiresAt": expires_ms, "scopes": ["user:inference"],
                                 "subscriptionType": "max"}})
    }

    type Call = (Vec<String>, Vec<(String, String)>, Option<Vec<u8>>);

    #[derive(Default)]
    struct DockerState {
        calls: Vec<Call>,
        up: HashSet<String>,
        fail_run: bool,
        no_image: bool,
    }

    /// A fake docker CLI: records each call and keeps which containers run.
    #[derive(Clone, Default)]
    struct FakeDocker(Arc<Mutex<DockerState>>);

    impl FakeDocker {
        fn verbs(&self) -> Vec<String> {
            self.0.lock().unwrap().calls.iter().map(|c| c.0[1].clone()).collect()
        }

        fn last(&self) -> Call {
            self.0.lock().unwrap().calls.last().cloned().unwrap()
        }
    }

    fn out(success: bool, stdout: &str, stderr: &str) -> RunOutput {
        RunOutput { success, stdout: stdout.into(), stderr: stderr.into() }
    }

    impl Docker for FakeDocker {
        async fn run(&self, argv: Vec<String>, env: Vec<(String, String)>, stdin: Option<Vec<u8>>, _: Duration) -> io::Result<RunOutput> {
            let mut s = self.0.lock().unwrap();
            s.calls.push((argv.clone(), env, stdin));
            let (verb, rest) = (argv[1].as_str(), &argv[2..]);
            Ok(match verb {
                "run" if s.fail_run => out(false, "", "no such image"),
                "run" => {
                    let name = rest[rest.iter().position(|a| a == "--name").unwrap() + 1].clone();
                    s.up.insert(name);
                    out(true, "", "")
                }
                "rm" => {
                    s.up.remove(rest.last().unwrap());
                    out(true, "", "")
                }
                "inspect" => out(true, if s.up.contains(rest.last().unwrap()) { "true\n" } else { "false\n" }, ""),
                "image" => out(!s.no_image, "", if s.no_image { "no such image" } else { "" }),
                "build" => {
                    s.no_image = false;
                    out(true, "", "")
                }
                "exec" if !s.up.contains(&rest[0]) => out(false, "", ""),
                _ => out(true, "", ""),
            })
        }
    }

    /// A fake host: its login, its Claude binary, a worktree and a fake docker.
    struct Host {
        _dir: TempDir,
        tmp: PathBuf,
        login: PathBuf,
        binary: PathBuf,
        worktree: PathBuf,
        bare: PathBuf,
        docker: FakeDocker,
        now: i64,
        iso: Isolated<FakeDocker>,
    }

    fn host() -> Host {
        let dir = TempDir::new().unwrap();
        let tmp = dir.path().to_path_buf();
        let mut paths = Paths::under(&tmp);
        let claude_dir = tmp.join("host-claude");
        std::fs::create_dir_all(&claude_dir).unwrap();
        paths.home.root = claude_dir.clone();
        let login_path = claude_dir.join(".credentials.json");
        let now = now_ms();
        std::fs::write(&login_path, login(now + 8 * HOUR_MS).to_string()).unwrap();
        paths.isolated.dir = tmp.join("iso");
        let binary = tmp.join("bin").join("claude");
        std::fs::create_dir_all(binary.parent().unwrap()).unwrap();
        std::fs::write(&binary, "#!/bin/sh\n").unwrap();
        paths.claude_bin = binary.clone();

        let bare = tmp.join("projects").join("awm").join(".bare");
        std::fs::create_dir_all(bare.join("worktrees").join("x")).unwrap();
        std::fs::write(bare.join("worktrees").join("x").join("commondir"), "../..\n").unwrap();
        let worktree = tmp.join("projects").join("awm").join("x");
        std::fs::create_dir_all(&worktree).unwrap();
        std::fs::write(worktree.join(".git"), format!("gitdir: {}\n", bare.join("worktrees").join("x").display())).unwrap();

        let docker = FakeDocker::default();
        let env = vec![("SSH_AUTH_SOCK".into(), "/tmp/agent.sock".into()), ("PATH".into(), "/usr/bin".into())];
        let iso = Isolated::new(&paths, docker.clone(), 1000, env);
        Host { _dir: dir, tmp, login: login_path, binary, worktree, bare, docker, now, iso }
    }

    fn req(name: &str, prompt: &str) -> StartRequest {
        StartRequest {
            project: Some("awm".into()),
            scope: Some("x".into()),
            name: name.into(),
            prompt: prompt.into(),
            model: Some("opus".into()),
            effort: Some("high".into()),
        }
    }

    async fn start(h: &Host, name: &str, prompt: &str, refusal: Option<String>) -> Value {
        let wt = h.worktree.clone();
        h.iso.start_isolated(&req(name, prompt), refusal, "node-a", |_, _| async move { Ok(wt) }).await
    }

    fn token_of(h: &Host, sid: &str) -> String {
        let mcp: Value = serde_json::from_str(&std::fs::read_to_string(h.iso.config_dir(sid).join("awm-mcp.json")).unwrap()).unwrap();
        mcp["mcpServers"]["awm"]["headers"]["X-Awm-Caller-Token"].as_str().unwrap().to_string()
    }

    // --- the login copy ---------------------------------------------------------

    #[test]
    fn test_the_copy_keeps_the_access_token_and_drops_the_refresh_token() {
        let copy = strip_login(&login(5)).unwrap();
        assert_eq!(copy["claudeAiOauth"]["accessToken"], "fake-access");
        assert!(copy["claudeAiOauth"].get("refreshToken").is_none());
        assert_eq!(strip_login(&json!({"claudeAiOauth": {"refreshToken": "r"}})), None);
        assert_eq!(strip_login(&Value::Null), None);
    }

    #[tokio::test]
    async fn test_sync_writes_a_private_copy_then_leaves_it_alone() {
        let h = host();
        let dest = h.tmp.join("c").join(".credentials.json");
        assert_eq!(sync_login(&dest, &h.login, now_ms()).unwrap(), LoginSync::Written);
        assert_eq!(std::fs::metadata(&dest).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(!std::fs::read_to_string(&dest).unwrap().contains("fake-refresh"));
        assert_eq!(sync_login(&dest, &h.login, now_ms()).unwrap(), LoginSync::Unchanged);
    }

    #[tokio::test]
    async fn test_a_missing_or_expired_host_login_signs_the_copy_out() {
        let h = host();
        let dest = h.tmp.join("c").join(".credentials.json");
        sync_login(&dest, &h.login, now_ms()).unwrap();
        assert_eq!(sync_login(&dest, &h.login, h.now + 9 * HOUR_MS).unwrap(), LoginSync::SignedOut);
        assert!(!dest.exists());
        sync_login(&dest, &h.login, now_ms()).unwrap();
        std::fs::remove_file(&h.login).unwrap();
        assert_eq!(sync_login(&dest, &h.login, now_ms()).unwrap(), LoginSync::SignedOut);
        assert!(!dest.exists());
        assert!(!h.login.exists());
    }

    #[tokio::test]
    async fn test_a_host_refresh_reaches_every_copy() {
        let h = host();
        let out = start(&h, "x", "", None).await;
        let sid = out["claude_session_id"].as_str().unwrap().to_string();
        let copy = h.iso.config_dir(&sid).join(".credentials.json");
        let mut fresh = login(h.now + 16 * HOUR_MS);
        fresh["claudeAiOauth"]["accessToken"] = json!("fake-access-2");
        std::fs::write(&h.login, fresh.to_string()).unwrap();
        assert_eq!(h.iso.sync_all().await.unwrap(), vec![(sid, LoginSync::Written)]);
        let written: Value = serde_json::from_str(&std::fs::read_to_string(copy).unwrap()).unwrap();
        assert_eq!(written["claudeAiOauth"]["accessToken"], "fake-access-2");
    }

    #[test]
    fn a_container_copy_holds_only_the_login_keys() {
        let mut host = login(5);
        host["claudeAiOauth"]["rateLimitTier"] = json!("tier-x");
        host["claudeAiOauth"]["someNewSecret"] = json!("fake-other");
        host["mcpOAuth"] = json!({"x": {"accessToken": "fake-mcp"}});
        let copy = strip_login(&host).unwrap();
        let root: Vec<&String> = copy.as_object().unwrap().keys().collect();
        assert_eq!(root, ["claudeAiOauth"]);
        let keys: Vec<&str> = copy["claudeAiOauth"].as_object().unwrap().keys().map(String::as_str).collect();
        assert_eq!(keys, LOGIN_KEYS);
        assert!(!copy.to_string().contains("refresh") && !copy.to_string().contains("fake-other"));
    }

    #[test]
    fn a_copy_is_written_0600_through_a_temp_file_and_a_rename() {
        let dir = TempDir::new().unwrap();
        let dest = dir.path().join("c").join(".credentials.json");
        std::fs::create_dir_all(dest.parent().unwrap()).unwrap();
        std::fs::write(&dest, "old").unwrap();
        std::fs::set_permissions(&dest, std::fs::Permissions::from_mode(0o644)).unwrap();
        let other = dir.path().join("c").join("hardlink");
        std::fs::hard_link(&dest, &other).unwrap();
        write_private(&dest, "new").unwrap();
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "new");
        assert_eq!(std::fs::read_to_string(&other).unwrap(), "old", "the rename replaced the file, not its bytes");
        assert_eq!(std::fs::metadata(&dest).unwrap().permissions().mode() & 0o777, 0o600);
        let names: Vec<String> =
            std::fs::read_dir(dest.parent().unwrap()).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
        assert!(names.iter().all(|n| !n.contains(".tmp.")), "no temp file is left: {names:?}");
    }

    #[test]
    fn a_copy_is_deleted_when_the_host_is_signed_out_or_expired() {
        let dir = TempDir::new().unwrap();
        let (dest, host) = (dir.path().join("c").join(".credentials.json"), dir.path().join("host.json"));
        let now = 1_000 * HOUR_MS;
        let cases = [json!({}), json!({"claudeAiOauth": {"accessToken": ""}}), login(now), login(now - 1)];
        for case in cases {
            std::fs::write(&host, login(now + HOUR_MS).to_string()).unwrap();
            assert_eq!(sync_login(&dest, &host, now).unwrap(), LoginSync::Written);
            std::fs::write(&host, case.to_string()).unwrap();
            assert_eq!(sync_login(&dest, &host, now).unwrap(), LoginSync::SignedOut, "{case}");
            assert!(!dest.exists(), "{case}");
        }
        std::fs::write(&host, login(now + HOUR_MS).to_string()).unwrap();
        sync_login(&dest, &host, now).unwrap();
        std::fs::write(&host, "{torn").unwrap();
        assert_eq!(sync_login(&dest, &host, now).unwrap(), LoginSync::SignedOut);
        assert!(!dest.exists());
    }

    // --- caller tokens ----------------------------------------------------------

    #[tokio::test]
    async fn test_a_token_is_kept_only_as_its_digest_and_dies_with_revoke() {
        let h = host();
        let rec = json!({"claude_session_id": "s1", "name": "n"}).as_object().unwrap().clone();
        let token = h.iso.mint(rec).await.unwrap();
        assert_eq!(token.len(), 43, "token_urlsafe(32)");
        assert!(token.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'));
        let path = h.iso.tokens().path();
        assert!(!std::fs::read_to_string(&path).unwrap().contains(&token));
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert_eq!(h.iso.caller_of_token(Some(&token)).await.unwrap()["name"], "n");
        assert!(h.iso.caller_of_token(Some("guess")).await.is_none() && h.iso.caller_of_token(None).await.is_none());
        assert_eq!(h.iso.revoke("s1").await.unwrap(), 1);
        assert!(h.iso.caller_of_token(Some(&token)).await.is_none());
    }

    #[test]
    fn base64url_matches_pythons_urlsafe_encoding() {
        assert_eq!(base64url(b""), "");
        assert_eq!(base64url(b"f"), "Zg");
        assert_eq!(base64url(b"fo"), "Zm8");
        assert_eq!(base64url(b"foo"), "Zm9v");
        assert_eq!(base64url(&[0xfb, 0xff]), "-_8");
    }

    // --- the container ----------------------------------------------------------

    #[test]
    fn test_only_rootless_docker_is_accepted() {
        assert!(matches!(docker_host(Some("unix:///var/run/docker.sock"), 1000), Err(IsolationError::Refused(_))));
        assert!(docker_host(Some("/run/docker.sock"), 1000).is_err());
        assert_eq!(docker_host(None, 1000).unwrap(), "unix:///run/user/1000/docker.sock");
    }

    #[tokio::test]
    async fn test_the_container_sees_the_binary_read_only_and_no_host_secrets() {
        let h = host();
        let spec = Spec {
            prompt: "go".into(),
            ..Spec::new(&h.iso.knobs, "00000000-0000-0000-0000-000000000000", "n", &h.worktree, &h.binary)
        };
        let argv = run_argv(&spec, &h.iso.knobs.dir);
        let mounts: Vec<&str> = argv.windows(2).filter(|w| w[0] == "-v").map(|w| w[1].as_str()).collect();
        assert!(mounts.contains(&format!("{}:{CLAUDE_IN}:ro", h.binary.display()).as_str()));
        assert!(mounts.contains(&format!("{0}:{0}", h.worktree.display()).as_str()));
        let bare = std::fs::canonicalize(&h.bare).unwrap();
        assert!(mounts.contains(&format!("{0}:{0}", bare.display()).as_str()), "{mounts:?}");
        let sources: Vec<&str> = mounts.iter().map(|m| m.split(':').next().unwrap()).collect();
        assert!(!sources.iter().any(|s| s.ends_with(".ssh") || s.ends_with(".claude") || s.ends_with("host-claude")));
        for flag in ["--cap-drop", "--security-opt", "--cpus", "--memory", "--pids-limit"] {
            assert!(argv.iter().any(|a| a == flag), "{flag}");
        }
        assert!(argv.iter().any(|a| a == "IS_SANDBOX=1") && argv.iter().any(|a| a == "--strict-mcp-config"));
        assert_eq!(argv[argv.len() - 2..], ["--", "go"]);
    }

    #[tokio::test]
    async fn test_start_runs_the_container_with_a_token_the_gateway_can_check() {
        let h = host();
        let out = start(&h, "iso", "hello", None).await;
        assert!(out["ok"] == true && out["isolated"] == true, "{out}");
        assert!(out["session_id"].as_str().unwrap().starts_with("cc"));
        assert_eq!(h.docker.verbs(), ["image", "rm", "run"]);
        let (_, env, _) = h.docker.last();
        assert!(!env.iter().any(|(k, _)| k == "SSH_AUTH_SOCK"));
        assert!(env.iter().any(|(k, v)| k == "DOCKER_HOST" && v.starts_with("unix:///run/user/")));
        let sid = out["claude_session_id"].as_str().unwrap();
        let cfg = h.iso.config_dir(sid);
        let who = h.iso.caller_answer(&json!(token_of(&h, sid))).await;
        assert!(who["known"] == true && who["name"] == "iso" && who["trust"] == "untrusted", "{who}");
        assert!(truthy(&who["tools"]["journal"]));
        let reflection: Vec<&str> = who["tools"]["reflection"].as_array().unwrap().iter().filter_map(Value::as_str).collect();
        assert!(!reflection.contains(&"message") && !reflection.contains(&"start_other"));
        assert!(!std::fs::read_to_string(cfg.join(".credentials.json")).unwrap().contains("refreshToken"));
        let state: Value = serde_json::from_str(&std::fs::read_to_string(cfg.join(".claude.json")).unwrap()).unwrap();
        assert_eq!(state["projects"][h.worktree.display().to_string()]["hasTrustDialogAccepted"], true);
    }

    #[tokio::test]
    async fn test_the_contract_verbs_reach_an_isolated_session_by_its_id() {
        let h = host();
        let out = start(&h, "iso", "", None).await;
        let sid = out["claude_session_id"].as_str().unwrap();
        let rec = h.iso.find("iso").await.unwrap();
        assert_eq!(h.iso.find(&sid.replace('-', "")).await.as_ref(), Some(&rec));
        assert_eq!(h.iso.find_answer(&rec).await["matches"][0]["kind"], "isolated");
        assert_eq!(h.iso.status_answer(&rec).await["state"], "running");
        assert_eq!(h.iso.send(&rec, "hi", true).await.unwrap()["ok"], true);
        assert_eq!(h.docker.last().0.last().unwrap(), "Enter");
        assert_eq!(h.iso.send(&rec, "a\nb", true).await.unwrap()["ok"], false);
        assert_eq!(h.iso.interrupt(&rec).await.unwrap()["ok"], true);
        assert_eq!(set_refusal()["unsupported"], true);
        let stopped = h.iso.stop_answer(&rec).await.unwrap();
        assert!(stopped["stopped"] == true && stopped["kept"] == true);
        assert_eq!(h.iso.status_answer(&rec).await["state"], "stopped");
        assert_eq!(h.iso.resume_answer(&rec, "").await.unwrap()["resumed"], true);
        let argv = h.docker.last().0;
        assert!(argv.iter().any(|a| a == "--resume") && !argv.iter().any(|a| a == "--session-id"));
    }

    #[tokio::test]
    async fn test_stop_revokes_the_token_and_keeps_the_transcript() {
        let h = host();
        let out = start(&h, "x", "", None).await;
        let sid = out["claude_session_id"].as_str().unwrap();
        let token = token_of(&h, sid);
        let rec = h.iso.find(sid).await.unwrap();
        h.iso.stop_answer(&rec).await.unwrap();
        assert_eq!(h.iso.caller_answer(&json!(token)).await, json!({"ok": true, "known": false}));
        assert!(h.iso.session_dir(sid).exists());
    }

    #[tokio::test]
    async fn test_no_host_login_refuses_the_start_and_leaves_no_token() {
        let h = host();
        std::fs::remove_file(&h.login).unwrap();
        let out = start(&h, "x", "", None).await;
        assert!(out["ok"] == false && out["reason"].as_str().unwrap().contains("login"), "{out}");
        assert!(!h.docker.verbs().iter().any(|v| v == "run"));
        assert!(h.iso.tokens().read().is_empty());
    }

    #[tokio::test]
    async fn test_a_failed_docker_run_leaves_no_token() {
        let h = host();
        h.docker.0.lock().unwrap().fail_run = true;
        let out = start(&h, "x", "", None).await;
        assert!(out["ok"] == false && out["reason"].as_str().unwrap().contains("no such image"), "{out}");
        assert!(h.iso.tokens().read().is_empty());
    }

    #[tokio::test]
    async fn test_a_peer_that_is_not_domestic_cannot_start_one() {
        let h = host();
        let refusal = crate::app::caller_refusal(Some("peer"), || unreachable!("a bare peer names no node"));
        let out = start(&h, "x", "", refusal).await;
        assert!(out["ok"] == false && out["reason"].as_str().unwrap().contains("unverified"), "{out}");
        assert!(h.docker.verbs().is_empty());
    }

    #[tokio::test]
    async fn test_list_shows_live_isolated_sessions_beside_the_rest() {
        let h = host();
        let out = start(&h, "x", "", None).await;
        let ids = |rows: Vec<Value>| rows.iter().map(|r| r["session_id"].as_str().unwrap().to_string()).collect::<Vec<_>>();
        assert_eq!(ids(h.iso.list_rows(None, None, false).await), [out["session_id"].as_str().unwrap()]);
        assert!(h.iso.list_rows(Some("other"), None, false).await.is_empty());
        let rec = h.iso.find(out["claude_session_id"].as_str().unwrap()).await.unwrap();
        h.iso.stop_answer(&rec).await.unwrap();
        assert!(h.iso.list_rows(None, None, false).await.is_empty());
        assert_eq!(h.iso.list_rows(None, None, true).await.len(), 1);
    }

    #[tokio::test]
    async fn an_isolated_start_with_no_scope_is_refused() {
        let h = host();
        for (project, scope) in [(Some("awm"), None), (None, None), (Some("awm"), Some(""))] {
            let r = StartRequest { project: project.map(Into::into), scope: scope.map(Into::into), ..req("lobby-ab12", "") };
            let out = h.iso.start_isolated(&r, None, "node-a", |_, _| async { unreachable!("no worktree is resolved") }).await;
            assert_eq!(out, json!({"ok": false, "reason": NEEDS_SCOPE}));
        }
        assert!(h.docker.verbs().is_empty() && h.iso.records().await.is_empty());
    }

    #[tokio::test]
    async fn a_live_name_is_not_started_twice() {
        let h = host();
        start(&h, "iso", "", None).await;
        let out = start(&h, "iso", "", None).await;
        assert_eq!(out, json!({"ok": false, "reason": "a live isolated session is already named 'iso'"}));
    }

    #[tokio::test]
    async fn the_first_start_builds_a_missing_image_from_the_dockerfile() {
        let h = host();
        h.docker.0.lock().unwrap().no_image = true;
        start(&h, "a", "", None).await;
        assert_eq!(h.docker.verbs(), ["image", "build", "rm", "run"]);
        let build = h.docker.0.lock().unwrap().calls[1].clone();
        assert_eq!(build.0, ["docker", "build", "-t", "awm-isolated:1", "-"]);
        assert_eq!(build.2.as_deref(), Some(DOCKERFILE.as_bytes()));
        start(&h, "b", "", None).await;
        assert_eq!(h.docker.verbs()[4..], ["rm", "run"], "the image is checked once per process");
    }

    #[tokio::test]
    async fn a_failed_image_build_refuses_the_start_and_leaves_no_token() {
        #[derive(Clone, Default)]
        struct NoBuild(FakeDocker);
        impl Docker for NoBuild {
            async fn run(&self, argv: Vec<String>, env: Vec<(String, String)>, stdin: Option<Vec<u8>>, t: Duration) -> io::Result<RunOutput> {
                match argv[1].as_str() {
                    "image" | "build" => {
                        self.0.0.lock().unwrap().calls.push((argv, env, stdin));
                        Ok(out(false, "", "pull access denied"))
                    }
                    _ => self.0.run(argv, env, stdin, t).await,
                }
            }
        }
        let h = host();
        let fake = NoBuild::default();
        let mut paths = Paths::under(&h.tmp);
        paths.home.root = h.login.parent().unwrap().to_path_buf();
        paths.isolated.dir = h.iso.knobs.dir.clone();
        paths.claude_bin = h.binary.clone();
        let iso = Isolated::new(&paths, fake.clone(), 1000, Vec::new());
        let wt = h.worktree.clone();
        let out = iso.start_isolated(&req("a", ""), None, "node-a", |_, _| async move { Ok(wt) }).await;
        assert!(out["reason"].as_str().unwrap().contains("docker build of awm-isolated:1 failed: pull access denied"), "{out}");
        assert_eq!(fake.0.verbs(), ["image", "build"]);
        assert!(iso.tokens().read().is_empty());
    }

    #[tokio::test]
    async fn the_watch_syncs_only_when_the_host_login_changes() {
        let h = host();
        let out = start(&h, "x", "", None).await;
        let sid = out["claude_session_id"].as_str().unwrap().to_string();
        let mut seen = None;
        assert!(h.iso.watch_once(&mut seen).await.is_empty(), "the start already wrote the copy");
        let mut fresh = login(h.now + 16 * HOUR_MS);
        fresh["claudeAiOauth"]["accessToken"] = json!("fake-access-2");
        std::fs::write(&h.login, fresh.to_string()).unwrap();
        let copy = h.iso.config_dir(&sid).join(".credentials.json");
        std::fs::remove_file(&copy).unwrap();
        assert_eq!(h.iso.watch_once(&mut seen).await, vec![(sid.clone(), LoginSync::Written)]);
        std::fs::remove_file(&copy).unwrap();
        assert!(h.iso.watch_once(&mut seen).await.is_empty(), "an unchanged stamp syncs nothing");
        std::fs::remove_file(&h.login).unwrap();
        assert_eq!(h.iso.watch_once(&mut seen).await, vec![(sid, LoginSync::SignedOut)]);
    }

    #[test]
    fn the_reflection_whitelist_is_the_gateways_ceiling() {
        let gateway = include_str!("../../../crates/awm-gateway/src/isolated.rs");
        let line = gateway.lines().find(|l| l.starts_with("pub const OWN_SESSION_VERBS")).expect("the gateway's ceiling");
        let list = line.split_once("= [").unwrap().1.trim_end_matches("];");
        let ceiling: Vec<&str> = list.split(',').map(|v| v.trim().trim_matches('"')).collect();
        assert_eq!(whitelist()["reflection"], ceiling.as_slice());
    }
}
