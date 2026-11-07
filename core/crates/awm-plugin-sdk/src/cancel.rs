//! Cancellation of one inbound call (`gatewayclient/cancel.py`).
//!
//! The gateway sends `cancel` when its caller goes away. The service trips the call's token
//! and aborts the handler's task. Aborting drops the handler's future, so a child process the
//! handler started with [`run`] dies with it: its whole process group gets SIGTERM, then
//! SIGKILL after a grace period.

use std::process::Output;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Notify;

use crate::service::VerbError;

const KILL_GRACE: Duration = Duration::from_secs(3);

/// Trips once when the caller cancels the call. Cloning shares the token.
#[derive(Clone, Default)]
pub struct CancelToken(Arc<Inner>);

#[derive(Default)]
struct Inner {
    flag: AtomicBool,
    notify: Notify,
}

impl CancelToken {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        self.0.flag.store(true, Ordering::SeqCst);
        self.0.notify.notify_waiters();
    }

    pub fn is_cancelled(&self) -> bool {
        self.0.flag.load(Ordering::SeqCst)
    }

    /// Resolve once the token trips.
    pub async fn cancelled(&self) {
        loop {
            let notified = self.0.notify.notified();
            if self.is_cancelled() {
                return;
            }
            notified.await;
        }
    }

    /// A `Cancelled` verb error once the token has tripped.
    pub fn check(&self) -> Result<(), VerbError> {
        if self.is_cancelled() {
            return Err(VerbError::new("Cancelled", "call cancelled by the caller"));
        }
        Ok(())
    }
}

/// Run `cmd` to completion with its output captured, in its own process group, which is
/// killed when the token trips or the calling future is dropped.
pub async fn run(token: &CancelToken, mut cmd: tokio::process::Command) -> Result<Output, VerbError> {
    token.check()?;
    cmd.process_group(0)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let child = cmd.spawn().map_err(|e| VerbError::runtime(format!("spawn: {e}")))?;
    let mut group = GroupGuard(child.id().map(|p| p as i32));
    let out = tokio::select! {
        out = child.wait_with_output() => out.map_err(|e| VerbError::runtime(e.to_string()))?,
        _ = token.cancelled() => return Err(VerbError::new("Cancelled", "call cancelled by the caller")),
    };
    group.0 = None;
    Ok(out)
}

/// Kills the process group on drop, unless the child has already exited.
struct GroupGuard(Option<i32>);

impl Drop for GroupGuard {
    fn drop(&mut self) {
        let Some(pgid) = self.0 else { return };
        // SAFETY: kill(2) on the process group this module started.
        if unsafe { libc::kill(-pgid, libc::SIGTERM) } != 0 {
            return;
        }
        std::thread::spawn(move || {
            std::thread::sleep(KILL_GRACE);
            // SAFETY: as above. A group that already exited makes this a no-op.
            unsafe {
                libc::kill(-pgid, libc::SIGKILL);
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn run_returns_output_and_dies_on_cancel() {
        let token = CancelToken::new();
        let mut cmd = tokio::process::Command::new("sh");
        cmd.args(["-c", "echo hi"]);
        assert_eq!(run(&token, cmd).await.unwrap().stdout, b"hi\n");

        let mut cmd = tokio::process::Command::new("sleep");
        cmd.arg("30");
        let t = token.clone();
        let started = std::time::Instant::now();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(100)).await;
            t.cancel();
        });
        let err = run(&token, cmd).await.unwrap_err();
        assert_eq!(err.error_class, "Cancelled");
        assert!(started.elapsed() < Duration::from_secs(5));
        assert!(token.check().is_err());
    }
}
