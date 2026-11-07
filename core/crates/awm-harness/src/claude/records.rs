//! Claude Code's JSON records, and the per-process session record `sessions/<pid>.json`.
//!
//! Three records describe a session and they do not agree, so which one is asked matters: the
//! daemon roster and each job's `state.json` (see [`super::roster`]), and the session record
//! here, written per running REPL, interactive or background, saying how the process is hosted.
//! Records are keyed by pid and pids get recycled, so a session record is trusted only while its
//! `procStart` matches the live process's start time.
//!
//! Pure reads. Deciding what a record means for a caller stays with the caller.

use std::path::{Path, PathBuf};

use awm_plugin_sdk::procfs;
use serde_json::{Map, Value};

use super::ClaudeHome;

pub type Object = Map<String, Value>;

/// A record exists but could not be read, so what it holds is unknown.
///
/// Distinct from an absent record, which reads as empty: a caller that must not act on a guess
/// treats this as "assume live", never as "nothing there". `path` is the exact file or directory.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("cannot read {} ({why}); what it holds is unknown", path.display())]
pub struct Unreadable {
    pub path: PathBuf,
    pub why: String,
}

impl Unreadable {
    pub fn new(path: impl Into<PathBuf>, why: impl Into<String>) -> Self {
        Self { path: path.into(), why: why.into() }
    }
}

fn parse_object(path: &Path, bytes: &[u8]) -> Result<Object, Unreadable> {
    let bytes = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes);
    match serde_json::from_slice::<Value>(bytes) {
        Ok(Value::Object(map)) => Ok(map),
        Ok(_) => Err(Unreadable::new(path, "not a JSON object")),
        Err(e) => Err(Unreadable::new(path, e.to_string())),
    }
}

/// The object in `path`, or an empty one for anything else: absent, unreadable or not an object.
pub fn read_json(path: &Path) -> Object {
    std::fs::read(path).ok().and_then(|b| parse_object(path, &b).ok()).unwrap_or_default()
}

/// The object in `path`; empty only when the file is absent, an error when it cannot be read,
/// parsed, or does not hold an object.
pub fn read_json_strict(path: &Path) -> Result<Object, Unreadable> {
    match std::fs::read(path) {
        Ok(bytes) => parse_object(path, &bytes),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Object::new()),
        Err(e) => Err(Unreadable::new(path, e.to_string())),
    }
}

// Python's coercions over JSON values, so a field reads as the Python readers read it.

pub(crate) fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn py_float(f: f64) -> String {
    if f.is_nan() {
        return "nan".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf".into() } else { "-inf".into() };
    }
    let sci = format!("{f:e}");
    let (mantissa, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    if (-4..16).contains(&exp) {
        let fixed = f.to_string();
        if fixed.contains('.') { fixed } else { format!("{fixed}.0") }
    } else {
        let sign = if exp < 0 { '-' } else { '+' };
        format!("{mantissa}e{sign}{:02}", exp.unsigned_abs())
    }
}

fn py_repr_str(s: &str) -> String {
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
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn py_repr(v: &Value) -> String {
    match v {
        Value::String(s) => py_repr_str(s),
        other => py_str(other),
    }
}

/// Python's `str(v)` of a JSON value: `True`, `None`, `5.0`, `[1, 'x']`.
pub(crate) fn py_str(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => match (n.as_i64(), n.as_u64()) {
            (Some(i), _) => i.to_string(),
            (_, Some(u)) => u.to_string(),
            _ => py_float(n.as_f64().unwrap_or(f64::NAN)),
        },
        Value::String(s) => s.clone(),
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter().map(|(k, v)| format!("{}: {}", py_repr_str(k), py_repr(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

/// Python's `None if v is None else str(v)`, where an absent key is `None` too.
pub(crate) fn py_opt_str(v: Option<&Value>) -> Option<String> {
    match v {
        None | Some(Value::Null) => None,
        Some(v) => Some(py_str(v)),
    }
}

/// Python's `str(v or "") or None`: a falsy value reads as absent.
pub(crate) fn py_truthy_str(v: Option<&Value>) -> Option<String> {
    v.filter(|v| py_truthy(v)).map(py_str).filter(|s| !s.is_empty())
}

/// Python's `int(text)` for an integer literal: surrounding whitespace, a sign, `_` between digits.
pub(crate) fn py_int_str(text: &str) -> Option<i64> {
    let t = text.trim();
    let (neg, digits) = match t.as_bytes().first()? {
        b'-' => (true, &t[1..]),
        b'+' => (false, &t[1..]),
        _ => (false, t),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return None;
    }
    if !digits.bytes().all(|b| b.is_ascii_digit() || b == b'_') {
        return None;
    }
    let n: i64 = digits.replace('_', "").parse().ok()?;
    Some(if neg { -n } else { n })
}

/// Python's `int(v)` of a JSON value, or None where Python would raise.
pub(crate) fn py_int(v: &Value) -> Option<i64> {
    match v {
        Value::Bool(b) => Some(i64::from(*b)),
        Value::Number(n) => n.as_i64().or_else(|| {
            let f = n.as_f64()?;
            (f.is_finite() && f.abs() < 9.2e18).then(|| f.trunc() as i64)
        }),
        Value::String(s) => py_int_str(s),
        _ => None,
    }
}

/// A JSON integer that is a valid pid. A boolean is not one.
pub(crate) fn as_pid(v: Option<&Value>) -> Option<u32> {
    v?.as_u64().and_then(|n| u32::try_from(n).ok()).filter(|p| *p > 0)
}

/// Why a session record was not trusted.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SessionRecordError {
    #[error("no Claude Code session record for the process; it does not look like a Claude Code session")]
    Missing,
    #[error("could not read the session record: {0}")]
    Unreadable(#[from] Unreadable),
    #[error("the process is gone")]
    Gone,
    #[error(
        "the session record is stale: it describes a process started at {claimed}, but the pid's \
         process started at {live}, so the pid was recycled"
    )]
    Stale { claimed: String, live: String },
}

/// Claude Code's session record for the live process `pid`.
///
/// Refuses a record that is absent, unreadable or not an object, a pid with no running process,
/// and a record whose `procStart` names a different process than the one now holding `pid`.
pub fn read_session_record(home: &ClaudeHome, pid: u32) -> Result<Object, SessionRecordError> {
    let path = home.session_record(pid);
    let record = match std::fs::read(&path) {
        Ok(bytes) => parse_object(&path, &bytes)?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(SessionRecordError::Missing),
        Err(e) => return Err(Unreadable::new(&path, e.to_string()).into()),
    };
    let live = procfs::start_ticks(pid).ok_or(SessionRecordError::Gone)?;
    // Python reads `str(record.get("procStart", ""))`, so a null `procStart` claims "None".
    let claimed = record.get("procStart").map(py_str).unwrap_or_default();
    if !claimed.is_empty() && claimed != live {
        return Err(SessionRecordError::Stale { claimed, live });
    }
    Ok(record)
}

/// The trusted session record of `pid`, or None when there is none to trust.
pub fn session_by_pid(home: &ClaudeHome, pid: u32) -> Option<Object> {
    if pid == 0 {
        return None;
    }
    read_session_record(home, pid).ok()
}

/// The Remote Control session id in `pid`'s trusted session record: where an interactive session
/// keeps it, since it has no job record.
pub fn pid_bridge_id(home: &ClaudeHome, pid: u32) -> Option<String> {
    py_truthy_str(session_by_pid(home, pid)?.get("bridgeSessionId"))
}

/// The background job's short id when `pid` is a background REPL.
pub fn job_of_pid(home: &ClaudeHome, pid: u32) -> Option<String> {
    let rec = session_by_pid(home, pid)?;
    if rec.get("kind").and_then(Value::as_str) != Some("bg") {
        return None;
    }
    py_truthy_str(rec.get("jobId"))
}

/// The live process other than `not_pid` whose session record holds `session_uuid`, with its
/// start ticks: the process the daemon swapped in for an outdated worker, same session, new pid.
pub fn holder_of(home: &ClaudeHome, session_uuid: &str, not_pid: Option<u32>) -> Option<(u32, String)> {
    if session_uuid.is_empty() {
        return None;
    }
    let mut pids: Vec<u32> = std::fs::read_dir(home.sessions())
        .ok()?
        .flatten()
        .filter_map(|e| {
            let name = e.file_name();
            let stem = name.to_str()?.strip_suffix(".json")?;
            if stem.is_empty() || !stem.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            stem.parse().ok()
        })
        .filter(|pid| Some(*pid) != not_pid)
        .collect();
    pids.sort_unstable();
    pids.into_iter().find_map(|pid| {
        let path = home.session_record(pid);
        let record = parse_object(&path, &std::fs::read(&path).ok()?).ok()?;
        if record.get("sessionId").and_then(Value::as_str) != Some(session_uuid) {
            return None;
        }
        Some((pid, procfs::start_ticks(pid)?))
    })
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::TempDir;

    pub(crate) const UUID: &str = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0";
    pub(crate) const DEAD_PID: u32 = (1 << 22) + 12345;

    pub(crate) fn me() -> u32 {
        std::process::id()
    }

    pub(crate) fn own_start() -> String {
        procfs::start_ticks(me()).unwrap()
    }

    pub(crate) fn home() -> (TempDir, ClaudeHome) {
        let dir = TempDir::new().unwrap();
        let h = ClaudeHome::at(dir.path().join(".claude"), dir.path().join(".claude.json"));
        for sub in ["daemon", "jobs", "sessions"] {
            std::fs::create_dir_all(h.root.join(sub)).unwrap();
        }
        (dir, h)
    }

    pub(crate) fn record(h: &ClaudeHome, pid: u32, fields: Value) {
        std::fs::write(h.session_record(pid), fields.to_string()).unwrap();
    }

    #[test]
    fn a_session_record_for_a_running_pid_is_returned() {
        let (_d, h) = home();
        record(&h, me(), json!({"kind": "bg", "jobId": "ab12cd34", "sessionId": UUID, "procStart": own_start()}));
        let rec = read_session_record(&h, me()).unwrap();
        assert_eq!(rec["jobId"], "ab12cd34");
        assert_eq!(job_of_pid(&h, me()).as_deref(), Some("ab12cd34"));
    }

    #[test]
    fn an_interactive_session_has_no_job() {
        let (_d, h) = home();
        record(&h, me(), json!({"kind": "interactive", "sessionId": UUID}));
        assert!(session_by_pid(&h, me()).is_some());
        assert_eq!(job_of_pid(&h, me()), None);
    }

    #[test]
    fn a_record_that_cannot_be_trusted_is_refused() {
        type Case = (&'static str, fn(&ClaudeHome));
        let cases: [Case; 4] = [
            ("missing", |_| {}),
            ("unreadable", |h| std::fs::write(h.session_record(me()), "{").unwrap()),
            ("not an object", |h| std::fs::write(h.session_record(me()), "[1]").unwrap()),
            ("stale", |h| record(h, me(), json!({"kind": "bg", "procStart": "1"}))),
        ];
        for (why, setup) in cases {
            let (_d, h) = home();
            setup(&h);
            let err = read_session_record(&h, me()).unwrap_err();
            match why {
                "missing" => assert_eq!(err, SessionRecordError::Missing),
                "stale" => assert!(
                    matches!(&err, SessionRecordError::Stale { claimed, live } if claimed == "1" && *live == own_start())
                ),
                _ => assert!(matches!(&err, SessionRecordError::Unreadable(u) if u.path == h.session_record(me()))),
            }
            assert_eq!(session_by_pid(&h, me()), None, "{why}");
        }
    }

    #[test]
    fn a_null_proc_start_claims_none_and_is_stale() {
        let (_d, h) = home();
        record(&h, me(), json!({"kind": "bg", "procStart": null}));
        assert!(matches!(read_session_record(&h, me()), Err(SessionRecordError::Stale { claimed, .. }) if claimed == "None"));
    }

    #[test]
    fn a_dead_process_is_refused_whatever_its_record_says() {
        let (_d, h) = home();
        record(&h, DEAD_PID, json!({"kind": "bg"}));
        assert_eq!(read_session_record(&h, DEAD_PID), Err(SessionRecordError::Gone));
    }

    #[test]
    fn session_by_pid_declines_a_pid_that_is_not_one() {
        let (_d, h) = home();
        record(&h, 0, json!({"kind": "bg"}));
        assert_eq!(session_by_pid(&h, 0), None);
    }

    #[test]
    fn an_interactive_session_keeps_its_bridge_id_in_its_session_record() {
        let (_d, h) = home();
        record(&h, me(), json!({"kind": "interactive", "procStart": own_start(), "bridgeSessionId": "cse_01Q"}));
        assert_eq!(pid_bridge_id(&h, me()).as_deref(), Some("cse_01Q"));
        assert_eq!(pid_bridge_id(&h, (1 << 22) + 1), None);
    }

    #[test]
    fn the_holder_of_a_session_is_a_live_pid_other_than_the_old_one() {
        let (_d, h) = home();
        record(&h, me(), json!({"sessionId": UUID}));
        record(&h, DEAD_PID, json!({"sessionId": UUID}));
        std::fs::write(h.sessions().join("x.json"), "{}").unwrap();
        std::fs::write(h.session_record(7), "{torn").unwrap();
        assert_eq!(holder_of(&h, UUID, None), Some((me(), own_start())));
        assert_eq!(holder_of(&h, UUID, Some(me())), None);
        assert_eq!(holder_of(&h, "", None), None);
        assert_eq!(holder_of(&h, "another", None), None);
        let (_e, empty) = home();
        std::fs::remove_dir(empty.sessions()).unwrap();
        assert_eq!(holder_of(&empty, UUID, None), None);
    }

    #[test]
    fn strict_and_lenient_reads_differ_only_on_a_damaged_file() {
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("r.json");
        assert_eq!(read_json(&p), Object::new());
        assert_eq!(read_json_strict(&p), Ok(Object::new()));
        for bad in ["{torn", "[1]", "\"s\""] {
            std::fs::write(&p, bad).unwrap();
            assert_eq!(read_json(&p), Object::new());
            assert_eq!(read_json_strict(&p).unwrap_err().path, p);
        }
        std::fs::write(&p, "\u{feff}{\"a\": 1}").unwrap();
        assert_eq!(read_json_strict(&p).unwrap()["a"], 1);
        assert_eq!(read_json_strict(dir.path()).unwrap_err().path, dir.path());
    }

    #[test]
    fn coercions_match_python() {
        let cases = [
            (json!(null), "None"),
            (json!(true), "True"),
            (json!(5), "5"),
            (json!(5.0), "5.0"),
            (json!(1.5), "1.5"),
            (json!(1e20), "1e+20"),
            (json!(1.5e-5), "1.5e-05"),
            (json!(1e15), "1000000000000000.0"),
            (json!("x"), "x"),
            (json!([1, "x", null, false]), "[1, 'x', None, False]"),
            (json!({"a": "it's"}), "{'a': \"it's\"}"),
        ];
        for (v, want) in cases {
            assert_eq!(py_str(&v), want, "{v}");
        }
        assert_eq!(py_int(&json!(true)), Some(1));
        assert_eq!(py_int(&json!(5.9)), Some(5));
        assert_eq!(py_int(&json!(" -1_000 ")), Some(-1000));
        assert_eq!(py_int(&json!("1__0")), None);
        assert_eq!(py_int(&json!([1])), None);
        assert_eq!(py_truthy_str(Some(&json!(""))), None);
        assert_eq!(py_truthy_str(Some(&json!(0))), None);
        assert_eq!(py_opt_str(Some(&json!(0))).as_deref(), Some("0"));
        assert_eq!(as_pid(Some(&json!(true))), None);
        assert_eq!(as_pid(Some(&json!(-3))), None);
    }
}
