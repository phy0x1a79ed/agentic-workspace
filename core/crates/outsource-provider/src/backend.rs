//! Where a provider's jobs run. The provider turns every compute step into a shell script; a
//! backend writes it to the work dir as `run.sh` and runs it. `Local` runs them detached on this
//! host; the Slurm backend runs them as job-array tasks on a cluster.

use async_trait::async_trait;
use outsource_proto::{PromiseId, Resources, RunStats};
use serde_json::Value;
use std::path::{Path, PathBuf};
use tokio::sync::mpsc;

#[derive(Debug, Clone)]
pub struct JobSpec {
    pub promise: PromiseId,
    /// Gets `run.sh`. The script writes its outputs here. The backend writes `exit` here when the
    /// script ends, holding its exit code, so a restarted provider can recover the result.
    pub workdir: PathBuf,
    /// The content of `run.sh`.
    pub script: String,
    pub res: Resources,
}

/// A job that ended: its promise and exit code. Each backend sends these on the channel it was
/// built with, once per launched job.
pub type Done = (PromiseId, i32);
pub type DoneTx = mpsc::UnboundedSender<Done>;

/// The shell line a backend runs for a job in its work dir. Where GNU `time` exists it leaves
/// `stats` (`maxrss_kb user_s sys_s wall_s`) beside `exit`; elsewhere the job runs unmeasured.
pub const RUN_LINE: &str =
    "if [ -x /usr/bin/time ]; then /usr/bin/time -f '%M %U %S %e' -o stats sh ./run.sh; else sh ./run.sh; fi";

/// The run stats in the text of a `stats` file; `None` when it holds no figures.
pub fn parse_stats(text: &str) -> Option<RunStats> {
    let f: Vec<&str> = text.lines().last()?.split_whitespace().collect();
    let [rss, user, sys, wall] = f[..] else { return None };
    Some(RunStats {
        max_rss_kb: rss.parse().ok(),
        cpu_s: user.parse::<f64>().ok().zip(sys.parse::<f64>().ok()).map(|(u, s)| u + s),
        wall_s: wall.parse().ok(),
    })
}

/// Writes `value` as JSON through a temp file and a rename, so a crash never leaves half a file.
pub fn write_json(path: &Path, value: &impl serde::Serialize) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, serde_json::to_vec(value)?)?;
    std::fs::rename(tmp, path)
}

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stats_text_parses_to_run_stats_and_absence_is_none() {
        assert_eq!(parse_stats(""), None);
        // GNU time puts a note on a failing command before the figures.
        assert_eq!(
            parse_stats("Command exited with non-zero status 1\n2048 1.25 0.25 3.5\n"),
            Some(RunStats { max_rss_kb: Some(2048), cpu_s: Some(1.5), wall_s: Some(3.5) })
        );
        assert_eq!(parse_stats("garbage"), None);
    }
}
