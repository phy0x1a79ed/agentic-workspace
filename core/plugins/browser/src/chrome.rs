//! The shared Chrome's container (fleet.md R102 item 5): started on demand, stopped when idle.
//!
//! The browser singleton owns the `awm-chrome` container on its rootless Docker. Any verb that
//! needs the shared Chrome starts it. Once no seat sits on it and nothing has used it for the idle
//! period, the idle loop stops it.

use std::path::PathBuf;
use std::time::{Duration, Instant};

use awm_cdp::Docker;
use serde_json::Value;
use tokio::sync::Mutex;

pub const NAME: &str = "awm-chrome";

/// How to run the shared Chrome, from `browser.json`'s `chrome` table.
#[derive(Debug, Clone)]
pub struct Run {
    pub image: String,
    pub cdp_port: u16,
    pub viewer_port: u16,
    pub profile: PathBuf,
    pub idle: Duration,
}

impl Run {
    pub fn from_json(v: &Value) -> Option<Run> {
        let s = |k: &str| v.get(k).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string);
        let n = |k: &str, d: u64| v.get(k).and_then(Value::as_u64).unwrap_or(d);
        Some(Run {
            image: s("image")?,
            cdp_port: n("cdp_port", 9222) as u16,
            viewer_port: n("viewer_port", 6080) as u16,
            profile: s("profile")?.into(),
            idle: Duration::from_secs(60 * n("idle_minutes", 30)),
        })
    }
}

pub struct Chrome {
    /// None when something else runs Chrome, as in tests.
    run: Option<Run>,
    cdp: String,
    docker: Docker,
    last_use: std::sync::Mutex<Instant>,
    starting: Mutex<()>,
}

impl Chrome {
    pub fn new(run: Option<Run>, cdp: &str) -> Chrome {
        Chrome { run, cdp: cdp.to_string(), docker: Docker::default(), last_use: std::sync::Mutex::new(Instant::now()), starting: Mutex::new(()) }
    }

    /// Mark the shared Chrome in use, and start it when it is not running.
    pub async fn ensure(&self) -> anyhow::Result<()> {
        *self.last_use.lock().unwrap() = Instant::now();
        let Some(run) = &self.run else { return Ok(()) };
        let _one = self.starting.lock().await;
        if awm_cdp::wait_ready(&self.cdp, Duration::from_millis(500)).await.is_ok() {
            return Ok(());
        }
        tracing::info!("starting the shared Chrome");
        self.docker.remove(NAME).await;
        let cdp = format!("127.0.0.1:{}:9222", run.cdp_port);
        let viewer = format!("127.0.0.1:{0}:{0}", run.viewer_port);
        let viewer_env = format!("VIEWER_PORT={}", run.viewer_port);
        let profile = format!("{}:/profile", run.profile.display());
        #[rustfmt::skip]
        let args = ["run", "-d", "--rm", "--name", NAME, "--shm-size", "1g", "-p", &cdp, "-p", &viewer,
                    "-e", &viewer_env, "-e", "CDP_PORT=9222", "-v", &profile, &run.image];
        self.docker.run(&args).await?;
        awm_cdp::wait_ready(&self.cdp, Duration::from_secs(60)).await.map_err(|e| anyhow::anyhow!("the shared Chrome did not open its debug port: {e}"))?;
        Ok(())
    }

    /// Stop the shared Chrome when nothing has used it for the idle period. True when it stopped.
    pub async fn stop_if_idle(&self, seats_on_it: usize) -> bool {
        let Some(run) = &self.run else { return false };
        if seats_on_it > 0 || self.last_use.lock().unwrap().elapsed() < run.idle {
            return false;
        }
        let _one = self.starting.lock().await;
        if self.docker.names(NAME).await.map(|n| n.iter().all(|x| x != NAME)).unwrap_or(true) {
            return false;
        }
        tracing::info!("stopping the idle shared Chrome");
        if let Err(e) = self.docker.run(&["stop", "-t", "20", NAME]).await {
            tracing::warn!(error = %e, "could not stop the shared Chrome");
            return false;
        }
        true
    }
}
