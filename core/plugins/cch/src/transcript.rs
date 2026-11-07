//! Reads a session's transcript jsonl for what it took in and whether a foreground tool call is open.
//!
//! The file is appended out of timestamp order, so no question here keys off the order of lines,
//! and every question answers `None` for "could not tell".

use std::collections::{HashMap, HashSet};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::SystemTime;

use awm_harness::claude::home::ClaudeHome;
use regex::Regex;
use serde_json::{Map, Value};

type Object = Map<String, Value>;

/// How far back the first poll reaches: enough to cover the turn in progress, so a tool call
/// already running when the tail opened is not missed.
pub const MAX_CATCHUP_BYTES: u64 = 1024 * 1024;

const QUEUE_CONSUMES: [&str; 2] = ["remove", "popAll"];

// A local slash command such as /clear or /mcp is recorded only in this form; /compact writes a
// plain "/compact" entry as well.
static COMMAND_ENTRY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?s)\A<command-name>(/\S+)</command-name>.*?<command-args>(.*?)</command-args>")
        .expect("valid regex")
});
static COMMAND_STDOUT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?s)\A<local-command-stdout>(.*)</local-command-stdout>\z").expect("valid regex")
});
static ANSI: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\x1b\[[0-9;]*[A-Za-z]").expect("valid regex"));

/// The body of a paste envelope `<pasted_content id="X">\n…\n</pasted_content id="X">`.
///
/// Claude Code records a long pasted prompt this way in the queue entry and the user entry alike;
/// matched literally, a resume never looked delivered. The closing id must equal the opening one.
fn paste_body(text: &str) -> Option<&str> {
    let rest = text.strip_prefix("<pasted_content id=\"")?;
    let id_len = rest.find('"')?;
    let id = &rest[..id_len];
    let after_id = rest[id_len..].strip_prefix("\">\n")?;
    let suffix = format!("\n</pasted_content id=\"{id}\">");
    after_id.strip_suffix(suffix.as_str())
}

/// `text` stripped, with any paste envelope or command envelope removed.
fn unwrapped(text: &str) -> String {
    let text = text.trim();
    if let Some(body) = paste_body(text) {
        return body.trim().to_string();
    }
    if let Some(c) = COMMAND_ENTRY.captures(text) {
        return format!("{} {}", &c[1], c[2].trim()).trim().to_string();
    }
    text.to_string()
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Python's `str()` of a JSON value, as far as ids and command names need it.
fn py_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

/// `str(v) if v else None`.
fn truthy_str(v: Option<&Value>) -> Option<String> {
    truthy(v).then(|| py_str(v))
}

fn str_of(v: Option<&Value>) -> &str {
    v.and_then(Value::as_str).unwrap_or("")
}

/// `(entry.get(key) or {})` for a key that should hold an object.
fn object_of<'a>(entry: &'a Object, key: &str) -> Option<&'a Object> {
    entry.get(key).and_then(Value::as_object)
}

/// The JSON object in `path`, or `None` when it is absent, unreadable or not an object.
pub(crate) async fn read_record(path: &Path) -> Option<Object> {
    let bytes = tokio::fs::read(path).await.ok()?;
    match serde_json::from_slice::<Value>(&bytes).ok()? {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

fn now_wall_ms() -> i64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// The session id Claude Code is currently writing under for `repl_pid`, or `None`.
///
/// Re-read rather than cached: `/clear` replaces a conversation in place and mints a new id.
pub async fn session_id_for(home: &ClaudeHome, repl_pid: u32) -> Option<String> {
    session_and_cwd_for(home, repl_pid).await.0
}

/// [`session_id_for`] plus the cwd the record names; `None` for each unknown.
///
/// The cwd decides which directory the transcript lives in, so a change in it is the first sign
/// that a `/cd` is about to move the file.
pub async fn session_and_cwd_for(home: &ClaudeHome, repl_pid: u32) -> (Option<String>, Option<String>) {
    match read_record(&home.session_record(repl_pid)).await {
        Some(record) => (truthy_str(record.get("sessionId")), truthy_str(record.get("cwd"))),
        None => (None, None),
    }
}

fn find_path(projects: &Path, session_id: &str) -> Option<PathBuf> {
    if session_id.is_empty() {
        return None;
    }
    let name = format!("{session_id}.jsonl");
    let mut best: Option<(Option<SystemTime>, PathBuf)> = None;
    for entry in std::fs::read_dir(projects).ok()?.flatten() {
        let candidate = entry.path().join(&name);
        if !candidate.exists() {
            continue;
        }
        let mtime = candidate.metadata().and_then(|m| m.modified()).ok();
        if best.as_ref().is_none_or(|(m, _)| mtime > *m) {
            best = Some((mtime, candidate));
        }
    }
    best.map(|(_, p)| p)
}

/// The transcript file for `session_id`, found by search rather than rule.
///
/// Globs `<projects>/*/<session_id>.jsonl` instead of deriving Claude Code's cwd slug, which a
/// `/cd` would break. Should two directories hold the id, the one written last wins.
pub async fn path_for(home: &ClaudeHome, session_id: &str) -> Option<PathBuf> {
    let projects = home.projects();
    let session_id = session_id.to_string();
    tokio::task::spawn_blocking(move || find_path(&projects, &session_id)).await.ok()?
}

async fn exists(path: &Path) -> bool {
    tokio::fs::try_exists(path).await.unwrap_or(false)
}

/// The entry's own timestamp in epoch ms, or 0 if it has none.
///
/// Zero means "cannot be dated", so a caller asking for events since some moment does not count
/// it. That is the safe direction: an identical line from an earlier compaction must not answer.
fn stamp_ms(entry: &Object) -> i64 {
    let raw = str_of(entry.get("timestamp"));
    if raw.is_empty() {
        return 0;
    }
    let iso = raw.replace('Z', "+00:00");
    if let Ok(t) = chrono::DateTime::parse_from_rfc3339(&iso) {
        return t.timestamp_millis();
    }
    for fmt in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"] {
        if let Ok(naive) = chrono::NaiveDateTime::parse_from_str(&iso, fmt) {
            // Python's astimezone reads a naive time as local time.
            return naive
                .and_local_timezone(chrono::Local)
                .earliest()
                .map(|t| t.timestamp_millis())
                .unwrap_or(0);
        }
    }
    0
}

/// The user-visible text of a `user` entry, or `""` for a tool result.
fn text_of(entry: &Object) -> String {
    match object_of(entry, "message").and_then(|m| m.get("content")) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(blocks)) => blocks
            .iter()
            .filter_map(Value::as_object)
            .filter(|b| str_of(b.get("type")) == "text")
            .map(|b| str_of(b.get("text")))
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

/// What one read of the file produced: the bytes after the offset, and where the next read starts.
struct Chunk {
    bytes: Vec<u8>,
    offset: u64,
    /// The file shrank under us, so it was re-read from the top.
    restarted: bool,
}

fn read_chunk(path: &Path, offset: Option<u64>) -> std::io::Result<Chunk> {
    let size = std::fs::metadata(path)?.len();
    let mut file = std::fs::File::open(path)?;
    let (start, catch_up, restarted) = match offset {
        // First poll of a live session: start at the end, and only walk back far enough to catch
        // an injection in progress.
        None => {
            let start = size.saturating_sub(MAX_CATCHUP_BYTES);
            (start, start > 0, false)
        }
        // Truncated or replaced under us; re-read from the top.
        Some(o) if o > size => (0, false, true),
        Some(o) => (o, false, false),
    };
    file.seek(SeekFrom::Start(start))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    let offset = start + bytes.len() as u64;
    if catch_up {
        // Drop the partial line we landed inside.
        let cut = bytes.iter().position(|b| *b == b'\n').map_or(bytes.len(), |i| i + 1);
        bytes.drain(..cut);
    }
    Ok(Chunk { bytes, offset, restarted })
}

/// Follows one session's transcript from wherever it stood when opened.
///
/// Reads are incremental: the byte offset is kept between polls, and only text the caller asked
/// about is retained, so following a session for an hour costs fixed memory.
#[derive(Debug, Clone)]
pub struct Tail {
    home: ClaudeHome,
    repl_pid: u32,
    session_id: Option<String>,
    cwd: Option<String>,
    path: Option<PathBuf>,
    offset: Option<u64>,
    partial: Vec<u8>,
    ever_read: bool,
    watched: HashSet<String>,
    queued: HashSet<String>,
    consumed: HashSet<String>,
    // Dated rather than set membership: both are read as "did this happen since we typed?", and a
    // first read reaches back far enough to see an earlier compaction's start line and boundary.
    started: HashMap<String, i64>,
    compacted_at: i64,
    open_tools: HashSet<String>,
    landed_at: HashMap<String, i64>,
    consumed_at: HashMap<String, i64>,
    // The line being typed right now and when typing began: the one owner a content-less
    // enqueue can have.
    expecting: Option<(String, i64)>,
    // A local command's output arrives as a separate entry under the same promptId.
    command_of_prompt: HashMap<String, String>,
    output: HashMap<String, (i64, String)>,
}

impl Tail {
    /// A tail on `repl_pid`'s transcript whose first poll starts near the end of the file.
    pub fn new(home: ClaudeHome, repl_pid: u32) -> Self {
        Self::with_offset(home, repl_pid, None)
    }

    /// A tail whose first poll reads the whole file.
    pub fn from_start(home: ClaudeHome, repl_pid: u32) -> Self {
        Self::with_offset(home, repl_pid, Some(0))
    }

    fn with_offset(home: ClaudeHome, repl_pid: u32, offset: Option<u64>) -> Self {
        Self {
            home,
            repl_pid,
            session_id: None,
            cwd: None,
            path: None,
            offset,
            partial: Vec::new(),
            ever_read: false,
            watched: HashSet::new(),
            queued: HashSet::new(),
            consumed: HashSet::new(),
            started: HashMap::new(),
            compacted_at: 0,
            open_tools: HashSet::new(),
            landed_at: HashMap::new(),
            consumed_at: HashMap::new(),
            expecting: None,
            command_of_prompt: HashMap::new(),
            output: HashMap::new(),
        }
    }

    /// The pid the tail currently follows.
    pub fn repl_pid(&self) -> u32 {
        self.repl_pid
    }

    /// Track `text` from here on; call before the text is typed in.
    pub fn watch(&mut self, text: &str) {
        self.watched.insert(text.trim().to_string());
    }

    /// `text` is being typed in now; call immediately before the write.
    ///
    /// Only the line named here, and only from `now_ms` (default: now), can claim a content-less
    /// enqueue, so a paste somebody else makes while we wait is never taken for ours.
    pub fn expect(&mut self, text: &str, now_ms: Option<i64>) {
        let text = text.trim().to_string();
        self.watched.insert(text.clone());
        self.expecting = Some((text, now_ms.unwrap_or_else(now_wall_ms)));
    }

    fn note_landed(&mut self, text: &str, stamp: i64, queued: bool) {
        if queued {
            self.queued.insert(text.to_string());
        } else {
            self.consumed.insert(text.to_string());
            self.queued.remove(text);
            let at = self.consumed_at.entry(text.to_string()).or_insert(0);
            *at = (*at).max(stamp);
        }
        let at = self.landed_at.entry(text.to_string()).or_insert(0);
        *at = (*at).max(stamp);
    }

    /// The expected line a content-less enqueue stamped `stamp` belongs to; one paste, one enqueue.
    fn paste_owner(&mut self, stamp: i64) -> String {
        match &self.expecting {
            Some((_, since)) if stamp != 0 && stamp >= *since => {
                self.expecting.take().map(|(text, _)| text).unwrap_or_default()
            }
            _ => String::new(),
        }
    }

    fn note_output(&mut self, line: Option<String>, entry: &Object, text: &str) {
        if let Some(line) = line.filter(|l| !l.is_empty() && self.watched.contains(l)) {
            self.output.insert(line, (stamp_ms(entry), ANSI.replace_all(text, "").trim().to_string()));
        }
    }

    async fn resolve(&mut self) -> Option<PathBuf> {
        let (session_id, cwd) = session_and_cwd_for(&self.home, self.repl_pid).await;
        if let Some(session_id) = session_id {
            if self.session_id.is_none() {
                self.path = path_for(&self.home, &session_id).await;
                self.session_id = Some(session_id);
                self.cwd = cwd;
            } else if self.session_id.as_deref() != Some(session_id.as_str()) {
                self.follow_clear(session_id, cwd).await;
            } else {
                let vanished = match &self.path {
                    None => true,
                    Some(p) => !exists(p).await,
                };
                if vanished || cwd != self.cwd {
                    // A /cd mid-wait: same id, and Claude Code moves the file into the new cwd's
                    // slug directory. A rename keeps the content, so the offset stands. The record
                    // can name the new cwd before the file has moved, so a vanished path also
                    // triggers the search.
                    self.cwd = cwd;
                    self.path = path_for(&self.home, &session_id).await;
                }
            }
        }
        self.path.clone()
    }

    /// Move to the new conversation a /clear started, from its first line.
    ///
    /// The old file is read to its end first: Claude Code writes the queue's `dequeue` there a few
    /// ms before the record names the new id, so a tail that switched without it would hold our
    /// command as queued for good. A clear runs between turns, so no foreground call carries over.
    async fn follow_clear(&mut self, session_id: String, cwd: Option<String>) {
        if let (Some(path), Some(_)) = (self.path.clone(), self.offset) {
            self.read(path).await;
        }
        self.path = path_for(&self.home, &session_id).await;
        self.session_id = Some(session_id);
        self.cwd = cwd;
        self.offset = Some(0);
        self.partial.clear();
        self.open_tools.clear();
    }

    /// The session id the tail last read under, or `None` before the first poll.
    pub fn session_id(&self) -> Option<&str> {
        self.session_id.as_deref()
    }

    /// Ingest whatever has been appended since the last call.
    ///
    /// `false` means the transcript could not be read this time, which is evidence about the file,
    /// not about the session.
    pub async fn poll(&mut self) -> bool {
        match self.resolve().await {
            Some(path) => self.read(path).await,
            None => false,
        }
    }

    async fn read(&mut self, path: PathBuf) -> bool {
        let offset = self.offset;
        let chunk = match tokio::task::spawn_blocking(move || read_chunk(&path, offset)).await {
            Ok(Ok(chunk)) => chunk,
            _ => return false,
        };
        if chunk.restarted {
            self.partial.clear();
        }
        self.offset = Some(chunk.offset);
        self.ever_read = true;
        let mut data = std::mem::take(&mut self.partial);
        data.extend_from_slice(&chunk.bytes);
        // Split in bytes: the last piece is a line still being written, and a multi-byte char cut
        // at the chunk edge stays whole in it.
        let tail_start = data.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
        self.partial = data.split_off(tail_start);
        for line in data.split(|b| *b == b'\n') {
            let line = String::from_utf8_lossy(line);
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            if let Ok(Value::Object(entry)) = serde_json::from_str::<Value>(line) {
                self.ingest(&entry);
            }
        }
        true
    }

    fn ingest(&mut self, entry: &Object) {
        // A subagent's own turns are written into the same file. They are not this session's
        // foreground work, and the Agent call that started one is itself an unmatched tool_use.
        if truthy(entry.get("isSidechain")) {
            return;
        }
        let kind = str_of(entry.get("type"));
        let subtype = str_of(entry.get("subtype"));
        match kind {
            "assistant" => self.ingest_assistant(entry),
            "user" => self.ingest_user(entry),
            "system" if subtype == "compact_boundary" => {
                self.compacted_at = self.compacted_at.max(stamp_ms(entry));
            }
            "system" if subtype == "local_command" => {
                // /cd reports this way, naming its command in `commandRun`.
                let content = if truthy(entry.get("content")) { py_str(entry.get("content")) } else { String::new() };
                let stdout = COMMAND_STDOUT.captures(content.trim()).map(|c| c[1].to_string());
                if let (Some(run), Some(stdout)) = (object_of(entry, "commandRun"), stdout) {
                    let part = |k: &str| if truthy(run.get(k)) { py_str(run.get(k)) } else { String::new() };
                    let line = format!("/{} {}", part("command"), part("args")).trim().to_string();
                    self.note_output(Some(line), entry, &stdout);
                }
            }
            "queue-operation" => self.ingest_queue(entry),
            "attachment" => {
                let Some(attachment) = object_of(entry, "attachment") else { return };
                if str_of(attachment.get("type")) == "queued_command" {
                    let text = unwrapped(str_of(attachment.get("prompt")));
                    if self.watched.contains(&text) {
                        self.note_landed(&text, stamp_ms(entry), false);
                    }
                }
            }
            _ => {}
        }
    }

    fn content_blocks(entry: &Object) -> impl Iterator<Item = &Object> {
        object_of(entry, "message")
            .and_then(|m| m.get("content"))
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_object)
    }

    fn ingest_assistant(&mut self, entry: &Object) {
        for block in Self::content_blocks(entry) {
            if str_of(block.get("type")) == "tool_use" && truthy(block.get("id")) {
                self.open_tools.insert(py_str(block.get("id")));
            }
        }
    }

    fn ingest_user(&mut self, entry: &Object) {
        for block in Self::content_blocks(entry) {
            if str_of(block.get("type")) == "tool_result" {
                self.open_tools.remove(&py_str(block.get("tool_use_id")));
            }
        }
        let raw = text_of(entry);
        let raw = raw.trim();
        if let Some(stdout) = COMMAND_STDOUT.captures(raw) {
            let prompt = if truthy(entry.get("promptId")) { py_str(entry.get("promptId")) } else { String::new() };
            let line = self.command_of_prompt.get(&prompt).cloned();
            self.note_output(line, entry, &stdout[1]);
            return;
        }
        let text = unwrapped(raw);
        if !text.is_empty() && self.watched.contains(&text) {
            // The turn running this line has begun: the CLI writes this as the prompt is taken
            // up, which for a slash command is the moment the command starts, not when it ends.
            let stamp = stamp_ms(entry);
            let at = self.started.entry(text.clone()).or_insert(0);
            *at = (*at).max(stamp);
            self.note_landed(&text, stamp, false);
            if truthy(entry.get("promptId")) {
                self.command_of_prompt.insert(py_str(entry.get("promptId")), text);
            }
        }
    }

    fn ingest_queue(&mut self, entry: &Object) {
        let op = str_of(entry.get("operation"));
        let mut text = unwrapped(str_of(entry.get("content")));
        let stamp = stamp_ms(entry);
        if op == "enqueue" {
            // Claude Code records a pasted prompt's enqueue with no content: matched by time to
            // the line being typed.
            if text.is_empty() {
                text = self.paste_owner(stamp);
            }
            if !text.is_empty() && self.watched.contains(&text) {
                self.note_landed(&text, stamp, true);
            }
        } else if QUEUE_CONSUMES.contains(&op) {
            // `remove` is the queue handing a line to the running turn, not a drop.
            if self.watched.contains(&text) {
                self.note_landed(&text, stamp, false);
            }
        } else if op == "dequeue" {
            // Carries no content: the queue drained wholesale into the turn that is starting.
            let queued: Vec<String> = self.queued.iter().cloned().collect();
            for text in queued {
                self.note_landed(&text, stamp, false);
            }
        }
    }

    /// Whether this transcript has ever been read. `false` means silent.
    pub fn readable(&self) -> bool {
        self.ever_read
    }

    fn known<T>(&self, answer: impl FnOnce() -> T) -> Option<T> {
        self.ever_read.then(answer)
    }

    /// Has `text` been taken into the conversation? `None` if unknown.
    pub fn consumed(&self, text: &str) -> Option<bool> {
        self.known(|| self.consumed.contains(text.trim()))
    }

    /// Was `text` taken from the queue into a turn after `since_ms`? `None` if unknown.
    ///
    /// Stricter than [`Tail::landed`]: an enqueued line lives only in the worker's memory, and a
    /// worker swap loses it. A line taken up is in the conversation.
    pub fn taken_up(&self, text: &str, since_ms: i64) -> Option<bool> {
        self.known(|| self.consumed_at.get(text.trim()).copied().unwrap_or(0) > since_ms)
    }

    /// Follow the same session in a new process, from where reading stopped.
    pub fn rebind(&mut self, repl_pid: u32) {
        self.repl_pid = repl_pid;
    }

    /// Has the turn that runs `text` begun? `None` if unknown.
    ///
    /// Only a plain `user` entry answers it; a queue event says a line changed hands, which for a
    /// slash command can be a whole turn earlier. Pass `since_ms` (the moment of injection): an
    /// earlier compaction left the identical `/compact` start line within reach of a first read.
    pub fn started(&self, text: &str, since_ms: Option<i64>) -> Option<bool> {
        self.known(|| match self.started.get(text.trim()) {
            None => false,
            Some(stamp) => since_ms.is_none_or(|since| *stamp > since),
        })
    }

    /// Has a compaction completed (a `compact_boundary` entry) after `since_ms`? `None` if unknown.
    pub fn compacted(&self, since_ms: Option<i64>) -> Option<bool> {
        self.known(|| self.compacted_at != 0 && since_ms.is_none_or(|since| self.compacted_at > since))
    }

    /// Is `text` sitting in the session's queue? `None` if unknown.
    pub fn queued(&self, text: &str) -> Option<bool> {
        self.known(|| self.queued.contains(text.trim()))
    }

    /// Did `text` reach the session at all, consumed or queued? `None` if unknown.
    ///
    /// With `since_ms`, asks it of this delivery alone: a resume is often word for word the one
    /// sent after the last compaction.
    pub fn landed(&self, text: &str, since_ms: Option<i64>) -> Option<bool> {
        let text = text.trim();
        self.known(|| match since_ms {
            Some(since) => self.landed_at.get(text).copied().unwrap_or(0) > since,
            None => self.consumed.contains(text) || self.queued.contains(text),
        })
    }

    /// What the local command `text` printed in a run after `since_ms`, or `None`.
    pub fn output(&self, text: &str, since_ms: i64) -> Option<String> {
        match self.output.get(text.trim()) {
            Some((stamp, said)) if *stamp > since_ms => Some(said.clone()),
            _ => None,
        }
    }

    /// Is a foreground tool call outstanding? `None` if unknown.
    ///
    /// A background task leaves no unmatched `tool_use`, because its `tool_result` came back the
    /// moment it was launched; this is the distinction the session record cannot make.
    pub fn tool_call_in_flight(&self) -> Option<bool> {
        self.known(|| !self.open_tools.is_empty())
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use serde_json::json;
    use tempfile::TempDir;

    use super::*;
    use crate::watcher;

    const SESSION: &str = "00000000-0000-4000-8000-000000000001";
    const CLEARED: &str = "00000000-0000-4000-8000-000000000002";
    const PID: u32 = 4242;

    /// A Claude home in a temp dir, with a projects folder and a matching per-pid record.
    struct Fx {
        _dir: TempDir,
        home: ClaudeHome,
        path: PathBuf,
    }

    impl Fx {
        fn tail(&self) -> Tail {
            Tail::new(self.home.clone(), PID)
        }

        fn record(&self, record: Value) {
            std::fs::write(self.home.session_record(PID), record.to_string()).unwrap();
        }

        fn slug(&self, name: &str) -> PathBuf {
            self.home.projects().join(name)
        }
    }

    fn sessions() -> Fx {
        let dir = TempDir::new().unwrap();
        let home = ClaudeHome::at(dir.path().join("claude"), dir.path().join("claude.json"));
        std::fs::create_dir_all(home.projects().join("-work-somewhere")).unwrap();
        std::fs::create_dir_all(home.sessions()).unwrap();
        let path = home.projects().join("-work-somewhere").join(format!("{SESSION}.jsonl"));
        std::fs::write(&path, "").unwrap();
        let fx = Fx { _dir: dir, home, path };
        fx.record(json!({"sessionId": SESSION}));
        fx
    }

    fn append(path: &Path, entries: &[Value]) {
        let mut fp = std::fs::OpenOptions::new().create(true).append(true).open(path).unwrap();
        for entry in entries {
            writeln!(fp, "{entry}").unwrap();
        }
    }

    fn with(mut entry: Value, key: &str, value: Value) -> Value {
        entry.as_object_mut().unwrap().insert(key.into(), value);
        entry
    }

    fn user_text(text: &str) -> Value {
        json!({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
    }

    fn tool_use(call_id: &str) -> Value {
        json!({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": call_id, "name": "Bash"}]}})
    }

    fn tool_result(call_id: &str) -> Value {
        json!({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": call_id}]}})
    }

    fn queue(op: &str, content: Option<&str>) -> Value {
        let entry = json!({"type": "queue-operation", "operation": op});
        match content {
            Some(c) => with(entry, "content", json!(c)),
            None => entry,
        }
    }

    fn queued_command(prompt: &str) -> Value {
        json!({"type": "attachment", "attachment": {"type": "queued_command", "prompt": prompt}})
    }

    fn at(ms: i64) -> String {
        chrono::DateTime::from_timestamp_millis(ms).unwrap().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
    }

    /// The entry the CLI writes as it takes a prompt up: a bare-string content, stamped at the start.
    fn prompt_taken_up(text: &str, when_ms: i64) -> Value {
        json!({"type": "user", "timestamp": at(when_ms), "message": {"role": "user", "content": text}})
    }

    fn compact_boundary(when_ms: i64) -> Value {
        json!({"type": "system", "subtype": "compact_boundary", "timestamp": at(when_ms),
               "content": "Conversation compacted",
               "compactMetadata": {"trigger": "manual", "preTokens": 1000, "postTokens": 100, "durationMs": 5000}})
    }

    fn pasted(text: &str) -> String {
        format!("<pasted_content id=\"p001\">\n{text}\n</pasted_content id=\"p001\">")
    }

    /// A pasted prompt joining the queue: no content at all.
    fn blind_enqueue(when_ms: i64) -> Value {
        with(queue("enqueue", None), "timestamp", json!(at(when_ms)))
    }

    fn command_entry(name: &str, args: &str, when_ms: i64, prompt_id: &str) -> Value {
        let content = format!(
            "<command-name>{name}</command-name>\n            <command-message>{}</command-message>\n            <command-args>{args}</command-args>",
            &name[1..]
        );
        json!({"type": "user", "timestamp": at(when_ms), "promptId": prompt_id,
               "message": {"role": "user", "content": content}})
    }

    fn command_stdout(text: &str, when_ms: i64, prompt_id: &str) -> Value {
        json!({"type": "user", "timestamp": at(when_ms), "promptId": prompt_id,
               "message": {"role": "user", "content": format!("<local-command-stdout>{text}</local-command-stdout>")}})
    }

    // -- finding the file

    #[tokio::test]
    async fn test_the_transcript_is_found_by_id_not_by_guessing_the_directory() {
        let fx = sessions();
        assert_eq!(path_for(&fx.home, SESSION).await, Some(fx.path.clone()));
    }

    #[tokio::test]
    async fn test_an_unknown_session_has_no_transcript() {
        let fx = sessions();
        assert_eq!(path_for(&fx.home, "no-such-session").await, None);
        assert_eq!(path_for(&fx.home, "").await, None);
    }

    #[tokio::test]
    async fn test_of_two_files_for_one_session_the_newest_is_the_transcript() {
        let fx = sessions();
        let stale = fx.slug("-a-sorts-first").join(fx.path.file_name().unwrap());
        std::fs::create_dir_all(stale.parent().unwrap()).unwrap();
        let f = std::fs::File::create(&stale).unwrap();
        f.set_modified(SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000)).unwrap();
        assert_eq!(path_for(&fx.home, SESSION).await, Some(fx.path.clone()));
    }

    #[tokio::test]
    async fn test_a_transcript_moved_to_a_new_directory_is_followed_from_where_it_stood() {
        // A `/cd` keeps the session id and moves the file into the new cwd's slug.
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("before the move");
        tail.watch("after the move");
        append(&fx.path, &[user_text("before the move")]);
        assert!(tail.poll().await);
        let moved = fx.slug("-work-elsewhere").join(fx.path.file_name().unwrap());
        std::fs::create_dir_all(moved.parent().unwrap()).unwrap();
        std::fs::rename(&fx.path, &moved).unwrap();
        append(&moved, &[user_text("after the move")]);
        assert!(tail.poll().await);
        assert_eq!(tail.landed("after the move", None), Some(true));
        assert_eq!(tail.offset, Some(std::fs::metadata(&moved).unwrap().len()), "nothing was read twice");
    }

    #[tokio::test]
    async fn test_a_changed_cwd_re_resolves_even_while_the_old_file_remains() {
        let fx = sessions();
        fx.record(json!({"sessionId": SESSION, "cwd": "/work/somewhere"}));
        let mut tail = fx.tail();
        tail.watch("in the new place");
        append(&fx.path, &[user_text("old")]);
        assert!(tail.poll().await);
        std::fs::File::options()
            .write(true)
            .open(&fx.path)
            .unwrap()
            .set_modified(SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000))
            .unwrap();
        let moved = fx.slug("-work-elsewhere").join(fx.path.file_name().unwrap());
        std::fs::create_dir_all(moved.parent().unwrap()).unwrap();
        std::fs::copy(&fx.path, &moved).unwrap();
        fx.record(json!({"sessionId": SESSION, "cwd": "/work/elsewhere"}));
        append(&moved, &[user_text("in the new place")]);
        assert!(tail.poll().await);
        assert_eq!(tail.landed("in the new place", None), Some(true));
    }

    // -- consumption

    #[tokio::test]
    async fn test_a_plain_user_message_is_consumption() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("resume please");
        append(&fx.path, &[user_text("resume please")]);
        tail.poll().await;
        assert_eq!(tail.consumed("resume please"), Some(true));
        assert_eq!(tail.landed("resume please", None), Some(true));
    }

    #[tokio::test]
    async fn test_an_enqueue_has_landed_but_is_not_yet_consumed() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("resume please");
        append(&fx.path, &[queue("enqueue", Some("resume please"))]);
        tail.poll().await;
        assert_eq!(tail.landed("resume please", None), Some(true));
        assert_eq!(tail.queued("resume please"), Some(true));
        assert_eq!(tail.consumed("resume please"), Some(false));
    }

    #[tokio::test]
    async fn test_remove_is_the_delivery_path_not_a_drop() {
        // Reading `remove` as a loss is how a delivered resume gets re-sent.
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("resume please");
        append(
            &fx.path,
            &[
                queue("enqueue", Some("resume please")),
                queue("remove", Some("resume please")),
                queued_command("resume please"),
            ],
        );
        tail.poll().await;
        assert_eq!(tail.consumed("resume please"), Some(true));
        assert_eq!(tail.queued("resume please"), Some(false));
    }

    #[tokio::test]
    async fn test_a_pasted_resume_is_recognised_through_its_envelope() {
        let fx = sessions();
        let resume = "continue with step two.\nState: the made-up widgets are done.";
        let mut tail = fx.tail();
        tail.watch(resume);
        append(&fx.path, &[queue("enqueue", Some(&pasted(resume)))]);
        tail.poll().await;
        assert_eq!(tail.landed(resume, None), Some(true));
        append(
            &fx.path,
            &[
                queue("remove", Some(&pasted(resume))),
                queued_command(&pasted(resume)),
                prompt_taken_up(&pasted(resume), 2_000_000),
            ],
        );
        tail.poll().await;
        assert_eq!(tail.consumed(resume), Some(true));
        assert_eq!(tail.started(resume, Some(1_000_000)), Some(true));
    }

    #[tokio::test]
    async fn test_a_content_less_enqueue_after_we_typed_is_ours() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("resume please");
        tail.expect("resume please", Some(2_000_000));
        append(&fx.path, &[blind_enqueue(2_000_050)]);
        tail.poll().await;
        assert_eq!(tail.landed("resume please", None), Some(true));
        assert_eq!(tail.queued("resume please"), Some(true));
        assert_eq!(tail.landed("resume please", Some(1_000_000)), Some(true));
    }

    #[tokio::test]
    async fn test_a_content_less_enqueue_from_before_we_typed_is_not_ours() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.expect("resume please", Some(2_000_000));
        append(&fx.path, &[blind_enqueue(1_999_000)]);
        tail.poll().await;
        assert_eq!(tail.landed("resume please", None), Some(false));
    }

    #[tokio::test]
    async fn test_a_content_less_enqueue_is_not_ours_unless_we_are_typing() {
        // Watching for a resume due later is not typing it.
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("resume please");
        append(&fx.path, &[blind_enqueue(2_000_050)]);
        tail.poll().await;
        assert_eq!(tail.landed("resume please", None), Some(false));
    }

    #[tokio::test]
    async fn test_one_paste_claims_one_enqueue() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.expect("resume please", Some(2_000_000));
        append(&fx.path, &[blind_enqueue(2_000_050), queue("dequeue", None)]);
        tail.poll().await;
        tail.watch("other");
        append(&fx.path, &[blind_enqueue(2_000_090)]);
        tail.poll().await;
        assert_eq!(tail.consumed("resume please"), Some(true));
        assert_eq!(tail.queued("resume please"), Some(false));
    }

    #[tokio::test]
    async fn test_landed_since_ignores_an_identical_resume_from_an_earlier_compaction() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("resume please");
        append(&fx.path, &[prompt_taken_up("resume please", 1_000_000)]);
        tail.poll().await;
        assert_eq!(tail.landed("resume please", None), Some(true));
        assert_eq!(tail.landed("resume please", Some(1_500_000)), Some(false));
        append(&fx.path, &[prompt_taken_up(&pasted("resume please"), 2_000_000)]);
        tail.poll().await;
        assert_eq!(tail.landed("resume please", Some(1_500_000)), Some(true));
    }

    #[tokio::test]
    async fn test_an_envelope_around_other_text_is_not_a_match() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("resume please");
        append(
            &fx.path,
            &[
                queue("enqueue", Some(&pasted("resume please, and more"))),
                queue("enqueue", Some("<pasted_content id=\"a\">\nresume please\n</pasted_content id=\"b\">")),
            ],
        );
        tail.poll().await;
        assert_eq!(tail.landed("resume please", None), Some(false));
    }

    #[tokio::test]
    async fn test_dequeue_drains_everything_outstanding() {
        // A dequeue is the whole queue going into the turn that is starting.
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("proceed with the plan");
        append(&fx.path, &[queue("enqueue", Some("proceed with the plan")), queue("dequeue", None)]);
        tail.poll().await;
        assert_eq!(tail.consumed("proceed with the plan"), Some(true));
    }

    #[tokio::test]
    async fn test_text_nobody_asked_about_is_not_retained() {
        let fx = sessions();
        let mut tail = fx.tail();
        append(&fx.path, &[user_text("something else entirely")]);
        tail.poll().await;
        assert_eq!(tail.consumed("something else entirely"), Some(false));
    }

    #[tokio::test]
    async fn test_matching_ignores_surrounding_whitespace() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("  resume please\n");
        append(&fx.path, &[user_text("resume please")]);
        tail.poll().await;
        assert_eq!(tail.consumed("resume please\n"), Some(true));
    }

    // -- execution: has the command actually begun?

    #[tokio::test]
    async fn test_a_prompt_taken_up_is_the_command_starting() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/compact");
        append(&fx.path, &[prompt_taken_up("/compact", 2_000_000)]);
        tail.poll().await;
        assert_eq!(tail.started("/compact", None), Some(true));
        assert_eq!(tail.consumed("/compact"), Some(true));
    }

    #[tokio::test]
    async fn test_an_enqueued_command_has_not_started() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/compact");
        append(&fx.path, &[queue("enqueue", Some("/compact"))]);
        tail.poll().await;
        assert_eq!(tail.started("/compact", None), Some(false));
        assert_eq!(tail.landed("/compact", None), Some(true));
    }

    #[tokio::test]
    async fn test_a_dequeue_alone_does_not_mean_the_command_started() {
        // The turn a dequeue starts may run an ordinary prompt first and reach the command last.
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/compact");
        append(&fx.path, &[queue("enqueue", Some("/compact")), queue("dequeue", None)]);
        tail.poll().await;
        assert_eq!(tail.consumed("/compact"), Some(true));
        assert_eq!(tail.started("/compact", None), Some(false));
    }

    #[tokio::test]
    async fn test_the_queue_handing_a_line_over_is_not_execution_either() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/compact");
        append(
            &fx.path,
            &[queue("enqueue", Some("/compact")), queue("remove", Some("/compact")), queued_command("/compact")],
        );
        tail.poll().await;
        assert_eq!(tail.consumed("/compact"), Some(true));
        assert_eq!(tail.started("/compact", None), Some(false));
    }

    #[tokio::test]
    async fn test_the_start_line_is_seen_even_when_the_queue_handed_it_over_first() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/compact");
        append(
            &fx.path,
            &[queue("enqueue", Some("/compact")), queue("dequeue", None), prompt_taken_up("/compact", 2_000_000)],
        );
        tail.poll().await;
        assert_eq!(tail.started("/compact", None), Some(true));
    }

    #[tokio::test]
    async fn test_a_compaction_boundary_is_reported() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.poll().await;
        assert_eq!(tail.compacted(None), Some(false));
        append(&fx.path, &[compact_boundary(2_000_000)]);
        tail.poll().await;
        assert_eq!(tail.compacted(None), Some(true));
    }

    #[tokio::test]
    async fn test_an_earlier_compactions_start_line_is_not_this_command_starting() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/compact");
        append(&fx.path, &[prompt_taken_up("/compact", 1_000_000), compact_boundary(1_100_000)]);
        tail.poll().await;
        assert_eq!(tail.started("/compact", Some(1_500_000)), Some(false));
        assert_eq!(tail.compacted(Some(1_500_000)), Some(false));
        assert_eq!(tail.started("/compact", Some(900_000)), Some(true));
        assert_eq!(tail.compacted(Some(900_000)), Some(true));
    }

    #[tokio::test]
    async fn test_an_undated_start_line_cannot_answer_a_since_question() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/compact");
        append(&fx.path, &[json!({"type": "user", "message": {"role": "user", "content": "/compact"}})]);
        tail.poll().await;
        assert_eq!(tail.started("/compact", None), Some(true));
        assert_eq!(tail.started("/compact", Some(1)), Some(false));
    }

    #[tokio::test]
    async fn test_an_unreadable_transcript_knows_nothing_about_starting() {
        let fx = sessions();
        std::fs::remove_file(&fx.path).unwrap();
        let mut tail = fx.tail();
        tail.watch("/compact");
        tail.poll().await;
        assert_eq!(tail.started("/compact", None), None);
        assert_eq!(tail.compacted(None), None);
    }

    // -- what the session is busy with

    #[tokio::test]
    async fn test_an_unanswered_tool_call_is_a_foreground_call_in_flight() {
        let fx = sessions();
        let mut tail = fx.tail();
        append(&fx.path, &[tool_use("toolu_1")]);
        tail.poll().await;
        assert_eq!(tail.tool_call_in_flight(), Some(true));
    }

    #[tokio::test]
    async fn test_a_tool_call_that_came_back_leaves_nothing_in_flight() {
        let fx = sessions();
        let mut tail = fx.tail();
        append(&fx.path, &[tool_use("toolu_1"), tool_result("toolu_1")]);
        tail.poll().await;
        assert_eq!(tail.tool_call_in_flight(), Some(false));
    }

    #[tokio::test]
    async fn test_a_background_task_leaves_nothing_in_flight() {
        // A background task hands its result straight back, so `shell` comes with a clean tail.
        let fx = sessions();
        let mut tail = fx.tail();
        append(
            &fx.path,
            &[tool_use("toolu_bg"), tool_result("toolu_bg"), tool_use("toolu_fg"), tool_result("toolu_fg")],
        );
        tail.poll().await;
        assert_eq!(tail.tool_call_in_flight(), Some(false));
    }

    #[tokio::test]
    async fn test_a_subagents_own_turns_are_not_this_sessions_work() {
        let fx = sessions();
        let mut tail = fx.tail();
        append(&fx.path, &[with(tool_use("toolu_side"), "isSidechain", json!(true))]);
        tail.poll().await;
        assert_eq!(tail.tool_call_in_flight(), Some(false));
    }

    // -- reading

    #[tokio::test]
    async fn test_an_unreadable_transcript_answers_dont_know() {
        let fx = sessions();
        std::fs::remove_file(&fx.path).unwrap();
        let mut tail = fx.tail();
        tail.watch("resume please");
        assert!(!tail.poll().await);
        assert!(!tail.readable());
        assert_eq!(tail.consumed("resume please"), None);
        assert_eq!(tail.landed("resume please", None), None);
        assert_eq!(tail.tool_call_in_flight(), None);
    }

    #[tokio::test]
    async fn test_polling_is_incremental_across_calls() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("second");
        append(&fx.path, &[user_text("first")]);
        tail.poll().await;
        assert_eq!(tail.consumed("second"), Some(false));
        append(&fx.path, &[user_text("second")]);
        tail.poll().await;
        assert_eq!(tail.consumed("second"), Some(true));
    }

    #[tokio::test]
    async fn test_a_half_written_last_line_is_picked_up_once_it_is_complete() {
        // Split inside a multi-byte char, which must stay whole in the partial buffer.
        let fx = sessions();
        let text = "resume please \u{2014} now";
        let mut tail = fx.tail();
        tail.watch(text);
        let line = user_text(text).to_string().into_bytes();
        let cut = line.windows(3).position(|w| w == "\u{2014}".as_bytes()).unwrap() + 1;
        let mut fp = std::fs::OpenOptions::new().append(true).open(&fx.path).unwrap();
        fp.write_all(&line[..cut]).unwrap();
        tail.poll().await;
        assert_eq!(tail.consumed(text), Some(false));
        fp.write_all(&line[cut..]).unwrap();
        fp.write_all(b"\n").unwrap();
        tail.poll().await;
        assert_eq!(tail.consumed(text), Some(true));
    }

    #[tokio::test]
    async fn test_a_line_that_is_not_json_is_skipped() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("resume please");
        let mut fp = std::fs::OpenOptions::new().append(true).open(&fx.path).unwrap();
        writeln!(fp, "{{not json at all}}").unwrap();
        writeln!(fp, "{}", user_text("resume please")).unwrap();
        tail.poll().await;
        assert_eq!(tail.consumed("resume please"), Some(true));
    }

    #[tokio::test]
    async fn test_a_first_poll_reaches_back_far_enough_to_see_the_turn_in_progress() {
        // Also: a file longer than the catch-up window starts inside it, at a line boundary.
        let fx = sessions();
        let filler = user_text(&"x".repeat(4096));
        append(&fx.path, &vec![filler; (MAX_CATCHUP_BYTES / 4096 + 4) as usize]);
        append(&fx.path, &[tool_use("toolu_1")]);
        let mut tail = fx.tail();
        tail.poll().await;
        assert_eq!(tail.tool_call_in_flight(), Some(true));
    }

    #[tokio::test]
    async fn test_a_clear_mid_wait_is_followed_to_the_new_transcript() {
        // /clear mints a new session id; following the old file would watch a dead conversation.
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("resume please");
        tail.poll().await;
        fx.record(json!({"sessionId": CLEARED}));
        let fresh = fx.slug("-work-somewhere").join(format!("{CLEARED}.jsonl"));
        append(&fresh, &[user_text("resume please")]);
        tail.poll().await;
        assert_eq!(tail.consumed("resume please"), Some(true));
    }

    #[tokio::test]
    async fn test_a_transcript_that_shrinks_is_re_read_from_the_top() {
        let fx = sessions();
        let mut tail = Tail::from_start(fx.home.clone(), PID);
        tail.watch("resume please");
        append(&fx.path, &[user_text("filler"), user_text("filler")]);
        tail.poll().await;
        std::fs::write(&fx.path, format!("{}\n", user_text("resume please"))).unwrap();
        tail.poll().await;
        assert_eq!(tail.consumed("resume please"), Some(true));
    }

    // -- a /clear, end to end

    /// Write what a live /clear writes: the dequeue, a new id, a new file.
    fn clear_session(fx: &Fx, when_ms: i64) {
        append(&fx.path, &[with(queue("dequeue", None), "timestamp", json!(at(when_ms - 20)))]);
        fx.record(json!({"sessionId": CLEARED}));
        let fresh = fx.slug("-work-somewhere").join(format!("{CLEARED}.jsonl"));
        append(
            &fresh,
            &[
                command_entry("/clear", "", when_ms, "p1"),
                json!({"type": "system", "subtype": "local_command", "timestamp": at(when_ms),
                       "content": "<local-command-stdout></local-command-stdout>"}),
            ],
        );
    }

    struct ShellProbe {
        updated_ms: i64,
        now: i64,
    }

    impl watcher::Probe for ShellProbe {
        async fn read_status(&mut self, _pid: u32) -> Option<(String, i64)> {
            Some(("shell".into(), self.updated_ms))
        }
        async fn proc_start(&mut self, _pid: u32) -> Option<String> {
            Some("123".into())
        }
        fn now_ms(&self) -> i64 {
            self.now
        }
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_self_cleared_session_with_a_background_task_gets_its_resume_at_once() {
        // The /clear start line exists only in its <command-name> form, and the dequeue sits in the
        // old file just before the new id, so a tail that switches files without reading it would
        // hold the command as queued, and a `shell` with our command queued never settles.
        let fx = sessions();
        let injected = 1_000_000;
        append(
            &fx.path,
            &[
                tool_use("toolu_bg"),
                tool_result("toolu_bg"),
                with(queue("enqueue", Some("/clear")), "timestamp", json!(at(injected + 100))),
            ],
        );
        let mut tail = fx.tail();
        tail.watch("/clear");
        tail.poll().await;
        assert_eq!(tail.queued("/clear"), Some(true));

        clear_session(&fx, injected + 20_000);
        let mut probe = ShellProbe { updated_ms: injected + 20_000, now: injected + 120_000 };
        let outcome = watcher::await_completion(PID, injected, "", "/clear", &mut tail, &mut probe).await;
        assert_eq!(outcome, watcher::Outcome::Started);
        assert_eq!(tail.session_id(), Some(CLEARED));
        assert_eq!(tail.queued("/clear"), Some(false));
    }

    #[tokio::test]
    async fn test_a_clear_leaves_no_foreground_call_in_flight() {
        // A tool call the old conversation never answered is not one the new one waits on.
        let fx = sessions();
        append(&fx.path, &[tool_use("toolu_orphan")]);
        let mut tail = fx.tail();
        tail.poll().await;
        assert_eq!(tail.tool_call_in_flight(), Some(true));
        clear_session(&fx, 2_000_000);
        tail.poll().await;
        assert_eq!(tail.tool_call_in_flight(), Some(false));
    }

    #[tokio::test]
    async fn test_the_first_poll_does_not_count_as_a_clear() {
        // Opening a tail is not a session id changing under it: nothing is reset.
        let fx = sessions();
        append(&fx.path, &[tool_use("toolu_1")]);
        let mut tail = fx.tail();
        tail.poll().await;
        assert_eq!(tail.session_id(), Some(SESSION));
        assert_eq!(tail.tool_call_in_flight(), Some(true));
    }

    #[tokio::test]
    async fn test_a_local_command_is_seen_to_start_from_its_command_name_entry() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/mcp reconnect demo");
        append(&fx.path, &[command_entry("/mcp", "reconnect demo", 2_000_000, "p1")]);
        tail.poll().await;
        assert_eq!(tail.started("/mcp reconnect demo", Some(1_000_000)), Some(true));
    }

    #[tokio::test]
    async fn test_a_local_commands_output_is_read_off_its_prompt() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/mcp reconnect demo");
        append(
            &fx.path,
            &[
                command_entry("/mcp", "reconnect demo", 2_000_000, "pA"),
                command_stdout("Reconnected to demo", 2_000_000, "pA"),
                command_stdout("someone else's", 2_000_000, "pB"),
            ],
        );
        tail.poll().await;
        assert_eq!(tail.output("/mcp reconnect demo", 1_000_000).as_deref(), Some("Reconnected to demo"));
    }

    #[tokio::test]
    async fn test_an_earlier_runs_output_does_not_answer_for_this_one() {
        let fx = sessions();
        let mut tail = fx.tail();
        tail.watch("/mcp reconnect demo");
        append(
            &fx.path,
            &[
                command_entry("/mcp", "reconnect demo", 500_000, "p1"),
                command_stdout("Reconnected to demo", 500_000, "p1"),
            ],
        );
        tail.poll().await;
        assert_eq!(tail.output("/mcp reconnect demo", 1_000_000), None);
    }
}
