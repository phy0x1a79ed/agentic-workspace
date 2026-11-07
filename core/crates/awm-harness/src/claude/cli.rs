//! Ending a background session through the `claude` CLI.
//!
//! Decides nothing about whether the caller may end it; that stays with the caller.

use std::future::Future;
use std::os::fd::OwnedFd;
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use awm_plugin_sdk::procfs;
use serde_json::Value;
use tokio::io::AsyncReadExt;
use tokio::time::{sleep, timeout, Instant};

use super::records::{as_pid, py_str};
use super::roster::{JobId, RosterFile};
use super::ClaudeHome;

/// How long one `claude stop` or `claude rm` may run.
pub const TIMEOUT: Duration = Duration::from_secs(20);
/// How long the removal waits for the stopped worker to exit.
pub const WORKER_EXIT: Duration = Duration::from_secs(10);
/// How long each `rm` waits for the job to be gone.
pub const SETTLE: Duration = Duration::from_secs(3);
/// How many times `rm` runs before the removal gives up.
pub const RM_ATTEMPTS: usize = 3;
/// How often the waits look again.
pub const POLL: Duration = Duration::from_millis(250);

const DETAIL_CHARS: usize = 300;

/// What ending a job did. `detail` is the CLI's output, or why the removal failed.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct StopOutcome {
    pub stopped: bool,
    pub removed: bool,
    pub kept: bool,
    pub detail: String,
}

/// One finished CLI run: whether it exited zero, and its stdout and stderr interleaved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunOutput {
    pub success: bool,
    pub output: Vec<u8>,
}

/// Why a CLI run produced no output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunError {
    TimedOut,
    CouldNotRun(String),
}

/// Runs one `claude` command. Injected so tests never start a real `claude`.
pub trait Runner {
    /// Run `argv` with no stdin, killing it after `limit`.
    fn run(&self, argv: &[String], limit: Duration) -> impl Future<Output = Result<RunOutput, RunError>> + Send;
}

/// The real runner, on `tokio::process`.
#[derive(Debug, Clone, Copy, Default)]
pub struct ProcessRunner;

impl Runner for ProcessRunner {
    async fn run(&self, argv: &[String], limit: Duration) -> Result<RunOutput, RunError> {
        let could_not = |e: std::io::Error| RunError::CouldNotRun(e.to_string());
        let Some((program, args)) = argv.split_first() else {
            return Err(RunError::CouldNotRun("empty argv".into()));
        };
        let (tx, mut rx) = tokio::net::unix::pipe::pipe().map_err(could_not)?;
        let out: OwnedFd = tx.into_blocking_fd().map_err(could_not)?;
        let err = out.try_clone().map_err(could_not)?;
        let mut cmd = tokio::process::Command::new(program);
        cmd.args(args).stdin(Stdio::null()).stdout(out).stderr(err).kill_on_drop(true);
        let mut child = cmd.spawn().map_err(could_not)?;
        // The command holds the pipe's write end until it is dropped, and the read waits for EOF.
        drop(cmd);
        let finish = async {
            let mut output = Vec::new();
            rx.read_to_end(&mut output).await?;
            let status = child.wait().await?;
            Ok::<_, std::io::Error>(RunOutput { success: status.success(), output })
        };
        match timeout(limit, finish).await {
            Ok(result) => result.map_err(could_not),
            Err(_) => Err(RunError::TimedOut),
        }
    }
}

/// Where a job still shows. Injected so tests can drive the waits.
pub trait Probe {
    /// Whether the job's worker process is still running.
    fn worker_alive(&self, job: &JobId) -> bool;
    /// Whether the job still has a record or a roster entry.
    fn present(&self, job: &JobId) -> bool;
}

/// The real probe: the job's worker in the roster, and its directory under `jobs/`.
#[derive(Debug, Clone)]
pub struct JobProbe {
    pub home: ClaudeHome,
}

impl JobProbe {
    fn worker(&self, job: &JobId) -> serde_json::Map<String, Value> {
        let workers = RosterFile::read(&self.home).workers;
        workers.get(job.as_str()).and_then(Value::as_object).cloned().unwrap_or_default()
    }
}

impl Probe for JobProbe {
    fn worker_alive(&self, job: &JobId) -> bool {
        let w = self.worker(job);
        let (Some(pid), Some(start)) = (as_pid(w.get("pid")), w.get("procStart").filter(|v| !v.is_null())) else {
            return false;
        };
        procfs::start_ticks(pid).is_some_and(|live| live == py_str(start))
    }

    fn present(&self, job: &JobId) -> bool {
        self.home.jobs().join(job.as_str()).exists() || !self.worker(job).is_empty()
    }
}

async fn run(runner: &impl Runner, claude: &Path, verb: &str, job: &JobId) -> (bool, String) {
    let argv = [claude.to_string_lossy().into_owned(), verb.into(), job.to_string()];
    match runner.run(&argv, TIMEOUT).await {
        Ok(out) => {
            let text: String = String::from_utf8_lossy(&out.output).trim().chars().take(DETAIL_CHARS).collect();
            (out.success, text)
        }
        Err(RunError::TimedOut) => (false, format!("claude {verb} timed out")),
        Err(RunError::CouldNotRun(why)) => (false, format!("claude {verb} could not run: {why}")),
    }
}

async fn wait(done: impl Fn() -> bool, within: Duration) -> bool {
    let deadline = Instant::now() + within;
    while !done() {
        if Instant::now() >= deadline {
            return false;
        }
        sleep(POLL).await;
    }
    true
}

/// `claude stop <job>`, then `claude rm <job>` until the job is gone.
///
/// `claude stop` returns before the worker has exited, and the daemon writes the job's record
/// once more when it settles the exit. An `rm` that runs in between deletes the record, which
/// then comes back, and the job sits in the fleet view as stopped. So the removal waits for the
/// worker, and success is the job being absent afterwards, not `rm`'s exit code. A job that is
/// already stopped makes the stop fail and the removal still proceed.
pub async fn stop_and_remove(job: &JobId, claude: &Path, runner: &impl Runner, probe: &impl Probe) -> StopOutcome {
    let (stopped, stop_detail) = run(runner, claude, "stop", job).await;
    wait(|| !probe.worker_alive(job), WORKER_EXIT).await;
    let mut rm_detail = String::new();
    let mut removed = false;
    for _ in 0..RM_ATTEMPTS {
        rm_detail = run(runner, claude, "rm", job).await.1;
        removed = wait(|| !probe.present(job), SETTLE).await;
        if removed {
            break;
        }
    }
    let detail = if removed {
        stop_detail
    } else {
        let why = if rm_detail.is_empty() { "the job is still there".to_string() } else { rm_detail };
        tracing::warn!("claude cli: removing {job} failed: {why}");
        why
    };
    StopOutcome { stopped, removed, kept: false, detail }
}

/// `claude stop <job>` alone: the job and its transcript stay, so it resumes by id.
pub async fn stop_keeping(job: &JobId, claude: &Path, runner: &impl Runner) -> StopOutcome {
    let (stopped, detail) = run(runner, claude, "stop", job).await;
    StopOutcome { stopped, removed: false, kept: true, detail }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claude::records::tests::home;
    use serde_json::json;
    use std::collections::HashMap;
    use std::sync::Mutex;

    // Answers each verb with a fixed result, recording every argv.
    #[derive(Default)]
    struct FakeRunner {
        results: HashMap<&'static str, Result<RunOutput, RunError>>,
        calls: Mutex<Vec<Vec<String>>>,
    }

    impl FakeRunner {
        fn with(results: impl IntoIterator<Item = (&'static str, Result<RunOutput, RunError>)>) -> Self {
            Self { results: results.into_iter().collect(), calls: Mutex::default() }
        }
        fn calls(&self) -> Vec<Vec<String>> {
            self.calls.lock().unwrap().clone()
        }
        fn verbs(&self) -> Vec<String> {
            self.calls().into_iter().map(|c| c[1].clone()).collect()
        }
        fn rms(&self) -> usize {
            self.calls.lock().unwrap().iter().filter(|c| c[1] == "rm").count()
        }
    }

    fn exit(code: i32, out: &str) -> Result<RunOutput, RunError> {
        Ok(RunOutput { success: code == 0, output: out.as_bytes().to_vec() })
    }

    impl Runner for FakeRunner {
        async fn run(&self, argv: &[String], _limit: Duration) -> Result<RunOutput, RunError> {
            self.calls.lock().unwrap().push(argv.to_vec());
            self.results.get(argv[1].as_str()).cloned().unwrap_or_else(|| exit(0, ""))
        }
    }

    // A job gone once `rm` has run `gone_after_rm` times (never with None), whose worker is alive
    // for the first `alive_for` polls; every poll's answer goes into `order`.
    struct FakeProbe<'a> {
        run: &'a FakeRunner,
        gone_after_rm: Option<usize>,
        alive_for: Mutex<i64>,
        order: Mutex<Vec<&'static str>>,
    }

    fn probe(run: &FakeRunner, gone_after_rm: Option<usize>, alive_for: i64) -> FakeProbe<'_> {
        FakeProbe { run, gone_after_rm, alive_for: Mutex::new(alive_for), order: Mutex::default() }
    }

    impl Probe for FakeProbe<'_> {
        fn worker_alive(&self, _: &JobId) -> bool {
            let mut left = self.alive_for.lock().unwrap();
            *left -= 1;
            let alive = *left >= 0;
            self.order.lock().unwrap().push(if alive { "alive" } else { "exited" });
            alive
        }
        fn present(&self, _: &JobId) -> bool {
            self.gone_after_rm.is_none_or(|n| self.run.rms() < n)
        }
    }

    fn job() -> JobId {
        JobId::parse("abcd1234").unwrap()
    }

    async fn stop(run: &FakeRunner, gone_after_rm: Option<usize>) -> StopOutcome {
        stop_and_remove(&job(), Path::new("c"), run, &probe(run, gone_after_rm, 0)).await
    }

    #[tokio::test(start_paused = true)]
    async fn stop_then_remove_in_that_order() {
        let run = FakeRunner::default();
        let out = stop_and_remove(&job(), Path::new("/bin/claude"), &run, &probe(&run, Some(1), 0)).await;
        assert_eq!(run.calls(), [["/bin/claude", "stop", "abcd1234"], ["/bin/claude", "rm", "abcd1234"]]);
        assert!(out.stopped && out.removed && !out.kept);
    }

    #[tokio::test(start_paused = true)]
    async fn an_already_stopped_job_is_still_removed() {
        let run = FakeRunner::with([("stop", exit(1, "not running"))]);
        let out = stop(&run, Some(1)).await;
        assert!(out.removed && !out.stopped);
    }

    #[tokio::test(start_paused = true)]
    async fn a_failed_removal_carries_the_cli_output() {
        let run = FakeRunner::with([("rm", exit(1, "no such job\n"))]);
        let out = stop(&run, None).await;
        assert!(!out.removed);
        assert_eq!(out.detail, "no such job");
        let mut want = vec!["stop"];
        want.extend(["rm"; RM_ATTEMPTS]);
        assert_eq!(run.verbs(), want);
    }

    #[tokio::test(start_paused = true)]
    async fn a_record_the_settling_daemon_writes_back_is_removed_again() {
        let run = FakeRunner::default();
        let out = stop(&run, Some(2)).await;
        assert!(out.removed);
        assert_eq!(run.verbs(), ["stop", "rm", "rm"]);
    }

    #[tokio::test(start_paused = true)]
    async fn rm_waits_for_the_worker_to_exit() {
        let run = FakeRunner::default();
        let p = probe(&run, Some(1), 3);
        stop_and_remove(&job(), Path::new("c"), &run, &p).await;
        assert_eq!(*p.order.lock().unwrap(), ["alive", "alive", "alive", "exited"]);
        assert_eq!(run.verbs(), ["stop", "rm"]);
    }

    #[tokio::test(start_paused = true)]
    async fn an_already_removed_job_counts_as_removed() {
        let run = FakeRunner::with([("stop", exit(1, "No job matching")), ("rm", exit(1, "No job matching 'abcd1234'"))]);
        assert!(stop(&run, Some(0)).await.removed);
    }

    #[test]
    fn the_probe_reads_the_jobs_dir_and_the_roster() {
        let (_d, h) = home();
        std::fs::remove_dir(h.jobs()).unwrap();
        std::fs::write(h.roster(), json!({"workers": {"abcd1234": {"pid": 1, "procStart": "x"}}}).to_string()).unwrap();
        let p = JobProbe { home: h.clone() };
        assert!(p.present(&job()) && !p.worker_alive(&job()));
        std::fs::write(h.roster(), json!({"workers": {}}).to_string()).unwrap();
        assert!(!p.present(&job()));
        std::fs::create_dir_all(h.jobs().join("abcd1234")).unwrap();
        assert!(p.present(&job()));
    }

    #[test]
    fn the_probe_sees_a_live_worker() {
        let (_d, h) = home();
        let start = crate::claude::records::tests::own_start();
        let worker = json!({"pid": std::process::id(), "procStart": start});
        std::fs::write(h.roster(), json!({"workers": {"abcd1234": worker}}).to_string()).unwrap();
        assert!(JobProbe { home: h }.worker_alive(&job()));
    }

    #[tokio::test(start_paused = true)]
    async fn a_timeout_is_a_failure_not_an_exception() {
        let run = FakeRunner::with([("stop", Err(RunError::TimedOut)), ("rm", Err(RunError::TimedOut))]);
        let out = stop(&run, None).await;
        assert!(!out.removed && out.detail.contains("timed out"), "{}", out.detail);
    }

    #[test]
    fn only_a_job_id_reaches_the_cli() {
        for bad in ["", "--help", "ABCD1234", "abc"] {
            assert!(JobId::parse(bad).is_err(), "{bad:?}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn stop_keeping_stops_only() {
        let run = FakeRunner::with([("stop", exit(0, " stopped \n"))]);
        let out = stop_keeping(&job(), Path::new("c"), &run).await;
        assert_eq!(out, StopOutcome { stopped: true, removed: false, kept: true, detail: "stopped".into() });
        assert_eq!(run.verbs(), ["stop"]);
    }

    #[tokio::test]
    async fn the_process_runner_merges_both_streams_and_times_out() {
        let sh = |script: &str| vec!["sh".to_string(), "-c".into(), script.into()];
        let out = ProcessRunner.run(&sh("echo out; echo err >&2; exit 3"), Duration::from_secs(5)).await.unwrap();
        assert!(!out.success);
        assert_eq!(String::from_utf8_lossy(&out.output), "out\nerr\n");
        let slow = ProcessRunner.run(&sh("sleep 5"), Duration::from_millis(100)).await;
        assert_eq!(slow, Err(RunError::TimedOut));
        let missing = ProcessRunner.run(&["/nonexistent/claude".to_string()], Duration::from_secs(1)).await;
        assert!(matches!(missing, Err(RunError::CouldNotRun(_))));
    }
}
