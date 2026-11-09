//! Blocking shell and git calls for node rollout, which runs as a command, not a service.

use std::collections::HashMap;
use std::io::Read;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub struct Ran {
    /// The exit code, or minus the signal that killed the shell.
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
}

impl Ran {
    pub fn ok(&self) -> bool {
        self.code == 0 && !self.timed_out
    }
}

/// `git -C cwd args`, with `input` on its stdin.
pub fn git(cwd: &Path, args: &[&str], env: &HashMap<String, String>, input: Option<&str>) -> Ran {
    let mut cmd = Command::new("git");
    cmd.arg("-C").arg(cwd).args(args).envs(env);
    cmd.stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() }).stdout(Stdio::piped()).stderr(Stdio::piped());
    let child = cmd.spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => return Ran { code: 127, stdout: String::new(), stderr: format!("git: {e}"), timed_out: false },
    };
    if let (Some(text), Some(mut stdin)) = (input, child.stdin.take()) {
        use std::io::Write;
        let _ = stdin.write_all(text.as_bytes());
    }
    match child.wait_with_output() {
        Ok(out) => Ran {
            code: out.status.code().unwrap_or_else(|| -out.status.signal().unwrap_or(0)),
            stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
            timed_out: false,
        },
        Err(e) => Ran { code: 127, stdout: String::new(), stderr: format!("git: {e}"), timed_out: false },
    }
}

fn kill_group(pid: u32, sig: i32) {
    // SAFETY: kill takes plain integers; a gone group is an error we ignore.
    unsafe {
        libc::kill(-(pid as i32), sig);
    }
}

/// Run `cmd` under bash in its own process group, stdout and stderr merged into `stdout`,
/// with stdin closed. Past `timeout` the group gets SIGTERM, then SIGKILL after `grace`.
pub fn bash(cmd: &str, cwd: &Path, timeout: Duration, env: &HashMap<String, String>, grace: Duration) -> Ran {
    let fail = |what: &str, e: std::io::Error| Ran { code: 127, stdout: format!("{what}: {e}"), stderr: String::new(), timed_out: false };
    let (r, w) = match pipe() {
        Ok(p) => p,
        Err(e) => return fail("pipe", e),
    };
    let w2 = match w.try_clone() {
        Ok(w2) => w2,
        Err(e) => return fail("pipe", e),
    };
    let mut command = Command::new("bash");
    command.args(["-c", cmd]).current_dir(cwd).envs(env).stdin(Stdio::null()).stdout(Stdio::from(w2)).stderr(Stdio::from(w)).process_group(0);
    let spawned = command.spawn();
    // Close the parent's copies of the write end, so the reader sees the end of the output.
    drop(command);
    let mut child = match spawned {
        Ok(c) => c,
        Err(e) => return fail("bash", e),
    };
    let buf = Arc::new(Mutex::new(Vec::new()));
    let reader = {
        let buf = buf.clone();
        std::thread::spawn(move || {
            let mut f = std::fs::File::from(r);
            let mut chunk = [0u8; 65536];
            while let Ok(n) = f.read(&mut chunk) {
                if n == 0 {
                    break;
                }
                buf.lock().unwrap_or_else(|p| p.into_inner()).extend_from_slice(&chunk[..n]);
            }
        })
    };
    let (t0, pid) = (Instant::now(), child.id());
    let mut term_at: Option<Instant> = None;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Some(st),
            Ok(None) => {}
            Err(_) => break None,
        }
        if term_at.is_none() && t0.elapsed() > timeout {
            kill_group(pid, libc::SIGTERM);
            term_at = Some(Instant::now());
        }
        if term_at.is_some_and(|t| t.elapsed() > grace) {
            kill_group(pid, libc::SIGKILL);
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    let timed_out = term_at.is_some();
    if timed_out {
        kill_group(pid, libc::SIGKILL);
    }
    // A child left in the background may hold the pipe open: wait for its end a little only.
    let deadline = Instant::now() + Duration::from_secs(2);
    while !reader.is_finished() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    let out = String::from_utf8_lossy(&buf.lock().unwrap_or_else(|p| p.into_inner())).into_owned();
    let code = status.map(|s| s.code().unwrap_or_else(|| -s.signal().unwrap_or(0))).unwrap_or(-1);
    Ran { code, stdout: out, stderr: String::new(), timed_out }
}

fn pipe() -> std::io::Result<(std::os::fd::OwnedFd, std::os::fd::OwnedFd)> {
    use std::os::fd::FromRawFd;
    let mut fds = [0; 2];
    // SAFETY: pipe2 writes two fresh descriptors into fds, which we then own.
    if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: both descriptors are open and owned by nothing else.
    Ok(unsafe { (std::os::fd::OwnedFd::from_raw_fd(fds[0]), std::os::fd::OwnedFd::from_raw_fd(fds[1])) })
}
