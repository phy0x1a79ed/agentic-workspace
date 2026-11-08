//! Which restricted mode, if any, a Claude Code session was started in, and the lineage records
//! cch keeps under `<cx state>/starts/` to answer it.
//!
//! Everything is read from files (Claude Code's session records, the daemon roster, the job
//! records and the lineage records), so the answer needs no running cch state. A caller is only
//! ever held to its own records: anything that is not a background session answers before the
//! roster or the lineage directory is opened, so a damaged record of another job cannot lock out
//! an interactive terminal.

use std::cell::{Cell, OnceCell};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use awm_harness::claude::records::{read_json, read_json_strict, Object, Unreadable};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::paths::Paths;

/// The answer for a session whose mode cannot be positively known. Gate it as the most
/// restricted mode.
pub const UNKNOWN: &str = "unknown";

/// How far up the process tree a child looks for the session that started it.
pub const MAX_ANCESTRY_HOPS: usize = 16;

/// Python's `$` also matches before one trailing newline, and the patterns below keep that.
fn without_py_eol(text: &str) -> &str {
    text.strip_suffix('\n').unwrap_or(text)
}

/// Whether `text` is a mode label: `^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$`. A recorded mode that
/// fails this is corrupt.
pub fn is_mode(text: &str) -> bool {
    let b = without_py_eol(text).as_bytes();
    (1..=40).contains(&b.len())
        && b[0].is_ascii_alphanumeric()
        && b[1..].iter().all(|c| c.is_ascii_alphanumeric() || b"._-".contains(c))
}

/// Whether `text` is a job id as lineage checks it: `^[0-9a-f]{8}$`.
pub fn is_job(text: &str) -> bool {
    let b = without_py_eol(text).as_bytes();
    b.len() == 8 && b.iter().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(c))
}

/// The process table, as lineage reads it.
pub trait Processes {
    /// The start time of `pid` in clock ticks since boot, or None when there is no such process.
    fn start(&self, pid: u32) -> Option<String>;
    /// The parent of `pid`, or None when it cannot be read.
    fn ppid(&self, pid: u32) -> Option<u32>;
    /// Whether `pid` could be a Claude Code REPL. Decides only how a damaged roster is treated.
    fn may_be_a_session(&self, pid: u32) -> bool;
}

/// The real process table under `/proc`.
#[derive(Debug, Clone, Copy, Default)]
pub struct Procfs;

impl Processes for Procfs {
    fn start(&self, pid: u32) -> Option<String> {
        awm_plugin_sdk::procfs::start_ticks(pid)
    }

    fn ppid(&self, pid: u32) -> Option<u32> {
        awm_plugin_sdk::procfs::ppid(pid)
    }

    fn may_be_a_session(&self, pid: u32) -> bool {
        awm_harness::claude::detect::may_be_a_session(pid)
    }
}

/// The mode of a session, and a gate must treat each answer differently.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Mode {
    /// Positively not a cx-started session: an interactive terminal, a pool session or a
    /// hand-started job, and its own records say so.
    Ungated,
    /// A session cx started (or is about to adopt), with the mode it was declared with.
    Declared(String),
    /// Anything else: an invalid pid, one of the caller's own records unreadable or stale, a
    /// background session with no record of how it was started, or a job that carries cx's
    /// launch flags but no lineage.
    Unknown,
}

impl Mode {
    fn from_answer(answer: Result<Option<String>, Unread>) -> Self {
        match answer {
            Ok(None) => Mode::Ungated,
            Ok(Some(m)) if m != UNKNOWN => Mode::Declared(m),
            _ => Mode::Unknown,
        }
    }

    /// The answer as the wire carries it: null, `"unknown"` or the mode.
    pub fn as_wire(&self) -> Option<&str> {
        match self {
            Mode::Ungated => None,
            Mode::Declared(m) => Some(m),
            Mode::Unknown => Some(UNKNOWN),
        }
    }
}

/// The mode of the session whose REPL is `caller_pid`, given as the JSON argument a verb received.
/// Anything but a positive JSON integer is [`Mode::Unknown`]; a boolean is not an integer.
pub fn mode_of_arg(paths: &Paths, procs: &dyn Processes, caller_pid: &Value) -> Mode {
    match caller_pid {
        Value::Number(n) => n.as_i64().map_or(Mode::Unknown, |pid| mode_of(paths, procs, pid)),
        _ => Mode::Unknown,
    }
}

/// The mode of the session whose REPL is `caller_pid`. Reads files synchronously: call it off the
/// async executor.
pub fn mode_of(paths: &Paths, procs: &dyn Processes, caller_pid: i64) -> Mode {
    match u32::try_from(caller_pid) {
        Ok(pid) if pid > 0 => Mode::from_answer(Call::new(paths, procs).mode_of(pid)),
        _ => Mode::Unknown,
    }
}

/// A record the answer needs is missing or unreadable.
#[derive(Debug, Clone)]
struct Unread(#[allow(dead_code)] String);

impl From<Unreadable> for Unread {
    fn from(e: Unreadable) -> Self {
        Unread(e.to_string())
    }
}

fn unread(why: impl Into<String>) -> Unread {
    Unread(why.into())
}

fn py_truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Python's `str(v)`, close enough to compare against a start time: only a string can equal one.
fn py_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(true)) => "True".into(),
        Some(Value::Bool(false)) => "False".into(),
        Some(other) => other.to_string(),
    }
}

/// Python's `value == pid`, where `True == 1` and `5.0 == 5`.
fn equals_pid(v: Option<&Value>, pid: u32) -> bool {
    match v {
        Some(Value::Number(n)) => n.as_f64() == Some(f64::from(pid)),
        Some(Value::Bool(b)) => u32::from(*b) == pid,
        _ => false,
    }
}

fn text(v: Option<&Value>) -> Option<&str> {
    v.and_then(Value::as_str)
}

fn object(v: Option<&Value>) -> Option<&Object> {
    v.and_then(Value::as_object)
}

fn is_bg(rec: &Object) -> bool {
    text(rec.get("kind")) == Some("bg")
}

/// The object in `path`; None when the file is absent; an error when it is damaged.
fn read_record(path: &Path) -> Result<Option<Object>, Unread> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(unread(format!("{}: {e}", path.display()))),
    };
    match serde_json::from_slice(&bytes) {
        Ok(Value::Object(map)) => Ok(Some(map)),
        _ => Err(unread(format!("{} is not an object", path.display()))),
    }
}

/// Whether the job's recorded launch flags are ones only cx writes.
///
/// A fallback for a job whose lineage record is gone, so it matches only the `=` forms that
/// `build_flags` writes, never what a person types: a person passes `--permission-mode plan` as
/// two tokens, and a resumed job keeps them that way. A start with `--permission-mode=<mode>` or
/// `--restricted` is cx's, and so is one with both `--effort=` and `--model=`, which covers a
/// skip-permissions start. A resumed job keeps its flags under `dispatch.launch.flagArgs`. The
/// roster keeps no environment cx could set as a stronger marker. This and `build_flags` change
/// together.
pub fn looks_cx_started(worker: &Object) -> bool {
    let empty = Object::new();
    let dispatch = object(worker.get("dispatch")).unwrap_or(&empty);
    let launch = object(dispatch.get("launch")).unwrap_or(&empty);
    let sources = [launch.get("args"), launch.get("flagArgs"), dispatch.get("respawnFlags"), worker.get("respawnFlags")];
    let strings: Vec<&str> =
        sources.into_iter().filter_map(|s| s.and_then(Value::as_array)).flatten().filter_map(Value::as_str).collect();
    let equals: BTreeSet<&str> = strings.iter().filter_map(|t| t.split_once('=').map(|(flag, _)| flag)).collect();
    strings.contains(&"--restricted")
        || equals.contains("--permission-mode")
        || (equals.contains("--effort") && equals.contains("--model"))
}

/// One `mode_of` call. The roster is read at most once through `view`, and a failed read is
/// remembered too, so a damaged roster costs one read, not one per ancestor.
struct Call<'a> {
    paths: &'a Paths,
    procs: &'a dyn Processes,
    view: OnceCell<Result<Object, Unread>>,
    roster_reads: Cell<usize>,
}

impl<'a> Call<'a> {
    fn new(paths: &'a Paths, procs: &'a dyn Processes) -> Self {
        Self { paths, procs, view: OnceCell::new(), roster_reads: Cell::new(0) }
    }

    fn mode_of(&self, pid: u32) -> Result<Option<String>, Unread> {
        if self.procs.start(pid).is_none() {
            // Nothing about a missing process can be positively known, and the ancestor walk
            // would read its missing /proc entry as "no parent".
            return Err(unread(format!("pid {pid} is not a running process")));
        }
        let (rec, claims_job) = self.verified_record(pid)?;
        if let Some(r) = rec.as_ref().filter(|r| is_bg(r)) {
            return self.bg_mode(r);
        }
        if let Some(parked) = self.parked_mode(rec.as_ref())? {
            return Ok(Some(parked));
        }
        if rec.is_none() {
            let (listed, unrecorded) = self.unrecorded_mode(pid)?;
            if listed {
                // The roster names this process as a job REPL and says whether cx started it.
                // Nothing above it can change that.
                return Ok(unrecorded);
            }
        }
        let inherited = self.inherited_mode(pid);
        if inherited.is_none() && claims_job {
            return Ok(Some(UNKNOWN.into()));
        }
        Ok(inherited)
    }

    /// Claude Code's record of the live process `pid`; None when it has none. A missing sessions
    /// directory means no session has run here. A record that is unreadable, or describes another
    /// process, is the caller's own damaged record and errors.
    fn session_record(&self, pid: u32) -> Result<Option<Object>, Unread> {
        let Some(data) = read_record(&self.paths.home.session_record(pid))? else { return Ok(None) };
        let claimed = data.get("procStart").map(|v| py_str(Some(v))).unwrap_or_default();
        match self.procs.start(pid) {
            Some(live) if claimed.is_empty() || claimed == live => Ok(Some(data)),
            _ => Err(unread(format!("the record of pid {pid} is stale"))),
        }
    }

    /// The session record of `pid`, and whether an untrusted one claimed a job.
    ///
    /// A record with no `procStart` cannot be told from one an earlier process with the same pid
    /// left behind, so it may grant nothing, above all not `Ungated`. It is dropped and the roster
    /// and the ancestors decide. Only when they find nothing, and the dropped record said it was a
    /// background session or a parked job, is the answer `unknown`: a record that says "I am a
    /// job" must not turn into "I am an ordinary terminal".
    fn verified_record(&self, pid: u32) -> Result<(Option<Object>, bool), Unread> {
        match self.session_record(pid)? {
            Some(rec) if !py_truthy(rec.get("procStart")) => {
                Ok((None, is_bg(&rec) || rec.get("parkedJobId").is_some_and(Value::is_string)))
            }
            rec => Ok((rec, false)),
        }
    }

    fn bg_mode(&self, rec: &Object) -> Result<Option<String>, Unread> {
        let Some(job) = text(rec.get("jobId")).filter(|j| is_job(j)) else {
            return Err(unread("a background session record carries no job id"));
        };
        let lenient = self.workers(false)?;
        if let Some(declared) = self.declared(job, self.names(job, lenient.get(job), rec.get("name")))? {
            return Ok(Some(declared));
        }
        let strict = self.workers(true)?;
        let Some(worker) = object(strict.get(job)) else {
            return Err(unread("the roster does not list the background session"));
        };
        Ok(looks_cx_started(worker).then(|| UNKNOWN.into()))
    }

    /// The mode of the job an interactive session took over. Attaching a terminal parks the job
    /// and runs its conversation in an interactive process, which keeps the job's mode, or
    /// attaching would lift the gate.
    fn parked_mode(&self, rec: Option<&Object>) -> Result<Option<String>, Unread> {
        let Some(rec) = rec else { return Ok(None) };
        let Some(parked) = text(rec.get("parkedJobId")).filter(|j| is_job(j)) else { return Ok(None) };
        let workers = self.workers(false)?;
        self.declared(parked, self.names(parked, workers.get(parked), rec.get("name")))
    }

    /// The mode of the nearest ancestor process that is a cx-started session.
    ///
    /// A process a restricted session starts (a `claude -p` child, a teammate, an MCP proxy
    /// behind a wrapper) is not itself the session and would be ungated without this walk. The
    /// first ancestor with a mode, `unknown` included, decides. Every ancestor is looked up in the
    /// roster, because a background REPL may have no session record or a stale one: a missing,
    /// stale or unreadable record counts as none.
    fn inherited_mode(&self, pid: u32) -> Option<String> {
        let mut current = pid;
        for _ in 0..MAX_ANCESTRY_HOPS {
            let Some(parent) = self.procs.ppid(current) else {
                // The process was alive a moment ago, so an unreadable parent is a process that
                // died under us, not the top of the tree. A reparented process shows ppid 1.
                return Some(UNKNOWN.into());
            };
            current = parent;
            if current <= 1 {
                return None;
            }
            let (rec, claims_job) = self.verified_record(current).unwrap_or((None, false));
            let mode = match &rec {
                None => match self.unrecorded_mode(current) {
                    Ok((_, None)) if claims_job => return Some(UNKNOWN.into()),
                    Ok((_, mode)) => mode,
                    Err(_) => return Some(UNKNOWN.into()),
                },
                Some(r) => {
                    match if is_bg(r) { self.bg_mode(r) } else { self.parked_mode(Some(r)) } {
                        Ok(mode) => mode,
                        Err(_) => return Some(UNKNOWN.into()),
                    }
                }
            };
            if mode.is_some() {
                return mode;
            }
        }
        None
    }

    /// A pid with no session record: only the roster can name it. Answers whether the roster
    /// lists it as a job REPL, and the mode it gives it. A roster that cannot be read says nothing
    /// about a shell, so it is an error only for a process that may be a Claude Code session.
    fn unrecorded_mode(&self, pid: u32) -> Result<(bool, Option<String>), Unread> {
        let workers = match self.view.get_or_init(|| self.workers(true)) {
            Ok(w) => w,
            Err(e) if self.procs.may_be_a_session(pid) => return Err(e.clone()),
            Err(_) => return Ok((false, None)),
        };
        for (job, worker) in workers {
            let Some(w) = worker.as_object() else { continue };
            if equals_pid(w.get("replPid"), pid)
                && self.procs.start(pid).is_some_and(|live| live == py_str(w.get("replProcStart")))
            {
                if let Some(declared) = self.declared(job, self.names(job, Some(worker), None))? {
                    return Ok((true, Some(declared)));
                }
                return Ok((true, looks_cx_started(w).then(|| UNKNOWN.into())));
            }
        }
        Ok((false, None))
    }

    /// The roster's workers. Absent means none; unreadable errors only when `strict`.
    fn workers(&self, strict: bool) -> Result<Object, Unread> {
        self.roster_reads.set(self.roster_reads.get() + 1);
        let path = self.paths.home.roster();
        let data = if strict { read_json_strict(&path)? } else { read_json(&path) };
        match data.get("workers") {
            None | Some(Value::Null) => Ok(Object::new()),
            Some(Value::Object(w)) => Ok(w.clone()),
            Some(_) if strict => Err(unread("the roster's workers are not an object")),
            Some(_) => Ok(Object::new()),
        }
    }

    /// Every name a pending start for this job could have been filed under.
    fn names(&self, job: &str, worker: Option<&Value>, session_name: Option<&Value>) -> BTreeSet<String> {
        let seed = object(worker)
            .and_then(|w| object(w.get("dispatch")))
            .and_then(|d| object(d.get("seed")))
            .and_then(|s| s.get("name"));
        let state = read_json(&self.paths.home.job_state(job));
        [session_name, seed, state.get("name")]
            .into_iter()
            .filter_map(text)
            .filter(|n| !n.is_empty())
            .map(str::to_string)
            .collect()
    }

    /// The mode recorded for this job, else for a pending start of one of its names. Only the
    /// files this job could own are read: a damaged record of any other job is not this caller's
    /// business.
    fn declared(&self, job: &str, names: BTreeSet<String>) -> Result<Option<String>, Unread> {
        let mut record = read_record(&self.paths.lineage_path(job))?;
        for name in &names {
            if record.is_some() {
                break;
            }
            record = read_record(&self.paths.pending_path(name))?;
        }
        let Some(record) = record else { return Ok(None) };
        match text(record.get("mode")) {
            Some(mode) if is_mode(mode) => Ok(Some(mode.to_string())),
            _ => Err(unread(format!("the record of {job} carries no valid mode"))),
        }
    }
}

/// Where a lobby start was asked to go before it fell back to the lobby.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Suggested {
    pub project: Option<String>,
    pub scope: Option<String>,
}

/// `value` as a `T`, taken out of `map`; left in place when absent, null or of another type.
fn take<T: serde::de::DeserializeOwned>(map: &mut Object, key: &str) -> Option<T> {
    let parsed = map.get(key).filter(|v| !v.is_null()).and_then(|v| serde_json::from_value(v.clone()).ok())?;
    map.remove(key);
    Some(parsed)
}

macro_rules! lineage_record {
    ($( $(#[$doc:meta])* $field:ident : $ty:ty, )*) => {
        /// A lineage record, `starts/<job>.json`, or a pending one, `starts/pending-<digest>.json`.
        ///
        /// Every key is optional. A key of an unexpected type, a null, and any key this struct
        /// does not name are kept in `extra` as read, so a record round-trips through a rewrite.
        /// `mode_of` reads only `mode`, and a record whose `mode` is not a valid label makes its
        /// session `unknown` at the gateway.
        #[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
        #[serde(from = "Object", into = "Object")]
        pub struct LineageRecord {
            $( $(#[$doc])* pub $field: Option<$ty>, )*
            pub extra: Object,
        }

        impl From<Object> for LineageRecord {
            fn from(mut map: Object) -> Self {
                Self { $( $field: take(&mut map, stringify!($field)), )* extra: map }
            }
        }

        impl From<LineageRecord> for Object {
            fn from(r: LineageRecord) -> Object {
                let mut out = Object::new();
                $(
                    if let Some(v) = r.$field {
                        out.insert(stringify!($field).into(), serde_json::to_value(v).unwrap_or(Value::Null));
                    }
                )*
                for (k, v) in r.extra {
                    out.entry(k).or_insert(v);
                }
                out
            }
        }
    };
}

lineage_record! {
    job: String,
    name: String,
    project: String,
    scope: String,
    cwd: String,
    /// The starting session: a background parent's job id, else an interactive one's session id.
    parent: String,
    parent_session: String,
    caller: String,
    /// The declared mode: the only key the gate reads.
    mode: String,
    remote_control: bool,
    model: String,
    effort: String,
    started_at: String,
    started_epoch: f64,
    pinned: bool,
    brief: bool,
    lobby: bool,
    suggested: Suggested,
    task_id: String,
    task_role: String,
    tags: Vec<String>,
    replaces: String,
    session_id: String,
    brief_text: String,
    adopted: bool,
    resumed_from: String,
    origin: String,
    /// Set on a record written before a launch, until the job it names appears.
    pending: bool,
}

fn read_object(path: &Path) -> Option<Object> {
    match serde_json::from_slice(&std::fs::read(path).ok()?).ok()? {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

/// The lineage record of `job`, or None when `job` is not a job id or its record is absent or
/// unreadable.
pub fn read_lineage(paths: &Paths, job: &str) -> Option<LineageRecord> {
    if !is_job(job) {
        return None;
    }
    read_object(&paths.lineage_path(job)).map(LineageRecord::from)
}

fn starts_entries(paths: &Paths) -> Vec<String> {
    let Ok(dir) = std::fs::read_dir(paths.starts_dir()) else { return Vec::new() };
    let mut names: Vec<String> = dir.flatten().filter_map(|e| e.file_name().into_string().ok()).collect();
    names.sort();
    names
}

/// Every readable, non-empty lineage record, by job.
pub fn lineage_records(paths: &Paths) -> BTreeMap<String, LineageRecord> {
    starts_entries(paths)
        .into_iter()
        .filter_map(|fname| {
            let job = fname.strip_suffix(".json")?.to_string();
            let rec = is_job(&job).then(|| read_object(&paths.lineage_path(&job))).flatten()?;
            (!rec.is_empty()).then(|| (job, LineageRecord::from(rec)))
        })
        .collect()
}

/// Every readable pending record that names its session, with its path.
pub fn pending_records(paths: &Paths) -> Vec<(PathBuf, LineageRecord)> {
    starts_entries(paths)
        .into_iter()
        .filter(|fname| fname.starts_with("pending-") && fname.ends_with(".json"))
        .filter_map(|fname| {
            let path = paths.starts_dir().join(fname);
            let rec = LineageRecord::from(read_object(&path)?);
            rec.name.is_some().then_some((path, rec))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use awm_plugin_sdk::procfs;
    use serde_json::json;
    use std::cell::RefCell;
    use tempfile::TempDir;

    const JOB: &str = "abababab";
    const PARENT_JOB: &str = "cdcdcdcd";

    fn me() -> u32 {
        std::process::id()
    }

    /// A made-up parent with a made-up start time. The real parent will not do: in the build
    /// container it is init, where the walk stops.
    const PARENT: u32 = 987_600;
    const PARENT_START: &str = "4242";

    fn parent() -> u32 {
        PARENT
    }

    fn start_of(pid: u32) -> Option<String> {
        if pid == PARENT { Some(PARENT_START.into()) } else { procfs::start_ticks(pid) }
    }

    fn proc_start(pid: u32) -> String {
        start_of(pid).unwrap()
    }

    /// The real start times plus the made-up parent's, with the parent walk and the session
    /// heuristic faked. By default the walk goes from this process to its parent and then to
    /// init, and no ancestor looks like a Claude Code process: the tests may run under a real
    /// session, whose records must not leak in.
    struct Procs<'a> {
        ppid: Box<dyn Fn(u32) -> Option<u32> + 'a>,
        may: Box<dyn Fn(u32) -> bool + 'a>,
    }

    impl<'a> Procs<'a> {
        fn hermetic() -> Self {
            Self::new(|pid| Some(if pid == me() { parent() } else { 1 }), |_| false)
        }

        fn new(ppid: impl Fn(u32) -> Option<u32> + 'a, may: impl Fn(u32) -> bool + 'a) -> Self {
            Self { ppid: Box::new(ppid), may: Box::new(may) }
        }

        fn may(may: impl Fn(u32) -> bool + 'a) -> Self {
            Self { may: Box::new(may), ..Self::hermetic() }
        }

    }

    impl Processes for Procs<'_> {
        fn start(&self, pid: u32) -> Option<String> {
            start_of(pid)
        }
        fn ppid(&self, pid: u32) -> Option<u32> {
            (self.ppid)(pid)
        }
        fn may_be_a_session(&self, pid: u32) -> bool {
            (self.may)(pid)
        }
    }

    struct Home {
        _dir: TempDir,
        paths: Paths,
    }

    impl Home {
        fn new() -> Self {
            let dir = TempDir::new().unwrap();
            let paths = Paths::under(dir.path());
            for d in [paths.home.daemon_dir(), paths.home.jobs(), paths.home.sessions(), paths.starts_dir()] {
                std::fs::create_dir_all(d).unwrap();
            }
            let h = Self { _dir: dir, paths };
            h.write_roster(json!({"workers": {}}));
            h
        }

        fn roster(&self) -> PathBuf {
            self.paths.home.roster()
        }

        fn write_roster(&self, doc: Value) {
            std::fs::write(self.roster(), doc.to_string()).unwrap();
        }

        fn one_worker(&self, job: &str, worker: Value) {
            let mut workers = Object::new();
            workers.insert(job.into(), worker);
            self.write_roster(json!({"workers": workers}));
        }

        fn damage_roster(&self, text: &str) {
            std::fs::write(self.roster(), text).unwrap();
        }

        fn edit_roster(&self, edit: impl FnOnce(&mut Value)) {
            let mut doc: Value = serde_json::from_str(&std::fs::read_to_string(self.roster()).unwrap()).unwrap();
            edit(&mut doc);
            self.write_roster(doc);
        }

        fn worker(&self, name: &str) {
            self.worker_at(me(), JOB, name, None);
        }

        fn worker_at(&self, pid: u32, job: &str, name: &str, start: Option<&str>) {
            let start = start.map_or_else(|| proc_start(pid), str::to_string);
            self.one_worker(job, json!({"replPid": pid, "replProcStart": start, "dispatch": {"seed": {"name": name}}}));
            std::fs::create_dir_all(self.paths.home.jobs().join(job)).unwrap();
            std::fs::write(self.paths.home.job_state(job), json!({"name": name}).to_string()).unwrap();
        }

        fn lineage(&self, mode: Value) {
            self.lineage_of(JOB, mode);
        }

        fn lineage_of(&self, job: &str, mode: Value) {
            std::fs::write(self.paths.lineage_path(job), json!({"mode": mode}).to_string()).unwrap();
        }

        fn starts_file(&self, file: &str, text: &str) {
            std::fs::write(self.paths.starts_dir().join(file), text).unwrap();
        }

        fn record(&self, pid: u32, kind: &str, job: Option<&str>) {
            self.record_started(pid, kind, job, &proc_start(pid));
        }

        fn record_started(&self, pid: u32, kind: &str, job: Option<&str>, start: &str) {
            self.raw_record(pid, json!({"pid": pid, "procStart": start, "kind": kind, "jobId": job, "name": "x"}));
        }

        fn raw_record(&self, pid: u32, doc: Value) {
            std::fs::write(self.paths.home.session_record(pid), doc.to_string()).unwrap();
        }

        /// The test process's real parent as a cx-started background session.
        fn restricted_parent(&self, mode: &str) {
            self.record(parent(), "bg", Some(PARENT_JOB));
            self.lineage_of(PARENT_JOB, json!(mode));
        }

        fn launched_with(&self, args: Value, key: &str) {
            self.edit_roster(|doc| {
                let worker = &mut doc["workers"][JOB];
                if key == "args" {
                    worker["dispatch"]["launch"] = json!({"args": args});
                } else {
                    worker[key] = args;
                }
            });
        }

        fn launch(&self, launch: Value) {
            self.edit_roster(|doc| doc["workers"][JOB]["dispatch"]["launch"] = launch);
        }

        fn mode(&self, procs: &dyn Processes, pid: u32) -> Mode {
            mode_of(&self.paths, procs, i64::from(pid))
        }

        fn me(&self) -> Mode {
            self.mode(&Procs::hermetic(), me())
        }

    }

    fn declared(mode: &str) -> Mode {
        Mode::Declared(mode.into())
    }

    #[test]
    fn test_a_cx_started_session_answers_its_declared_mode() {
        let h = Home::new();
        h.worker("rep");
        h.lineage(json!("representative"));
        assert_eq!(h.me(), declared("representative"));
    }

    #[test]
    fn test_a_bg_session_missing_from_the_roster_uses_its_lineage() {
        let h = Home::new();
        h.record(me(), "bg", Some(JOB));
        h.lineage(json!("representative"));
        assert_eq!(h.me(), declared("representative"));
    }

    #[test]
    fn test_a_pid_with_no_record_is_not_cx_started() {
        assert_eq!(Home::new().me(), Mode::Ungated);
    }

    #[test]
    fn test_an_unreadable_roster_is_unknown() {
        let h = Home::new();
        h.worker("rep");
        h.lineage(json!("representative"));
        h.damage_roster("{ not json");
        assert_eq!(h.mode(&Procs::may(|_| true), me()), Mode::Unknown);
    }

    // A caller is held only to its own records.

    #[test]
    fn test_a_corrupt_lineage_record_of_the_caller_is_unknown() {
        let h = Home::new();
        h.worker("rep");
        h.starts_file(&format!("{JOB}.json"), "{ not json");
        assert_eq!(h.me(), Mode::Unknown);
    }

    // A job that looks cx-started but has no lineage.

    #[test]
    fn test_a_job_launched_with_cx_flags_but_no_lineage_is_unknown() {
        let cases =
            [(json!(["--permission-mode=dontAsk"]), "args"), (json!(["--restricted"]), "args"), (json!(["--permission-mode=dontAsk"]), "respawnFlags")];
        for (args, key) in cases {
            let h = Home::new();
            h.worker("rep");
            h.launched_with(args.clone(), key);
            assert_eq!(h.me(), Mode::Unknown, "{args} {key}");
        }
    }

    #[test]
    fn test_an_invalid_recorded_mode_is_unknown() {
        for bad in [json!("has space"), json!(""), json!(7), json!(null), json!("x".repeat(41))] {
            let h = Home::new();
            h.worker("rep");
            h.lineage(bad.clone());
            assert_eq!(h.me(), Mode::Unknown, "{bad}");
        }
    }

    #[test]
    fn test_a_recycled_roster_pid_is_not_taken_for_the_session() {
        let h = Home::new();
        h.worker_at(me(), JOB, "rep", Some("1"));
        h.lineage(json!("representative"));
        assert_eq!(h.me(), Mode::Ungated);
    }

    // A process a restricted session starts.

    #[test]
    fn test_a_child_with_its_own_interactive_record_inherits_its_parents_mode() {
        let h = Home::new();
        h.restricted_parent("delegate");
        h.record(me(), "interactive", None);
        assert_eq!(h.me(), declared("delegate"));
    }

    #[test]
    fn test_an_ancestor_job_with_a_damaged_lineage_makes_the_child_unknown() {
        let h = Home::new();
        h.restricted_parent("delegate");
        h.starts_file(&format!("{PARENT_JOB}.json"), "{ not json");
        h.record(me(), "interactive", None);
        assert_eq!(h.me(), Mode::Unknown);
    }

    #[test]
    fn test_no_session_ancestor_leaves_an_interactive_process_ungated() {
        let h = Home::new();
        h.record(me(), "interactive", None);
        assert_eq!(h.me(), Mode::Ungated);
    }

    #[test]
    fn test_the_walk_is_bounded_and_survives_a_cycle() {
        let h = Home::new();
        h.record(me(), "interactive", None);
        assert_eq!(h.mode(&Procs::new(|_| Some(me()), |_| false), me()), Mode::Ungated);
        let calls = RefCell::new(Vec::new());
        let counting = Procs::new(
            |pid| {
                calls.borrow_mut().push(pid);
                Some(pid + 1)
            },
            |_| false,
        );
        h.mode(&counting, me());
        assert!(calls.borrow().len() <= MAX_ANCESTRY_HOPS);
    }

    // An interactive session that took over a parked job.

    #[test]
    fn test_an_interactive_record_with_a_parked_job_keeps_the_jobs_mode() {
        let h = Home::new();
        h.worker("rep");
        h.lineage(json!("representative"));
        h.raw_record(
            me(),
            json!({"pid": me(), "procStart": proc_start(me()), "kind": "interactive", "parkedJobId": JOB, "name": "rep"}),
        );
        assert_eq!(h.me(), declared("representative"));
    }

    // A pid that does not exist.

    // An ancestor REPL with no session record.

    #[test]
    fn test_a_damaged_roster_makes_a_session_looking_ancestor_unknown() {
        let h = Home::new();
        h.damage_roster("{ not json");
        assert_eq!(h.mode(&Procs::may(|pid| pid == parent()), me()), Mode::Unknown);
    }

    // The roster is always consulted; the filter only judges a damaged one.

    // The heuristic itself is `awm_harness::claude::detect`, tested there; these check the wiring.

    #[test]
    fn test_a_process_with_neither_readable_is_assumed_to_be_a_session() {
        assert!(Procfs.may_be_a_session(1_000_000_000));
    }

    // What a cx start looks like in the roster.

    #[test]
    fn test_a_skip_permissions_cx_start_is_recognised_by_its_equals_form_flags() {
        let h = Home::new();
        h.worker("rep");
        h.launch(json!({"args": ["--dangerously-skip-permissions", "--effort=medium", "--model=sonnet[1m]"]}));
        assert_eq!(h.me(), Mode::Unknown);
    }

    #[test]
    fn test_the_pool_seed_flags_are_not_a_cx_start() {
        let h = Home::new();
        h.worker("rep");
        h.launch(json!({"args": ["--session-id", "x", "-n", "<warm badger>", "--dangerously-skip-permissions",
                                 "--allow-dangerously-skip-permissions", "--effort", "medium"]}));
        assert_eq!(h.me(), Mode::Ungated);
    }

    // A record without procStart is not trusted.

    // The record type and its readers (not in the Python suite).

    #[test]
    fn a_lineage_record_round_trips_unknown_keys_and_odd_types() {
        let doc = json!({"job": "abababab", "name": "rep", "parent": null, "mode": "worker",
                         "pinned": "yes", "started_epoch": 1.5, "suggested": {"project": "p", "scope": null},
                         "tags": ["a"], "future": {"x": 1}});
        let rec: LineageRecord = serde_json::from_value(doc.clone()).unwrap();
        assert_eq!((rec.job.as_deref(), rec.mode.as_deref(), rec.pinned, rec.parent.as_deref()), (Some("abababab"), Some("worker"), None, None));
        assert_eq!(rec.suggested, Some(Suggested { project: Some("p".into()), scope: None }));
        assert_eq!(rec.extra.get("pinned"), Some(&json!("yes")));
        let back = serde_json::to_value(&rec).unwrap();
        let mut want = doc.as_object().unwrap().clone();
        want.sort_keys();
        let mut got = back.as_object().unwrap().clone();
        got.sort_keys();
        assert_eq!(got, want);
    }

}
