//! The local backend: each job runs detached from the provider in its own session, so a job
//! outlives a dropped link or a restarted provider.

use crate::backend::{Backend, DoneTx, JobSpec};
use async_trait::async_trait;
use baitan_proto::{PromiseId, Resources};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::os::fd::{FromRawFd, OwnedFd};
use std::path::Path;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tokio::io::unix::AsyncFd;
use tokio::io::Interest;
use tokio::process::Command;

struct Job {
    pid: i32,
    cancelled: Arc<AtomicBool>,
}

pub struct LocalBackend {
    done: DoneTx,
    jobs: Arc<Mutex<HashMap<PromiseId, Job>>>,
}

impl LocalBackend {
    pub fn new(done: DoneTx) -> Self {
        LocalBackend { done, jobs: Default::default() }
    }

    fn watch(&self, promise: PromiseId, pid: i32, workdir: std::path::PathBuf, cancelled: Arc<AtomicBool>, child: Option<tokio::process::Child>) {
        self.jobs.lock().unwrap().insert(promise.clone(), Job { pid, cancelled: cancelled.clone() });
        let jobs = self.jobs.clone();
        let done = self.done.clone();
        tokio::spawn(async move {
            match child {
                Some(mut c) => drop(c.wait().await),
                None => pid_exit(pid).await,
            }
            jobs.lock().unwrap().remove(&promise);
            if !cancelled.load(Ordering::SeqCst) {
                let _ = done.send((promise, read_exit(&workdir).unwrap_or(-1)));
            }
        });
    }
}

fn read_exit(workdir: &Path) -> Option<i32> {
    std::fs::read_to_string(workdir.join("exit")).ok()?.trim().parse().ok()
}

fn alive(pid: i32) -> bool {
    unsafe { libc::kill(pid, 0) == 0 }
}

/// Resolves when the process ends, woken by the kernel through a pidfd.
async fn pid_exit(pid: i32) {
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) } as i32;
    if fd < 0 {
        return;
    }
    let owned = unsafe { OwnedFd::from_raw_fd(fd) };
    if let Ok(afd) = unsafe { AsyncFd::register_with_interest(owned, Interest::READABLE) } {
        let _ = afd.readable().await;
    }
}

#[async_trait]
impl Backend for LocalBackend {
    async fn start_eta(&self, _res: &Resources) -> f64 {
        0.0
    }

    /// Idempotent per work dir: a finished job reports its `exit` file, a live one is awaited
    /// again, and only a job with neither is started.
    async fn launch(&self, job: JobSpec) -> anyhow::Result<()> {
        let JobSpec { promise, workdir, .. } = job;
        let cancelled = Arc::new(AtomicBool::new(false));
        if let Some(code) = read_exit(&workdir) {
            let _ = self.done.send((promise, code));
            return Ok(());
        }
        let pid_file = workdir.join("pid");
        if let Some(pid) = std::fs::read_to_string(&pid_file).ok().and_then(|s| s.trim().parse::<i32>().ok()) {
            if alive(pid) {
                self.watch(promise, pid, workdir, cancelled, None);
                return Ok(());
            }
        }
        let mut cmd = Command::new("sh");
        cmd.arg("-c")
            .arg("sh ./run.sh >log 2>&1; echo $? > exit.tmp; mv exit.tmp exit")
            .current_dir(&workdir)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        let child = cmd.spawn()?;
        let pid = child.id().ok_or_else(|| anyhow::anyhow!("job exited before it was tracked"))? as i32;
        std::fs::write(&pid_file, pid.to_string())?;
        self.watch(promise, pid, workdir, cancelled, Some(child));
        Ok(())
    }

    async fn life(&self, promise: &str) -> Value {
        match self.jobs.lock().unwrap().get(promise) {
            Some(j) => json!({"backend": "local", "pid": j.pid, "alive": alive(j.pid)}),
            None => json!({"backend": "local", "alive": false}),
        }
    }

    async fn kill(&self, promise: &str) {
        if let Some(j) = self.jobs.lock().unwrap().remove(promise) {
            j.cancelled.store(true, Ordering::SeqCst);
            unsafe { libc::kill(-j.pid, libc::SIGKILL) };
        }
    }
}
