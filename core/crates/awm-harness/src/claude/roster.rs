//! The daemon roster `daemon/roster.json` and each job's own record `jobs/<short>/state.json`.
//!
//! The roster is the daemon's table: liveness, the PTY lane, the CLI version, the start time,
//! and the name the job was created under (`dispatch.seed.name`, which never changes). The job
//! record is the job's own, and the only one that follows a rename. A [`Job`] joins the two.

use std::fmt;
use std::path::Path;
use std::str::FromStr;

use awm_plugin_sdk::procfs;
use serde_json::{Map, Value};

use super::records::{
    as_pid, py_int, py_opt_str, py_truthy, py_truthy_str, read_json, read_json_strict, Object, Unreadable,
};
use super::ClaudeHome;

/// Remote Control names a session `cse_<id>` in the job record and serves it at
/// `APP_SESSION_URL<id>`.
pub const APP_SESSION_URL: &str = "https://claude.ai/code/session_";
const BRIDGE_PREFIX: &str = "cse_";

/// The app URL of a Remote Control session, or None when there is none.
pub fn bridge_link(bridge_session_id: Option<&str>) -> Option<String> {
    let ident = bridge_session_id?.strip_prefix(BRIDGE_PREFIX)?;
    (!ident.is_empty()).then(|| format!("{APP_SESSION_URL}{ident}"))
}

/// A Claude Code background job's short id: eight lowercase hex digits.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, serde::Serialize, serde::Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct JobId(String);

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0:?} is not a Claude Code job id (eight lowercase hex digits)")]
pub struct BadJobId(pub String);

impl JobId {
    /// `text` as a job id, if it is eight lowercase hex digits.
    pub fn parse(text: &str) -> Result<Self, BadJobId> {
        let ok = text.len() == 8 && text.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
        if ok { Ok(Self(text.to_string())) } else { Err(BadJobId(text.to_string())) }
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for JobId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl FromStr for JobId {
    type Err = BadJobId;
    fn from_str(s: &str) -> Result<Self, BadJobId> {
        Self::parse(s)
    }
}

impl TryFrom<String> for JobId {
    type Error = BadJobId;
    fn try_from(s: String) -> Result<Self, BadJobId> {
        Self::parse(&s)
    }
}

impl From<JobId> for String {
    fn from(id: JobId) -> String {
        id.0
    }
}

impl AsRef<str> for JobId {
    fn as_ref(&self) -> &str {
        &self.0
    }
}

/// The daemon roster as written: its protocol version, its supervisor and its workers by short id.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct RosterFile {
    pub proto: Option<i64>,
    pub supervisor_pid: Option<u32>,
    pub workers: Map<String, Value>,
}

impl RosterFile {
    fn from_object(doc: &Object) -> Self {
        Self {
            proto: doc.get("proto").and_then(Value::as_i64),
            supervisor_pid: as_pid(doc.get("supervisorPid")),
            workers: doc.get("workers").and_then(Value::as_object).cloned().unwrap_or_default(),
        }
    }

    /// The roster, or an empty one when it is absent, unreadable or malformed.
    pub fn read(home: &ClaudeHome) -> Self {
        Self::from_object(&read_json(&home.roster()))
    }

    /// The roster; empty only when absent, an error when present and unreadable, or when its
    /// `workers` is set to something other than an object.
    pub fn read_strict(home: &ClaudeHome) -> Result<Self, Unreadable> {
        let path = home.roster();
        let doc = read_json_strict(&path)?;
        match doc.get("workers") {
            Some(w) if py_truthy(w) && !w.is_object() => Err(Unreadable::new(path, "workers is not an object")),
            _ => Ok(Self::from_object(&doc)),
        }
    }
}

/// One background job, as the roster and its own record describe it.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Job {
    pub short: String,
    /// False when the job record is absent, which is what `claude rm` leaves for the moment
    /// before the daemon clears the roster entry.
    pub has_record: bool,
    pub name: String,
    pub tokens: i64,
    pub intent: String,
    pub needs: Option<String>,
    pub origin_cwd: Option<String>,
    pub cwd: Option<String>,
    pub first_terminal_at: Option<String>,
    pub repl_pid: Option<u32>,
    pub repl_proc_start: Option<String>,
    pub started_at_ms: Option<i64>,
    pub cli_version: Option<String>,
    pub pty_sock: Option<String>,
    pub pty_auth: Option<String>,
    pub dec_modes: Vec<i64>,
    pub session_id: Option<String>,
    pub source: Option<String>,
    pub seed_name: Option<String>,
    /// Present once Remote Control has connected, gone once it is turned off.
    pub bridge_session_id: Option<String>,
}

fn object(v: Option<&Value>) -> Option<&Object> {
    v.and_then(Value::as_object)
}

fn text_or_empty(v: Option<&Value>) -> String {
    py_truthy_str(v).unwrap_or_default()
}

fn int_or_zero(v: Option<&Value>) -> i64 {
    v.filter(|v| py_truthy(v)).and_then(py_int).unwrap_or(0)
}

fn millis(v: Option<&Value>) -> Option<i64> {
    let n = v?.as_number()?;
    n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f as i64))
}

fn int_items(v: Option<&Value>) -> Vec<i64> {
    let items = v.and_then(Value::as_array).map(Vec::as_slice).unwrap_or_default();
    items
        .iter()
        .filter_map(|m| match m {
            Value::Bool(b) => Some(i64::from(*b)),
            Value::Number(n) => n.as_i64(),
            _ => None,
        })
        .collect()
}

impl Job {
    /// The job `short`, from its roster entry `worker` joined to its own record under `home`.
    /// Its session id is its live process's: the roster keeps the first one through a `/clear`.
    pub fn from_worker(short: &str, worker: &Value, home: &ClaudeHome) -> Self {
        let st = read_json(&home.job_state(short));
        let empty = Object::new();
        let w = worker.as_object().unwrap_or(&empty);
        let repl_pid = as_pid(w.get("replPid"));
        let live = repl_pid.map(|pid| read_json(&home.session_record(pid))).filter(|r| as_pid(r.get("pid")) == repl_pid);
        let dispatch = object(w.get("dispatch")).unwrap_or(&empty);
        let seed = object(dispatch.get("seed")).unwrap_or(&empty);
        Self {
            short: short.to_string(),
            has_record: !st.is_empty(),
            name: text_or_empty(st.get("name")),
            tokens: int_or_zero(st.get("tokens")),
            intent: text_or_empty(st.get("intent")),
            needs: py_opt_str(st.get("needs")),
            origin_cwd: py_opt_str(st.get("originCwd")),
            cwd: py_opt_str(st.get("cwd")),
            first_terminal_at: py_opt_str(st.get("firstTerminalAt")),
            repl_pid,
            repl_proc_start: py_opt_str(w.get("replProcStart")),
            started_at_ms: millis(w.get("startedAt")),
            cli_version: py_opt_str(w.get("cliVersion")),
            pty_sock: py_opt_str(w.get("ptySock")),
            pty_auth: py_opt_str(w.get("ptyAuth")),
            dec_modes: int_items(w.get("decModes")),
            session_id: live.and_then(|r| py_opt_str(r.get("sessionId"))).or_else(|| py_opt_str(w.get("sessionId"))),
            source: py_opt_str(dispatch.get("source")),
            seed_name: py_opt_str(seed.get("name")),
            bridge_session_id: py_truthy_str(st.get("bridgeSessionId")),
        }
    }

    pub fn remote_control(&self) -> bool {
        self.bridge_session_id.as_deref().is_some_and(|s| !s.is_empty())
    }

    /// The app URL of this job's Remote Control session, when it has one.
    pub fn link(&self) -> Option<String> {
        bridge_link(self.bridge_session_id.as_deref())
    }
}

/// Every background job the daemon knows about, oldest first by start time.
pub fn load(home: &ClaudeHome) -> Vec<Job> {
    let roster = RosterFile::read(home);
    let mut jobs: Vec<Job> = roster
        .workers
        .iter()
        .filter(|(_, w)| w.is_object())
        .map(|(short, w)| Job::from_worker(short, w, home))
        .collect();
    jobs.sort_by_key(|j| j.started_at_ms.unwrap_or(0));
    jobs
}

/// The pid of the running Claude Code daemon, or None when there is none.
///
/// The precondition on launching: whichever process first launches a background job after a
/// reboot donates its control group and its environment to every later job, because they are
/// all children of that first daemon.
pub fn daemon_pid(home: &ClaudeHome) -> Option<u32> {
    RosterFile::read(home).supervisor_pid.filter(|pid| procfs::start_ticks(*pid).is_some())
}

/// The Remote Control session id in job `short`'s own record, or None when it is off.
pub fn job_bridge_id(home: &ClaudeHome, short: &str) -> Option<String> {
    py_truthy_str(read_json(&home.job_state(short)).get("bridgeSessionId"))
}

/// The version directory the `claude` binary resolves to, such as `2.1.268`, read off the
/// symlink rather than by running the binary.
pub fn binary_version(claude_bin: &Path) -> Option<String> {
    let target = std::fs::canonicalize(claude_bin).ok()?;
    target.file_name()?.to_str().filter(|n| !n.is_empty()).map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claude::records::tests::{home, me, DEAD_PID};
    use serde_json::json;
    use tempfile::TempDir;

    fn write_roster(h: &ClaudeHome, workers: Value, supervisor: Option<u32>) {
        let doc = json!({"proto": 1, "supervisorPid": supervisor.unwrap_or_else(me), "workers": workers});
        std::fs::write(h.roster(), doc.to_string()).unwrap();
    }

    fn write_job(h: &ClaudeHome, short: &str, state: Value) {
        std::fs::create_dir_all(h.jobs().join(short)).unwrap();
        std::fs::write(h.job_state(short), state.to_string()).unwrap();
    }

    #[test]
    fn load_joins_the_roster_to_each_jobs_own_record_and_coerces_its_fields() {
        let (_d, h) = home();
        write_roster(
            &h,
            json!({
                "newer": {"replPid": 7, "startedAt": 2000, "cliVersion": "2.1.1", "sessionId": "s2",
                          "decModes": [1, "x", 2], "dispatch": {"source": "cli", "seed": {"name": "<warm a>"}}},
                "older": {"replPid": 8, "startedAt": 1000},
            }),
            None,
        );
        write_job(&h, "newer", json!({"name": "renamed", "tokens": 5, "intent": "x", "needs": null, "cwd": "/w"}));
        let got = load(&h);
        assert_eq!(got.iter().map(|j| j.short.as_str()).collect::<Vec<_>>(), ["older", "newer"]);
        let newer = &got[1];
        assert_eq!((newer.name.as_str(), newer.tokens, newer.seed_name.as_deref()), ("renamed", 5, Some("<warm a>")));
        assert_eq!(newer.dec_modes, [1, 2]);
        assert_eq!((newer.repl_pid, newer.source.as_deref(), newer.cwd.as_deref()), (Some(7), Some("cli"), Some("/w")));
        assert_eq!(newer.needs, None);
        assert!(newer.has_record);
        assert!(!got[0].has_record && got[0].name.is_empty());
        assert_eq!(newer.session_id.as_deref(), Some("s2"));
        std::fs::create_dir_all(h.sessions()).unwrap();
        std::fs::write(h.session_record(7), json!({"pid": 7, "sessionId": "s3"}).to_string()).unwrap();
        assert_eq!(load(&h)[1].session_id.as_deref(), Some("s3"), "a /clear's new id, from the live session record");

        write_roster(&h, json!({"ab12cd34": {"replProcStart": 4242, "cliVersion": true, "decModes": [true, 3.5]}}), None);
        write_job(&h, "ab12cd34", json!({"name": true, "tokens": "12", "intent": 0, "needs": 3}));
        let j = &load(&h)[0];
        assert_eq!((j.name.as_str(), j.tokens, j.intent.as_str()), ("True", 12, ""));
        assert_eq!((j.needs.as_deref(), j.repl_proc_start.as_deref()), (Some("3"), Some("4242")));
        assert_eq!((j.cli_version.as_deref(), j.dec_modes.as_slice()), (Some("True"), &[1][..]));
    }

    #[test]
    fn a_damaged_roster_loads_empty_but_reads_strict_as_an_error() {
        let (_d, h) = home();
        assert!(load(&h).is_empty());
        assert_eq!(RosterFile::read_strict(&h), Ok(RosterFile::default()));
        assert_eq!(daemon_pid(&h), None);
        write_roster(&h, json!({"ab12cd34": {}}), None);
        let r = RosterFile::read_strict(&h).unwrap();
        assert_eq!((r.proto, r.supervisor_pid, r.workers.len()), (Some(1), Some(me()), 1));
        assert_eq!(daemon_pid(&h), Some(me()));
        write_roster(&h, json!({}), Some(DEAD_PID));
        assert_eq!(daemon_pid(&h), None, "the daemon is the supervisor only while its process runs");

        for bad in ["{not json", r#"{"workers": [1]}"#] {
            std::fs::write(h.roster(), bad).unwrap();
            assert!(load(&h).is_empty());
            assert_eq!(RosterFile::read_strict(&h).unwrap_err().path, h.roster());
            assert_eq!(RosterFile::read(&h), RosterFile::default());
        }
    }

    #[test]
    fn a_bridge_id_makes_a_job_live_with_an_app_link() {
        assert_eq!(
            bridge_link(Some("cse_01GVs3xRsnepMP5feSRbjqA8")).as_deref(),
            Some("https://claude.ai/code/session_01GVs3xRsnepMP5feSRbjqA8")
        );
        for bridge in [None, Some(""), Some("cse_"), Some("session_01A"), Some("01A")] {
            assert_eq!(bridge_link(bridge), None, "{bridge:?}");
        }
        let (_d, h) = home();
        write_roster(&h, json!({"on": {"replPid": 7}, "off": {"replPid": 8}}), None);
        write_job(&h, "on", json!({"name": "a", "bridgeSessionId": "cse_01X"}));
        write_job(&h, "off", json!({"name": "b", "bridgeSessionId": ""}));
        let jobs = load(&h);
        let on = jobs.iter().find(|j| j.short == "on").unwrap();
        let off = jobs.iter().find(|j| j.short == "off").unwrap();
        assert!(on.remote_control());
        assert_eq!(on.link().as_deref(), Some("https://claude.ai/code/session_01X"));
        assert!(!off.remote_control() && off.link().is_none());
        assert_eq!(job_bridge_id(&h, "off"), None, "a cleared bridge id reads as off");
        assert_eq!(job_bridge_id(&h, "missing"), None);
    }

    #[test]
    fn a_job_id_is_eight_lowercase_hex_digits_and_the_binary_version_is_its_directory() {
        assert_eq!(JobId::parse("ab12cd34").unwrap().as_str(), "ab12cd34");
        for bad in ["", "ab12cd3", "ab12cd345", "AB12CD34", "ab12cd3g", "ab12cd34\n"] {
            assert!(JobId::parse(bad).is_err(), "{bad:?}");
        }
        let id: JobId = serde_json::from_value(json!("0f1e2d3c")).unwrap();
        assert_eq!(serde_json::to_value(&id).unwrap(), json!("0f1e2d3c"));
        assert!(serde_json::from_value::<JobId>(json!("nope")).is_err());

        let dir = TempDir::new().unwrap();
        std::fs::create_dir(dir.path().join("2.1.268")).unwrap();
        std::os::unix::fs::symlink(dir.path().join("2.1.268"), dir.path().join("claude")).unwrap();
        assert_eq!(binary_version(&dir.path().join("claude")).as_deref(), Some("2.1.268"));
        assert_eq!(binary_version(&dir.path().join("missing")), None);
    }
}
