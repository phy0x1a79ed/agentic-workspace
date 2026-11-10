//! The warm pool's read-only view of itself, which the `list` pool summary answers with. It is
//! derived from the live records when asked: "the warm session" is whichever claimable one is
//! newest, never a stored pointer, because a stored pointer is the half that goes stale.

use std::collections::BTreeSet;
use std::sync::Arc;

use serde_json::{json, Map, Value};
use tokio::sync::Mutex;

use crate::lineage::Processes;
use crate::paths::Paths;
use crate::sessions::{self, age_s, is_untouched, Session, Sessions};

/// Serialises a claim against the removal half of a reconcile tick, in this one process. Two
/// claims arriving together take it in turn: the first moves the session, which stamps its
/// origin directory, and the second then finds nothing claimable and answers with a miss.
pub static LOCK: Mutex<()> = Mutex::const_new(());

/// Which sessions a terminal is attached to. No record says so; the attaching argv in the
/// process table is the whole signal, so tests fake it.
pub trait Terminals {
    /// The short ids of the sessions a `claude attach` holds right now.
    fn attached(&self) -> BTreeSet<String>;
}

/// The real process table.
#[derive(Debug, Clone, Copy, Default)]
pub struct LiveTerminals;

impl Terminals for LiveTerminals {
    fn attached(&self) -> BTreeSet<String> {
        sessions::attached_shorts()
    }
}

/// What the pool reads through: cch's paths, the process table that says whether a session's
/// process still runs, and the terminals attached to sessions.
#[derive(Debug, Clone)]
pub struct Pool<P, T = LiveTerminals> {
    pub paths: Arc<Paths>,
    pub procs: P,
    pub terminals: T,
}

impl<P: Processes, T: Terminals> Pool<P, T> {
    pub fn new(paths: Arc<Paths>, procs: P, terminals: T) -> Self {
        Self { paths, procs, terminals }
    }

    /// The session predicates over this pool's paths and process table.
    pub fn sessions(&self) -> Sessions<'_> {
        Sessions::new(&self.paths, &self.procs)
    }
}

impl<P, T> Pool<P, T>
where
    P: Processes + Clone + Send + Sync + 'static,
    T: Terminals + Clone + Send + Sync + 'static,
{
    /// Run `f` over a copy of the pool on the blocking pool: the roster, the job records and the
    /// process table are all synchronous file reads.
    pub async fn read<R: Send + 'static>(&self, f: impl FnOnce(&Self) -> R + Send + 'static) -> R {
        let pool = self.clone();
        match tokio::task::spawn_blocking(move || f(&pool)).await {
            Ok(out) => out,
            Err(e) => std::panic::resume_unwind(e.into_panic()),
        }
    }
}

/// The claimable sessions in `all`, newest first: the first is the one to hand out.
///
/// Newest rather than oldest because rotation replaces before it removes, so the pool may hold
/// an old session on its way out beside its replacement, and the newest gives its taker the most
/// time before the daemon retires it.
pub fn warm(all: Vec<Session>, pool: &Sessions<'_>, version: Option<&str>, now: Option<f64>) -> Vec<Session> {
    let mut out: Vec<Session> = all.into_iter().filter(|s| pool.claimable(s, version, now)).collect();
    out.sort_by_key(|s| std::cmp::Reverse(s.started_at_ms.unwrap_or(0)));
    out
}

/// Why `s` cannot be handed out, in a word a person can act on, or None if it can.
///
/// Ordered by what the reader can do about it. "taken" comes late: a claim moves every session
/// it hands out, so "claimed" is the news, and "taken" is left for a session the ledger no longer
/// calls a spare although nothing moved it.
pub fn why_not(s: &Session, pool: &Sessions<'_>, version: Option<&str>, now: Option<f64>) -> Option<String> {
    let Some(version) = version else { return Some("no claude binary".into()) };
    let why = if !s.has_record {
        "deleted".to_string()
    } else if !pool.is_alive(s) {
        "gone".into()
    } else if s.tokens != 0 {
        "prompted".into()
    } else if s.origin_cwd.is_some() {
        "claimed".into()
    } else if !pool.is_ours(s) {
        "taken".into()
    } else if !is_untouched(s) {
        "used".into()
    } else if s.cli_version.as_deref() != Some(version) {
        format!("binary {}", s.cli_version.as_deref().unwrap_or("None"))
    } else if age_s(s, now) >= pool.paths.rotate_age_s {
        "aged out".into()
    } else {
        return None;
    };
    Some(why)
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

/// What [`status`] reports for the loop when none is running.
fn no_loop() -> Map<String, Value> {
    let v = json!({"loop": "not running", "holds_lock": false, "last_tick": null});
    v.as_object().cloned().unwrap_or_default()
}

/// The whole pool in one reply at epoch second `now`: what is held, and whether it can be held.
/// `loop_status` is the reconcile loop's own status, merged in.
///
/// It lists everything the ledger holds: where a session went is the question status exists to
/// answer, and dropping it the instant it is claimed answers nothing.
pub fn status<P: Processes, T: Terminals>(
    pool: &Pool<P, T>,
    now: f64,
    loop_status: Option<Map<String, Value>>,
) -> Value {
    let s = pool.sessions();
    let version = s.binary_version();
    let pid = s.daemon_pid();
    let attached = pool.terminals.attached();
    let now = Some(now);
    let mut rows: Vec<Value> = s
        .load()
        .into_iter()
        .filter(|x| x.has_record && (s.is_ours(x) || s.was_ours(x)))
        .map(|x| {
            let v = version.as_deref();
            json!({
                "session_id": x.canonical(),
                "name": x.name,
                "age_s": round1(age_s(&x, now)),
                "alive": s.is_alive(&x),
                "cwd": x.cwd,
                "cli_version": x.cli_version,
                "claimable": s.claimable(&x, v, now),
                "attached": attached.contains(&x.short),
                "why_not": why_not(&x, &s, v, now),
                "removable": s.removable(&x, v, now, Some(&attached)),
            })
        })
        .collect();
    rows.sort_by(|a, b| a["age_s"].as_f64().unwrap_or(0.0).total_cmp(&b["age_s"].as_f64().unwrap_or(0.0)));
    let warm: Vec<Value> = rows.iter().filter(|r| r["claimable"] == true).map(|r| r["session_id"].clone()).collect();
    let mut out = json!({
        "warm": warm,
        "want": pool.paths.want,
        "sessions": rows,
        "daemon_pid": pid,
        "seeding": if pid.is_some() { "ok" } else { "refused: no claude code daemon is running" },
        "binary_version": version,
        "rotate_age_s": pool.paths.rotate_age_s,
    });
    if let Some(map) = out.as_object_mut() {
        map.extend(loop_status.unwrap_or_else(no_loop));
    }
    out
}

/// A synthesized box of five pool sessions, shared by the pool modules' tests.
#[cfg(test)]
pub(crate) mod testing {
    use std::collections::BTreeSet;
    use std::path::{Path, PathBuf};
    use std::sync::Arc;
    use std::time::{SystemTime, UNIX_EPOCH};

    use serde_json::{json, Value};
    use tempfile::TempDir;

    use super::{Pool, Terminals};
    use crate::ledger::Stage;
    use crate::lineage::Processes;
    use crate::paths::Paths;

    pub const VERSION: &str = "2.1.268";
    pub const OLD_VERSION: &str = "2.1.267";
    pub const ALIVE_PID: u32 = 4242;
    pub const ALIVE_START: &str = "777";

    /// One live process, `ALIVE_PID`; every other pid is gone.
    #[derive(Debug, Clone, Copy, Default)]
    pub struct Table;

    impl Processes for Table {
        fn start(&self, pid: u32) -> Option<String> {
            (pid == ALIVE_PID).then(|| ALIVE_START.to_string())
        }
        fn ppid(&self, _: u32) -> Option<u32> {
            None
        }
        fn may_be_a_session(&self, _: u32) -> bool {
            false
        }
    }

    /// No terminal is attached to anything.
    #[derive(Debug, Clone, Copy, Default)]
    pub struct NoTerminals;

    impl Terminals for NoTerminals {
        fn attached(&self) -> BTreeSet<String> {
            BTreeSet::new()
        }
    }

    /// A temporary Claude home holding five sessions: a fresh spare (`warmfresh`), a spare
    /// somebody renamed and prompted (`adopted`), a claim abandoned before its first prompt
    /// (`stranded`), the same claim after its process died (`strandeddead`), and a claim that
    /// has just been made (`claimedfresh`). The newest starts five seconds before the box is made.
    pub struct TestBox {
        _dir: TempDir,
        pub root: PathBuf,
        pub paths: Paths,
        pub newest_ms: i64,
    }

    pub const SHORTS: [&str; 5] = ["warmfresh", "adopted", "stranded", "strandeddead", "claimedfresh"];

    /// The canonical session id of the box's session `i`.
    pub fn id_of(i: usize) -> String {
        format!("cc0000000000004000800000000000000{i}")
    }

    /// The short of the box's session with canonical id `id`.
    pub fn short_of(id: &str) -> String {
        SHORTS[(0..SHORTS.len()).find(|&i| id_of(i) == id).unwrap()].to_string()
    }

    fn now_ms() -> i64 {
        SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64)
    }

    fn write_json(path: &Path, v: &Value) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, v.to_string()).unwrap();
    }

    impl TestBox {
        pub fn new() -> Self {
            let dir = TempDir::new().unwrap();
            let root = dir.path().canonicalize().unwrap();
            let mut paths = Paths::under(&root);
            let versions = root.join("versions").join(VERSION);
            std::fs::create_dir_all(&versions).unwrap();
            std::fs::create_dir_all(root.join("bin")).unwrap();
            std::os::unix::fs::symlink(&versions, root.join("bin").join("claude")).unwrap();
            paths.claude_bin = root.join("bin").join("claude");
            paths.seed_dir = root.join("seed");
            std::fs::create_dir_all(&paths.seed_dir).unwrap();
            paths.pool_forced = Some(true);
            let newest_ms = now_ms() - 5000;
            let b = Self { _dir: dir, root, paths, newest_ms };
            b.write_box();
            b
        }

        fn write_box(&self) {
            let seed = self.paths.seed_dir.to_string_lossy().into_owned();
            let n = self.newest_ms;
            // (short, seed name, cli version, alive, started ms, ledger stage); the session id of
            // row i is [`id_of`]`(i)`.
            let workers = [
                ("warmfresh", "dunlin", VERSION, true, n, Stage::Spare),
                ("adopted", "zorilla", VERSION, true, n - 5_352_669, Stage::Kept),
                ("stranded", "saiga", OLD_VERSION, true, n - 57_064_857, Stage::Claimed),
                ("strandeddead", "saiga", OLD_VERSION, false, n - 57_064_857, Stage::Claimed),
                ("claimedfresh", "vole", VERSION, true, n - 30_000, Stage::Claimed),
            ];
            let mut ledger = crate::ledger::Ledger::new();
            let mut map = serde_json::Map::new();
            for (i, (short, seed_name, cli, alive, started, stage)) in workers.into_iter().enumerate() {
                let (pid, start) = if alive { (ALIVE_PID, ALIVE_START) } else { (2, "1") };
                ledger.insert(id_of(i), stage);
                map.insert(
                    short.into(),
                    json!({
                        "replPid": pid,
                        "replProcStart": start,
                        "startedAt": started,
                        "cliVersion": cli,
                        "ptySock": format!("/nonexistent/pty-{i}.sock"),
                        "ptyAuth": "not-a-secret",
                        "sessionId": format!("00000000-0000-4000-8000-00000000000{i}"),
                        "dispatch": {"source": "cli", "seed": {"name": seed_name}},
                    }),
                );
            }
            std::fs::create_dir_all(self.paths.service_dir.as_path()).unwrap();
            std::fs::write(self.paths.ledger_path(), serde_json::to_vec(&ledger).unwrap()).unwrap();
            let roster = json!({"proto": 1, "supervisorPid": std::process::id(), "workers": map});
            write_json(&self.paths.home.roster(), &roster);
            let pristine = |name: &str, origin: Option<&str>, cwd: &str| {
                json!({"name": name, "intent": "", "needs": "send a prompt to start", "originCwd": origin,
                       "cwd": cwd, "firstTerminalAt": null, "state": "idle"})
            };
            let states = [
                ("warmfresh", pristine("dunlin", None, &seed)),
                (
                    "adopted",
                    json!({"name": "pair session", "tokens": 31_000, "intent": "", "originCwd": "/work/alpha",
                           "cwd": "/work/alpha", "firstTerminalAt": "2026-01-01T00:00:00.000Z", "state": "done"}),
                ),
                ("stranded", pristine("saiga", Some("/work/beta"), "/work/beta")),
                ("strandeddead", pristine("saiga", Some("/work/beta"), "/work/beta")),
                ("claimedfresh", pristine("vole", Some("/work/gamma"), "/work/gamma")),
            ];
            for (short, state) in states {
                write_json(&self.paths.home.job_state(short), &state);
            }
        }

        /// The pool over this box, with `adjust` applied to its paths first.
        pub fn pool_with(&self, adjust: impl FnOnce(&mut Paths)) -> Pool<Table, NoTerminals> {
            let mut paths = self.paths.clone();
            adjust(&mut paths);
            Pool::new(Arc::new(paths), Table, NoTerminals)
        }

        pub fn pool(&self) -> Pool<Table, NoTerminals> {
            self.pool_with(|_| {})
        }

        /// A clock reading a minute after the newest session started.
        pub fn a_minute_on(&self) -> f64 {
            self.newest_ms as f64 / 1000.0 + 60.0
        }

        /// The wall clock now.
        pub fn now(&self) -> f64 {
            now_ms() as f64 / 1000.0
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::{TestBox, VERSION};
    use super::*;

    fn rows(b: &TestBox) -> std::collections::HashMap<String, Value> {
        let out = status(&b.pool(), b.now(), None);
        out["sessions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| (super::testing::short_of(r["session_id"].as_str().unwrap()), r.clone()))
            .collect()
    }

    #[test]
    fn test_status_names_the_reason_each_session_is_unavailable() {
        let b = TestBox::new();
        let rows = rows(&b);
        assert_eq!(rows["adopted"]["why_not"], "prompted");
        assert_eq!(rows["stranded"]["why_not"], "claimed");
        assert_eq!(rows["claimedfresh"]["why_not"], "claimed");
        assert_eq!(rows["strandeddead"]["why_not"], "gone");
    }

    #[test]
    fn the_newest_claimable_session_comes_first() {
        let b = TestBox::new();
        let pool = b.pool();
        let s = pool.sessions();
        let mut all = s.load();
        let mut twin = all.iter().find(|x| x.short == "warmfresh").unwrap().clone();
        twin.short = "warmolder".into();
        twin.started_at_ms = twin.started_at_ms.map(|ms| ms - 1000);
        all.push(twin);
        let order: Vec<String> = warm(all, &s, Some(VERSION), Some(b.now())).into_iter().map(|x| x.short).collect();
        assert_eq!(order, ["warmfresh", "warmolder"]);
    }

}
