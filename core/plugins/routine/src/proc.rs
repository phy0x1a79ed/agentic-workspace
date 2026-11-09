//! Run one shell command in its own process group, with a time budget.
//!
//! A timeout, a cancel or a shutdown kills the whole group, so a step's children die with it.

use std::collections::HashMap;
use std::io::Read;
use std::os::fd::{FromRawFd, OwnedFd};
use std::os::unix::process::ExitStatusExt;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::sync::watch;

/// How much output a step keeps, in characters.
pub const TAIL: usize = 4000;
pub const KILL_GRACE: Duration = Duration::from_secs(5);

/// Why a run stopped before its step ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Halt {
    Cancel,
    Shutdown,
}

/// A run's stop signal: None while it may go on.
pub type HaltRx = watch::Receiver<Option<Halt>>;

#[derive(Debug, Clone, PartialEq)]
pub struct Outcome {
    /// The exit code, or minus the signal that killed the shell, as Python's `returncode`.
    pub code: Option<i32>,
    pub output: String,
    pub timed_out: bool,
    pub seconds: f64,
}

impl Outcome {
    pub fn ok(&self) -> bool {
        self.code == Some(0) && !self.timed_out
    }
}

pub(crate) fn pipe() -> std::io::Result<(OwnedFd, OwnedFd)> {
    let mut fds = [0; 2];
    // SAFETY: pipe2 writes two fresh descriptors into fds, which we then own.
    if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: both descriptors are open and owned by nothing else.
    Ok(unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) })
}

/// The last `keep` bytes a reader thread saw, and whether the pipe reached its end.
#[derive(Default)]
struct Tail {
    bytes: std::collections::VecDeque<u8>,
    done: bool,
}

fn read_tail(mut r: std::fs::File, tail: Arc<Mutex<Tail>>, keep: usize, done: std::sync::mpsc::Sender<()>) {
    let mut buf = [0u8; 65536];
    loop {
        match r.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                let mut t = tail.lock().unwrap_or_else(|p| p.into_inner());
                t.bytes.extend(&buf[..n]);
                let over = t.bytes.len().saturating_sub(keep);
                t.bytes.drain(..over);
            }
        }
    }
    tail.lock().unwrap_or_else(|p| p.into_inner()).done = true;
    let _ = done.send(());
}

fn last_chars(bytes: &[u8], n: usize) -> String {
    let text = String::from_utf8_lossy(bytes);
    let count = text.chars().count();
    text.chars().skip(count.saturating_sub(n)).collect()
}

/// Run `cmd` under bash in `cwd`, its stdout and stderr merged. The step ends on exit, on its
/// timeout, or when `halt` fires: either of the last two kills its group, SIGTERM first.
/// Returns None when `halt` stopped it.
pub async fn run(cmd: &str, cwd: &str, timeout: f64, env: &HashMap<String, String>, halt: &mut HaltRx) -> std::io::Result<Option<Outcome>> {
    let t0 = Instant::now();
    let (r, w) = pipe()?;
    let mut command = tokio::process::Command::new("bash");
    command.args(["-c", cmd]).current_dir(cwd).envs(env).stdin(Stdio::null()).stdout(Stdio::from(w.try_clone()?)).stderr(Stdio::from(w));
    awm_plugin_sdk::proc::in_group(&mut command);
    let mut child = command.spawn()?;
    // The parent's copies of the write end closed with `command`'s Stdio once spawned.
    drop(command);
    let tail = Arc::new(Mutex::new(Tail::default()));
    let (done_tx, done_rx) = std::sync::mpsc::channel();
    {
        let tail = tail.clone();
        std::thread::spawn(move || read_tail(std::fs::File::from(r), tail, 4 * TAIL, done_tx));
    }
    let budget = Duration::from_secs_f64(timeout.max(0.0));
    let mut timed_out = false;
    let mut halted = None;
    tokio::select! {
        _ = child.wait() => {}
        _ = tokio::time::sleep(budget) => timed_out = true,
        h = halt.wait_for(Option::is_some) => halted = h.ok().and_then(|h| *h),
    }
    if timed_out || halted.is_some() {
        awm_plugin_sdk::proc::stop_group(&mut child, KILL_GRACE).await;
    }
    let status = child.wait().await?;
    let _ = tokio::task::spawn_blocking(move || done_rx.recv_timeout(Duration::from_secs(2))).await;
    if halted.is_some() {
        return Ok(None);
    }
    let output = last_chars(tail.lock().unwrap_or_else(|p| p.into_inner()).bytes.make_contiguous(), TAIL);
    let code = status.code().or_else(|| status.signal().map(|s| -s));
    Ok(Some(Outcome { code, output, timed_out, seconds: (t0.elapsed().as_secs_f64() * 1000.0).round() / 1000.0 }))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Dead: no such pid, or a zombie no one has reaped yet.
    pub(crate) fn gone(pid: i32) -> bool {
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Err(_) => true,
            Ok(stat) => stat.rsplit_once(')').and_then(|(_, rest)| rest.split_whitespace().next()) == Some("Z"),
        }
    }

    fn no_halt() -> (watch::Sender<Option<Halt>>, HaltRx) {
        watch::channel(None)
    }

    #[tokio::test]
    async fn captures_both_streams_and_the_code() {
        let (_tx, mut rx) = no_halt();
        let env = HashMap::from([("X".to_string(), "42".to_string())]);
        let out = run("echo out; echo err >&2; echo $X; exit 3", "/", 10.0, &env, &mut rx).await.unwrap().unwrap();
        assert_eq!(out.code, Some(3));
        assert_eq!(out.output, "out\nerr\n42\n");
        assert!(!out.ok() && !out.timed_out);
    }

    #[tokio::test]
    async fn a_timeout_kills_the_group() {
        let (_tx, mut rx) = no_halt();
        let tmp = tempfile::tempdir().unwrap();
        let pidfile = tmp.path().join("pid");
        let cmd = format!("sleep 30 & echo $! > {}; wait", pidfile.display());
        let out = run(&cmd, "/", 0.5, &HashMap::new(), &mut rx).await.unwrap().unwrap();
        assert!(out.timed_out && !out.ok());
        let child: i32 = std::fs::read_to_string(&pidfile).unwrap().trim().parse().unwrap();
        for _ in 0..100 {
            if gone(child) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(gone(child), "the step's child died with it");
    }

    #[tokio::test]
    async fn a_halt_stops_the_step() {
        let (tx, mut rx) = no_halt();
        let t0 = Instant::now();
        let task = tokio::spawn(async move { run("sleep 30", "/", 60.0, &HashMap::new(), &mut rx).await });
        tx.send(Some(Halt::Shutdown)).unwrap();
        assert_eq!(task.await.unwrap().unwrap(), None);
        assert!(t0.elapsed() < Duration::from_secs(4));
    }
}
