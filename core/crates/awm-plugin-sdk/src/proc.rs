//! How a service parents a child process, signals and stops its group, or hands it to the
//! user's systemd manager (`gatewayclient/proc.py`).

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

/// The environment `systemd-run --user` needs to reach this user's manager, or None when there
/// is none. A transient unit of the user manager lives outside the gateway's cgroup, so a restart
/// of the gateway's own unit does not kill it.
pub fn user_manager_env() -> Option<Vec<(String, String)>> {
    let on_path = std::env::var_os("PATH")
        .is_some_and(|p| std::env::split_paths(&p).any(|d| d.join("systemd-run").is_file()));
    if !on_path {
        return None;
    }
    // SAFETY: getuid cannot fail.
    let uid = unsafe { libc::getuid() };
    let runtime = std::env::var("XDG_RUNTIME_DIR").ok().filter(|r| !r.is_empty()).unwrap_or_else(|| format!("/run/user/{uid}"));
    use std::os::unix::fs::FileTypeExt;
    let bus = Path::new(&runtime).join("bus");
    if !std::fs::metadata(&bus).is_ok_and(|m| m.file_type().is_socket()) {
        return None;
    }
    Some(vec![
        ("XDG_RUNTIME_DIR".into(), runtime.clone()),
        ("DBUS_SESSION_BUS_ADDRESS".into(), format!("unix:path={runtime}/bus")),
    ])
}

/// `argv` as a transient unit `unit` of the user's manager, with `env` set inside it, or None
/// when this user has no manager to hand it to.
pub fn in_unit(unit: &str, argv: &[String], env: &[(String, String)]) -> Option<tokio::process::Command> {
    let manager = user_manager_env()?;
    let mut cmd = tokio::process::Command::new("systemd-run");
    cmd.args(["--user", "--collect", "--quiet", &format!("--unit={unit}")]);
    for (k, v) in env {
        cmd.arg(format!("--setenv={k}={v}"));
    }
    cmd.arg("--").args(argv).envs(manager);
    Some(cmd)
}

/// Make `cmd`'s child its own session and process group, and have it get SIGTERM when its
/// parent dies.
///
/// The death signal follows the thread that forks the child, not the process. Spawn from a
/// long-lived thread: a child spawned inside `spawn_blocking` dies when that pool thread
/// retires after its idle timeout.
pub fn in_group(cmd: &mut tokio::process::Command) -> &mut tokio::process::Command {
    // SAFETY: the hook calls only async-signal-safe functions (setsid, prctl, getppid, _exit).
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM as libc::c_ulong, 0, 0, 0);
            // A parent that exited between the fork and prctl leaves nothing to signal the child.
            if libc::getppid() == 1 {
                libc::_exit(1);
            }
            Ok(())
        })
    }
}

/// Signal `pid`'s whole process group, or `pid` alone when its group is gone.
pub fn signal_group(pid: u32, sig: i32) {
    let pid = pid as libc::pid_t;
    // SAFETY: getpgid and kill take plain integers and report failure by return value.
    unsafe {
        let pgid = libc::getpgid(pid);
        if pgid <= 0 || libc::kill(-pgid, sig) != 0 {
            libc::kill(pid, sig);
        }
    }
}

/// Stop a child started [`in_group`]: SIGTERM to its group, then SIGKILL after `timeout`.
pub async fn stop_group(child: &mut tokio::process::Child, timeout: Duration) {
    let Some(pid) = child.id() else { return };
    signal_group(pid, libc::SIGTERM);
    if tokio::time::timeout(timeout, child.wait()).await.is_err() {
        signal_group(pid, libc::SIGKILL);
        let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
    }
}

/// The last `tail` lines of a child's log file.
pub fn log_tail(path: &Path, tail: usize) -> String {
    match std::fs::read(path) {
        Ok(bytes) => {
            let text = String::from_utf8_lossy(&bytes);
            let lines: Vec<&str> = text.lines().collect();
            lines[lines.len().saturating_sub(tail)..].join("\n")
        }
        Err(e) => format!("(no log at {}: {e})", path.display()),
    }
}

/// One supervised child, started [`in_group`] with its output appended to a log file
/// (`proc.py` `GroupChild`). A hold keeps [`GroupChild::keep_running`] from bringing it back;
/// the next start releases it.
pub struct GroupChild {
    pub name: String,
    pub log_file: PathBuf,
    state: tokio::sync::Mutex<ChildState>,
}

#[derive(Default)]
struct ChildState {
    child: Option<tokio::process::Child>,
    held: bool,
    started_at: Option<u64>,
}

pub const STOP_TIMEOUT: Duration = Duration::from_secs(10);

impl GroupChild {
    pub fn new(name: &str, log_file: PathBuf) -> Arc<Self> {
        Arc::new(Self { name: name.into(), log_file, state: Default::default() })
    }

    /// Start `cmd` unless the child runs already, and release any hold.
    pub async fn start(&self, mut cmd: tokio::process::Command) -> std::io::Result<Value> {
        let mut st = self.state.lock().await;
        st.held = false;
        if let Some(child) = st.child.as_mut() {
            if child.try_wait()?.is_none() {
                return Ok(json!({"action": "already-running", "running": true, "pid": child.id()}));
            }
        }
        if let Some(dir) = self.log_file.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let log = std::fs::OpenOptions::new().create(true).append(true).open(&self.log_file)?;
        in_group(&mut cmd).stdin(std::process::Stdio::null()).stdout(log.try_clone()?).stderr(log);
        let child = cmd.spawn()?;
        let pid = child.id();
        st.child = Some(child);
        st.started_at = SystemTime::now().duration_since(UNIX_EPOCH).ok().map(|d| d.as_secs());
        tracing::info!("{}: started pid {pid:?}", self.name);
        Ok(json!({"action": "started", "running": true, "pid": pid}))
    }

    /// Stop the child: SIGTERM to its group, SIGKILL after `timeout`. With `hold`, keep
    /// [`GroupChild::keep_running`] from bringing it back until the next start.
    pub async fn stop(&self, timeout: Option<Duration>, hold: bool) -> Value {
        let mut st = self.state.lock().await;
        st.held = hold;
        st.started_at = None;
        let Some(mut child) = st.child.take() else { return json!({"action": "not-running", "running": false}) };
        if child.try_wait().ok().flatten().is_some() {
            return json!({"action": "not-running", "running": false});
        }
        stop_group(&mut child, timeout.unwrap_or(STOP_TIMEOUT)).await;
        json!({"action": "stopped", "running": false})
    }

    pub async fn running(&self) -> bool {
        let mut st = self.state.lock().await;
        st.child.as_mut().is_some_and(|c| matches!(c.try_wait(), Ok(None)))
    }

    pub async fn status(&self) -> Value {
        let running = self.running().await;
        let st = self.state.lock().await;
        json!({"name": self.name, "running": running, "held": st.held, "pid": st.child.as_ref().and_then(|c| c.id()),
               "started_at": st.started_at, "log_file": self.log_file})
    }

    pub fn logs(&self, tail: usize) -> String {
        log_tail(&self.log_file, tail)
    }

    /// Keep the child up for good: start it with `make()` whenever it is down and not held,
    /// checking every `every`.
    pub fn keep_running<F>(self: &Arc<Self>, every: Duration, make: F) -> tokio::task::JoinHandle<()>
    where
        F: Fn() -> tokio::process::Command + Send + Sync + 'static,
    {
        let me = self.clone();
        tokio::spawn(async move {
            loop {
                let held = me.state.lock().await.held;
                if !held && !me.running().await {
                    if let Err(e) = me.start(make()).await {
                        tracing::error!("{}: start failed: {e}", me.name);
                    }
                }
                tokio::time::sleep(every).await;
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_group_child_stops_with_its_children() {
        let mut cmd = tokio::process::Command::new("sh");
        cmd.args(["-c", "sleep 30 & sleep 30"]);
        let mut child = in_group(&mut cmd).spawn().unwrap();
        let pid = child.id().unwrap() as i32;
        // SAFETY: getpgid on our own child.
        assert_eq!(unsafe { libc::getpgid(pid) }, pid, "the child leads its own group");
        let started = std::time::Instant::now();
        stop_group(&mut child, Duration::from_secs(3)).await;
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(child.try_wait().unwrap().is_some());
    }

    #[tokio::test]
    async fn a_held_child_stays_down() {
        let tmp = tempfile::tempdir().unwrap();
        let child = GroupChild::new("t", tmp.path().join("logs/t.log"));
        let make = || {
            let mut c = tokio::process::Command::new("sh");
            c.args(["-c", "echo up; exec sleep 30"]);
            c
        };
        let keeper = child.keep_running(Duration::from_millis(20), make);
        for _ in 0..100 {
            if child.running().await {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(child.running().await);
        assert_eq!(child.stop(Some(Duration::from_secs(2)), true).await["action"], "stopped");
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(!child.running().await, "a hold keeps it down");
        assert!(child.logs(5).contains("up"));
        keeper.abort();
        child.stop(None, false).await;
    }

}
