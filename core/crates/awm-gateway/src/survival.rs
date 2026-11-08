//! What lets plugins outlive a gateway restart: the services journal, the gateway's own start
//! log, the state-dir lock, and the /proc checks that keep a journaled pid from being mistaken
//! for another process.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::Write;
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

pub const JOURNAL_FILE: &str = "services.json";
pub const START_LOG_FILE: &str = "gateway-starts.log";
pub const LOCK_FILE: &str = "gateway.lock";
/// Gateway starts within `START_WINDOW_S` that bring the next one up in hold mode.
pub const START_BUDGET: usize = 3;
pub const START_WINDOW_S: i64 = 600;
/// A plugin that exits this many times within `CRASH_WINDOW_S` is tripped.
pub const CRASH_LIMIT: usize = 5;
pub const CRASH_WINDOW_S: i64 = 60;

pub fn unix_now() -> i64 {
    chrono::Utc::now().timestamp()
}

/// One plugin's record. The process fields are empty while no process is journaled; the breaker
/// fields persist across process and gateway lifetimes.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct Entry {
    pub pid: Option<i32>,
    pub pgid: Option<i32>,
    /// Field 22 of `/proc/<pid>/stat`: clock ticks after boot at which the process started.
    pub start_time: Option<u64>,
    pub service_id: Option<String>,
    /// Unix seconds of recent exits, oldest first.
    #[serde(default)]
    pub exits: Vec<i64>,
    #[serde(default)]
    pub tripped: bool,
    /// The install config's `[env]` the process was spawned with, so an adopted process is
    /// judged by `stale_env` like a spawned one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub env: Option<BTreeMap<String, String>>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct File_ {
    #[serde(default)]
    boot_id: String,
    #[serde(default)]
    services: BTreeMap<String, Entry>,
}

pub struct Journal {
    path: PathBuf,
    inner: Mutex<File_>,
}

impl Journal {
    /// Read the journal. A journal written before the last boot keeps its breaker state and
    /// drops every pid, since pids restart at boot.
    pub fn load(state: &Path) -> Self {
        let path = state.join(JOURNAL_FILE);
        let mut file: File_ = std::fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
        let boot = boot_id();
        if file.boot_id != boot {
            for e in file.services.values_mut() {
                e.pid = None;
                e.pgid = None;
                e.start_time = None;
            }
            file.boot_id = boot;
        }
        Self { path, inner: Mutex::new(file) }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, File_> {
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn flush(&self, file: &File_) {
        if let Err(e) = write_atomic(&self.path, &serde_json::to_string_pretty(file).unwrap_or_default()) {
            tracing::error!("services journal {}: {e}", self.path.display());
        }
    }

    pub fn snapshot(&self) -> BTreeMap<String, Entry> {
        self.lock().services.clone()
    }

    pub fn get(&self, name: &str) -> Option<Entry> {
        self.lock().services.get(name).cloned()
    }

    pub fn tripped(&self, name: &str) -> bool {
        self.lock().services.get(name).is_some_and(|e| e.tripped)
    }

    /// Change a record, creating it if there is none. For a spawn.
    pub fn upsert(&self, name: &str, change: impl FnOnce(&mut Entry)) {
        let mut file = self.lock();
        change(file.services.entry(name.to_string()).or_default());
        file.boot_id = boot_id();
        self.flush(&file);
    }

    /// Change a record only if one exists. A service that a deliberate stop removed stays
    /// removed when its loop reports the exit afterwards.
    pub fn update_existing(&self, name: &str, change: impl FnOnce(&mut Entry)) {
        let mut file = self.lock();
        if let Some(e) = file.services.get_mut(name) {
            change(e);
            self.flush(&file);
        }
    }

    pub fn remove(&self, name: &str) {
        let mut file = self.lock();
        if file.services.remove(name).is_some() {
            self.flush(&file);
        }
    }

    pub fn clear(&self) {
        let mut file = self.lock();
        file.services.clear();
        self.flush(&file);
    }

    /// Count an exit of `name` at `now`. True when the crash breaker is now tripped.
    pub fn record_exit(&self, name: &str, now: i64) -> bool {
        let mut tripped = false;
        self.update_existing(name, |e| {
            e.exits.retain(|t| now - t <= CRASH_WINDOW_S);
            e.exits.push(now);
            if e.exits.len() >= CRASH_LIMIT {
                e.tripped = true;
            }
            tripped = e.tripped;
        });
        tripped
    }
}

/// Write `text` to `path` through a temporary file in the same folder, so a reader sees the old
/// content or the new and a crash never leaves half a file.
pub fn write_atomic(path: &Path, text: &str) -> std::io::Result<()> {
    let tmp = path.with_extension("json.tmp");
    let mut f = File::create(&tmp)?;
    f.write_all(text.as_bytes())?;
    f.sync_all()?;
    std::fs::rename(&tmp, path)
}

fn boot_id() -> String {
    std::fs::read_to_string("/proc/sys/kernel/random/boot_id").map(|s| s.trim().to_string()).unwrap_or_default()
}

/// The start time and state of a process, from `/proc/<pid>/stat`. The command name in field 2
/// may hold spaces and parentheses, so the fields are counted from the last `)`.
fn proc_stat(pid: i32) -> Option<(char, u64)> {
    let text = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let rest = &text[text.rfind(')')? + 1..];
    let mut fields = rest.split_whitespace();
    let state = fields.next()?.chars().next()?;
    // `rest` begins at field 3, so field 22 is 19 further on.
    let start = fields.nth(18)?.parse().ok()?;
    Some((state, start))
}

/// True when the process group `pgid` has lost its leader, or its leader is the journaled one
/// (a zombie). A leader with another start time means the pid was reused and the group is not ours.
pub fn group_is_ours(pgid: i32, start: u64) -> bool {
    proc_stat(pgid).is_none_or(|(_, s)| s == start)
}

pub fn start_time(pid: i32) -> Option<u64> {
    proc_stat(pid).map(|(_, start)| start)
}

/// True when `pid` is a live process that started at `start_time`. A zombie is not live.
pub fn alive_as(pid: i32, start: u64) -> bool {
    proc_stat(pid).is_some_and(|(state, s)| s == start && state != 'Z' && state != 'X')
}

/// True when any process of group `pgid` is still running. Zombies of the group do not count.
pub fn group_alive(pgid: i32) -> bool {
    let Ok(dir) = std::fs::read_dir("/proc") else { return false };
    for e in dir.flatten() {
        let Some(pid) = e.file_name().to_str().and_then(|s| s.parse::<i32>().ok()) else { continue };
        let Ok(text) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else { continue };
        let Some(rest) = text.rfind(')').map(|i| &text[i + 1..]) else { continue };
        let mut f = rest.split_whitespace();
        let (state, _ppid, group) = (f.next(), f.next(), f.next().and_then(|g| g.parse::<i32>().ok()));
        if group == Some(pgid) && !matches!(state, Some("Z") | Some("X")) {
            return true;
        }
    }
    false
}

/// Append this start to the log and return how many starts, this one included, fall within the
/// budget window. The log keeps only the starts inside the window.
pub fn log_start(state: &Path, now: i64) -> usize {
    let path = state.join(START_LOG_FILE);
    let mut times: Vec<i64> = std::fs::read_to_string(&path)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| l.trim().parse().ok())
        .filter(|t| now - t <= START_WINDOW_S)
        .collect();
    times.push(now);
    let text: String = times.iter().map(|t| format!("{t}\n")).collect();
    if let Err(e) = write_atomic(&path, &text) {
        tracing::error!("{}: {e}", path.display());
    }
    times.len()
}

/// Forget the logged starts, when an operator leaves hold mode or stops the gateway on purpose.
pub fn clear_start_log(state: &Path) {
    let _ = std::fs::remove_file(state.join(START_LOG_FILE));
}

/// The exclusive lock on a state directory, held for the life of the gateway. The file is opened
/// close-on-exec, so a plugin the gateway spawns never inherits the lock and outlives it without
/// keeping the next gateway out.
pub struct StateLock {
    _file: File,
}

impl StateLock {
    pub fn acquire(state: &Path) -> anyhow::Result<Self> {
        let path = state.join(LOCK_FILE);
        let file = std::fs::OpenOptions::new().create(true).truncate(false).read(true).write(true).open(&path)?;
        // SAFETY: flock(2) on a descriptor this struct owns.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            let holder = std::fs::read_to_string(&path).unwrap_or_default();
            anyhow::bail!("another gateway holds {} (pid {})", path.display(), holder.trim());
        }
        let _ = file.set_len(0);
        let _ = (&file).write_all(std::process::id().to_string().as_bytes());
        Ok(Self { _file: file })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_journal_survives_a_reload_and_a_removed_entry_stays_removed() {
        let dir = tempfile::tempdir().unwrap();
        let j = Journal::load(dir.path());
        j.upsert("a", |e| e.pid = Some(42));
        j.upsert("b", |e| e.service_id = Some("x".into()));
        assert_eq!(Journal::load(dir.path()).get("a").unwrap().pid, Some(42));
        j.remove("a");
        j.update_existing("a", |e| e.pid = Some(7));
        assert!(Journal::load(dir.path()).get("a").is_none());
        assert_eq!(Journal::load(dir.path()).get("b").unwrap().service_id.as_deref(), Some("x"));
    }

    #[test]
    fn five_exits_in_a_minute_trip_and_the_trip_persists() {
        let dir = tempfile::tempdir().unwrap();
        let j = Journal::load(dir.path());
        j.upsert("p", |_| {});
        for i in 0..4 {
            assert!(!j.record_exit("p", 1000 + i));
        }
        assert!(j.record_exit("p", 1004));
        assert!(Journal::load(dir.path()).tripped("p"));
        let j = Journal::load(dir.path());
        j.upsert("q", |_| {});
        assert!(!j.record_exit("q", 1000));
        assert!(!j.record_exit("q", 1100), "an exit outside the window does not add up");
        assert_eq!(j.get("q").unwrap().exits, vec![1100]);
    }

    #[test]
    fn a_process_is_known_by_pid_and_start_time() {
        let me = std::process::id() as i32;
        let start = start_time(me).expect("own stat");
        assert!(alive_as(me, start));
        assert!(!alive_as(me, start + 1), "another start time is another process");
        assert!(!alive_as(i32::MAX, 1));
        assert!(group_alive(unsafe { libc::getpgid(0) }));
    }

    #[test]
    fn a_second_lock_on_the_same_state_dir_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let first = StateLock::acquire(dir.path()).unwrap();
        assert!(StateLock::acquire(dir.path()).is_err());
        drop(first);
        assert!(StateLock::acquire(dir.path()).is_ok());
    }
}
