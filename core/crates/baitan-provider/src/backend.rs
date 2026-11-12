//! Where a provider's jobs run. The provider turns every step into a shell script in a work dir;
//! a backend only runs scripts. `Local` runs them detached on this host; the Slurm front runs them
//! as job-array tasks.

use async_trait::async_trait;
use baitan_proto::{PromiseId, Resources};
use serde_json::Value;
use std::path::PathBuf;
use tokio::sync::mpsc;

#[derive(Debug, Clone)]
pub struct JobSpec {
    pub promise: PromiseId,
    /// Holds `run.sh`. The script writes its outputs here. The backend writes `exit` here when the
    /// script ends, holding its exit code, so a restarted provider can recover the result.
    pub workdir: PathBuf,
    pub res: Resources,
}

/// A job that ended: its promise and exit code. Each backend sends these on the channel it was
/// built with, once per launched job.
pub type Done = (PromiseId, i32);
pub type DoneTx = mpsc::UnboundedSender<Done>;

#[async_trait]
pub trait Backend: Send + Sync {
    /// Seconds until a job with these resources could start: queue wait plus any batching wait.
    async fn start_eta(&self, res: &Resources) -> f64;
    /// Hands the job over. Returns at once; completion arrives as a `Done`.
    async fn launch(&self, job: JobSpec) -> anyhow::Result<()>;
    /// Sign of life for a running job, from what the backend already knows (process alive,
    /// cached Slurm state). Never a trigger.
    async fn life(&self, promise: &str) -> Value;
    /// Stops the job. No `Done` follows.
    async fn kill(&self, promise: &str);
}
