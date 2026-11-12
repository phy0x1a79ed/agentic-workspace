//! Slurm backend for a baitan front: every Slurm call passes one per-cluster gate, queries are
//! cached, launched jobs are batched into job arrays, and completion is read from `exit` files.

use anyhow::bail;
use async_trait::async_trait;
use baitan_proto::{PromiseId, Resources};
use baitan_provider::backend::{Backend, DoneTx, JobSpec};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::Notify;
use tokio::task::JoinHandle;
use tokio::time::{sleep, sleep_until, Instant};

/// Minimum spacing between Slurm calls on one cluster, sbatch included.
const GATE: Duration = Duration::from_secs(60);
/// A queue waits this long after its first job so a burst of launches lands in one array.
const HOLD: Duration = Duration::from_secs(2);
/// Interval of the `exit` file check while jobs are outstanding.
const EXIT_POLL: Duration = Duration::from_secs(10);
/// Queue wait at fair-share factor 0; the wait scales with `(1 - factor)^2`.
const MAX_QUEUE_WAIT: f64 = 3600.0;
/// Extra wait per own task already pending.
const PER_PENDING: f64 = 5.0;
/// Fair-share factor assumed before the first `sshare` answer.
const DEFAULT_SHARE: f64 = 0.5;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SlurmConfig {
    pub cluster: String,
    pub account: String,
    #[serde(default)]
    pub partition: Option<String>,
    /// `--time` for every array, e.g. `02:00:00`.
    pub walltime: String,
    /// Holds each batch's list file and task script.
    pub scratch: PathBuf,
    /// Directory of the Slurm binaries; the plain command names on `PATH` when `None`.
    #[serde(default)]
    pub bin_dir: Option<PathBuf>,
    /// The cluster schedules whole nodes and refuses `--mem` (trillium), so arrays name no CPU or memory.
    #[serde(default)]
    pub whole_node: bool,
}

type Profile = (u32, u64);

fn profile(r: &Resources) -> Profile {
    (r.cpus, r.mem_mb)
}

enum Slot {
    Queued,
    /// The `<jobid>_<index>` name `squeue` and `scancel` use.
    Sent(String),
}

struct Queue {
    profile: Profile,
    since: Instant,
    jobs: Vec<JobSpec>,
}

/// A submitted array task as persisted in `jobs.json`; `key` is `<jobid>_<index>`.
#[derive(Serialize, Deserialize)]
struct Saved {
    key: String,
    promise: PromiseId,
    workdir: PathBuf,
}

#[derive(Default)]
struct State {
    jobs: HashMap<PromiseId, (PathBuf, Slot)>,
    queues: Vec<Queue>,
    kills: Vec<String>,
    squeue: Option<(Instant, HashMap<String, String>)>,
    share: Option<(Instant, f64)>,
    /// A bid or sign-of-life asked about a stale cache; refresh at the next free slot.
    wanted: bool,
    next_ok: Option<Instant>,
    calls: Vec<(Instant, String)>,
    batches: u64,
}

struct Inner {
    cfg: SlurmConfig,
    done: DoneTx,
    st: Mutex<State>,
    wake: Notify,
    outstanding: Notify,
}

enum Plan {
    Submit(usize),
    Kill,
    Squeue,
    Sshare,
    At(Instant),
    Idle,
}

pub struct SlurmBackend {
    inner: Arc<Inner>,
    tasks: Vec<JoinHandle<()>>,
}

impl SlurmBackend {
    /// Starts the scheduler and the exit watcher; must run inside a tokio runtime.
    pub fn new(cfg: SlurmConfig, done: DoneTx) -> Self {
        let inner = Arc::new(Inner {
            cfg,
            done,
            st: Mutex::default(),
            wake: Notify::new(),
            outstanding: Notify::new(),
        });
        inner.recover();
        let tasks = vec![tokio::spawn(schedule(inner.clone())), tokio::spawn(watch_exits(inner.clone()))];
        SlurmBackend { inner, tasks }
    }

    /// Every Slurm call made so far: start time and command line.
    pub fn calls(&self) -> Vec<(Instant, String)> {
        self.inner.st.lock().unwrap().calls.clone()
    }
}

impl Drop for SlurmBackend {
    fn drop(&mut self) {
        for t in &self.tasks {
            t.abort();
        }
    }
}

fn stale(fetched: Option<Instant>, now: Instant) -> bool {
    fetched.is_none_or(|t| now >= t + GATE)
}

impl State {
    /// Marks the caches for a lazy refresh when either is stale.
    fn touch(&mut self, now: Instant) -> bool {
        let want = stale(self.squeue.as_ref().map(|c| c.0), now) || stale(self.share.as_ref().map(|c| c.0), now);
        self.wanted |= want;
        want
    }

    fn has_sent(&self) -> bool {
        self.jobs.values().any(|(_, s)| matches!(s, Slot::Sent(_)))
    }

    fn plan(&self, now: Instant) -> Plan {
        let due = self.queues.iter().position(|q| q.since + HOLD <= now);
        // Outstanding tasks keep the squeue cache fresh: that is how a vanished task is noticed.
        let sq = (self.wanted || self.has_sent()) && stale(self.squeue.as_ref().map(|c| c.0), now);
        let sh = self.wanted && stale(self.share.as_ref().map(|c| c.0), now);
        let action = if let Some(i) = due {
            Plan::Submit(i)
        } else if !self.kills.is_empty() {
            Plan::Kill
        } else if sq {
            Plan::Squeue
        } else if sh {
            Plan::Sshare
        } else {
            let next_ok = self.next_ok.unwrap_or(now);
            let hold = self.queues.iter().map(|q| q.since + HOLD).min();
            let refresh = self.squeue.as_ref().filter(|_| self.has_sent()).map(|c| c.0 + GATE);
            return match hold.into_iter().chain(refresh).min() {
                Some(t) => Plan::At(t.max(next_ok)),
                None => Plan::Idle,
            };
        };
        match self.next_ok {
            Some(t) if t > now => Plan::At(t),
            _ => action,
        }
    }
}

/// The only place Slurm commands run: it owns the gate. One loop, one call per slot, so
/// nothing else can reach Slurm.
async fn schedule(inner: Arc<Inner>) {
    loop {
        let plan = inner.st.lock().unwrap().plan(Instant::now());
        match plan {
            Plan::Submit(i) => inner.submit(i).await,
            Plan::Kill => inner.cancel().await,
            Plan::Squeue => inner.refresh_squeue().await,
            Plan::Sshare => inner.refresh_share().await,
            Plan::At(t) => tokio::select! { _ = sleep_until(t) => {}, _ = inner.wake.notified() => {} },
            Plan::Idle => inner.wake.notified().await,
        }
    }
}

/// Slurm gives a login node no completion event and compute-node writes on a parallel
/// filesystem raise no inotify event, so stat the `exit` files. A plain stat is no Slurm call.
async fn watch_exits(inner: Arc<Inner>) {
    loop {
        let sent: Vec<(PromiseId, PathBuf)> = {
            let st = inner.st.lock().unwrap();
            st.jobs
                .iter()
                .filter(|(_, (_, s))| matches!(s, Slot::Sent(_)))
                .map(|(p, (w, _))| (p.clone(), w.join("exit")))
                .collect()
        };
        if sent.is_empty() {
            inner.outstanding.notified().await;
            continue;
        }
        for (promise, path) in sent {
            let Ok(text) = tokio::fs::read_to_string(&path).await else { continue };
            let Ok(code) = text.trim().parse::<i32>() else { continue };
            let mut st = inner.st.lock().unwrap();
            if st.jobs.remove(&promise).is_some() {
                inner.persist(&st);
                let _ = inner.done.send((promise, code));
            }
        }
        sleep(EXIT_POLL).await;
    }
}

impl Inner {
    fn saved_path(&self) -> PathBuf {
        self.cfg.scratch.join(&self.cfg.cluster).join("jobs.json")
    }

    /// Rewrites `jobs.json` with the submitted tasks, so a restarted front can pick them up.
    fn persist(&self, st: &State) {
        let saved: Vec<Saved> = st
            .jobs
            .iter()
            .filter_map(|(p, (w, s))| match s {
                Slot::Sent(k) => Some(Saved { key: k.clone(), promise: p.clone(), workdir: w.clone() }),
                Slot::Queued => None,
            })
            .collect();
        let path = self.saved_path();
        let write = || -> anyhow::Result<()> {
            std::fs::create_dir_all(path.parent().unwrap())?;
            let tmp = path.with_extension("tmp");
            std::fs::write(&tmp, serde_json::to_vec(&saved)?)?;
            Ok(std::fs::rename(tmp, &path)?)
        };
        if let Err(e) = write() {
            tracing::warn!("persist jobs: {e}");
        }
    }

    /// Reloads persisted tasks; the exit watcher and the next squeue refresh then settle them.
    fn recover(&self) {
        let Ok(bytes) = std::fs::read(self.saved_path()) else { return };
        let Ok(saved) = serde_json::from_slice::<Vec<Saved>>(&bytes) else { return };
        let mut st = self.st.lock().unwrap();
        for s in saved {
            st.jobs.insert(s.promise, (s.workdir, Slot::Sent(s.key)));
        }
        self.outstanding.notify_one();
    }

    async fn slurm(&self, cmd: &str, args: &[String]) -> anyhow::Result<String> {
        {
            let mut st = self.st.lock().unwrap();
            let now = Instant::now();
            st.next_ok = Some(now + GATE);
            st.calls.push((now, format!("{cmd} {}", args.join(" "))));
        }
        let prog = match &self.cfg.bin_dir {
            Some(d) => d.join(cmd),
            None => PathBuf::from(cmd),
        };
        // sockeye refuses an sbatch run from /arc/home, so every call runs from scratch.
        tokio::fs::create_dir_all(&self.cfg.scratch).await?;
        let out = tokio::process::Command::new(prog)
            .args(args)
            .current_dir(&self.cfg.scratch)
            .stdin(std::process::Stdio::null())
            .output()
            .await?;
        if !out.status.success() {
            bail!("{cmd} failed: {}", String::from_utf8_lossy(&out.stderr).trim());
        }
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    fn fail(&self, jobs: Vec<JobSpec>) {
        let mut st = self.st.lock().unwrap();
        for j in jobs {
            if st.jobs.remove(&j.promise).is_some() {
                let _ = self.done.send((j.promise, -1));
            }
        }
    }

    async fn submit(&self, idx: usize) {
        let (q, n) = {
            let mut st = self.st.lock().unwrap();
            st.batches += 1;
            (st.queues.remove(idx), st.batches)
        };
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_millis());
        let dir = self.cfg.scratch.join(&self.cfg.cluster).join(format!("batch-{stamp}-{n}"));
        let list: String = q.jobs.iter().map(|j| format!("{}\n", j.workdir.display())).collect();
        let list_path = dir.join("workdirs");
        let script = format!(
            "#!/bin/sh\nd=$(sed -n \"$((SLURM_ARRAY_TASK_ID + 1))p\" '{}')\ncd \"$d\" || exit 1\nsh run.sh\necho $? > exit.tmp && mv exit.tmp exit\n",
            list_path.display()
        );
        let script_path = dir.join("task.sh");
        let written = async {
            tokio::fs::create_dir_all(&dir).await?;
            tokio::fs::write(&list_path, list).await?;
            tokio::fs::write(&script_path, script).await
        }
        .await;
        if let Err(e) = written {
            tracing::warn!("batch files: {e}");
            return self.fail(q.jobs);
        }
        let mut args = vec![format!("--array=0-{}", q.jobs.len() - 1), "--nodes=1".into()];
        if !self.cfg.whole_node {
            args.extend([format!("--cpus-per-task={}", q.profile.0), format!("--mem={}M", q.profile.1)]);
        }
        args.extend([
            format!("--account={}", self.cfg.account),
            format!("--time={}", self.cfg.walltime),
            "--job-name=baitan".into(),
            format!("--output={}/slurm-%A_%a.out", dir.display()),
        ]);
        if let Some(p) = &self.cfg.partition {
            args.push(format!("--partition={p}"));
        }
        args.push(script_path.display().to_string());
        let id = match self.slurm("sbatch", &args).await {
            Ok(out) => out.split_whitespace().last().and_then(|t| t.parse::<u64>().ok()),
            Err(e) => {
                tracing::warn!("{e}");
                None
            }
        };
        let Some(id) = id else { return self.fail(q.jobs) };
        {
            let mut st = self.st.lock().unwrap();
            for (i, j) in q.jobs.iter().enumerate() {
                let key = format!("{id}_{i}");
                match st.jobs.get_mut(&j.promise) {
                    Some(e) => e.1 = Slot::Sent(key),
                    None => st.kills.push(key), // killed while sbatch was running
                }
            }
            self.persist(&st);
        }
        self.outstanding.notify_one();
    }

    async fn cancel(&self) {
        let keys = std::mem::take(&mut self.st.lock().unwrap().kills);
        if let Err(e) = self.slurm("scancel", &keys).await {
            tracing::warn!("{e}");
        }
    }

    async fn refresh_squeue(&self) {
        let out = self.slurm("squeue", &["-h".into(), "-r".into(), "--me".into(), "-o".into(), "%i %T".into()]).await;
        let gone: Vec<(PromiseId, PathBuf)> = {
            let mut st = self.st.lock().unwrap();
            match out {
                Ok(text) => {
                    let map: HashMap<String, String> = text
                        .lines()
                        .filter_map(|l| l.split_once(' '))
                        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
                        .collect();
                    let listed: HashSet<&String> = map.keys().collect();
                    let gone = st
                        .jobs
                        .iter()
                        .filter(|(_, (_, s))| matches!(s, Slot::Sent(k) if !listed.contains(k)))
                        .map(|(p, (w, _))| (p.clone(), w.clone()))
                        .collect();
                    st.squeue = Some((Instant::now(), map));
                    gone
                }
                Err(e) => {
                    tracing::warn!("{e}");
                    Vec::new()
                }
            }
        };
        // A task Slurm no longer lists that left no `exit` (walltime, OOM, node loss) is a
        // failed job; one with an `exit` is reported by the exit watcher.
        for (promise, workdir) in gone {
            if tokio::fs::try_exists(workdir.join("exit")).await.unwrap_or(false) {
                continue;
            }
            let mut st = self.st.lock().unwrap();
            if st.jobs.remove(&promise).is_some() {
                self.persist(&st);
                let _ = self.done.send((promise, -1));
            }
        }
        let mut st = self.st.lock().unwrap();
        let now = Instant::now();
        st.wanted &= stale(st.squeue.as_ref().map(|c| c.0), now) || stale(st.share.as_ref().map(|c| c.0), now);
    }

    async fn refresh_share(&self) {
        let user = std::env::var("USER").unwrap_or_default();
        let args = ["-h", "-P", "-u", &user, "-A", &self.cfg.account, "-o", "FairShare"].map(String::from);
        let out = self.slurm("sshare", &args).await;
        let mut st = self.st.lock().unwrap();
        match out {
            Ok(text) => {
                let f = text.lines().rev().find_map(|l| l.trim().parse::<f64>().ok());
                st.share = Some((Instant::now(), f.unwrap_or(DEFAULT_SHARE).clamp(0.0, 1.0)));
            }
            Err(e) => tracing::warn!("{e}"),
        }
        let now = Instant::now();
        st.wanted &= stale(st.squeue.as_ref().map(|c| c.0), now) || stale(st.share.as_ref().map(|c| c.0), now);
    }
}

#[async_trait]
impl Backend for SlurmBackend {
    /// Queue wait plus batching wait, from the caches only. Queue wait is
    /// `MAX_QUEUE_WAIT * (1 - fair-share)^2` plus `PER_PENDING` per own pending task. Batching
    /// wait is the time to this profile's sbatch slot: the hold, the gate, and one gate period
    /// for every other profile queued ahead.
    async fn start_eta(&self, res: &Resources) -> f64 {
        let now = Instant::now();
        let (queue_wait, batch_wait, want) = {
            let mut st = self.inner.st.lock().unwrap();
            let want = st.touch(now);
            let share = st.share.map_or(DEFAULT_SHARE, |c| c.1);
            let pending = st
                .squeue
                .as_ref()
                .map_or(0, |c| c.1.values().filter(|s| *s == "PENDING").count());
            let queue_wait = MAX_QUEUE_WAIT * (1.0 - share).powi(2) + PER_PENDING * pending as f64;
            let me = profile(res);
            let ready = st
                .queues
                .iter()
                .find(|q| q.profile == me)
                .map_or(now, |q| q.since)
                + HOLD;
            let slot = ready.max(st.next_ok.unwrap_or(now));
            let ahead = st.queues.iter().filter(|q| q.profile != me).count() as u32;
            let batch_wait = (slot - now) + GATE * ahead;
            (queue_wait, batch_wait.as_secs_f64(), want)
        };
        if want {
            self.inner.wake.notify_one();
        }
        queue_wait + batch_wait
    }

    async fn launch(&self, job: JobSpec) -> anyhow::Result<()> {
        {
            let mut st = self.inner.st.lock().unwrap();
            st.jobs.insert(job.promise.clone(), (job.workdir.clone(), Slot::Queued));
            let p = profile(&job.res);
            match st.queues.iter_mut().find(|q| q.profile == p) {
                Some(q) => q.jobs.push(job),
                None => st.queues.push(Queue { profile: p, since: Instant::now(), jobs: vec![job] }),
            }
        }
        self.inner.wake.notify_one();
        Ok(())
    }

    async fn life(&self, promise: &str) -> Value {
        let now = Instant::now();
        let (workdir, state, task, want) = {
            let mut st = self.inner.st.lock().unwrap();
            let want = st.touch(now);
            let Some((workdir, slot)) = st.jobs.get(promise) else {
                return json!({"backend": "slurm", "state": "unknown"});
            };
            let (state, task) = match slot {
                Slot::Queued => ("queued".to_string(), None),
                Slot::Sent(k) => {
                    let s = match &st.squeue {
                        None => "submitted".to_string(),
                        Some((_, m)) => m.get(k).cloned().unwrap_or_else(|| "not-in-queue".into()),
                    };
                    (s, Some(k.clone()))
                }
            };
            (workdir.clone(), state, task, want)
        };
        if want {
            self.inner.wake.notify_one();
        }
        let exit = tokio::fs::try_exists(workdir.join("exit")).await.unwrap_or(false);
        json!({"backend": "slurm", "state": state, "task": task, "exit": exit})
    }

    async fn kill(&self, promise: &str) {
        let mut st = self.inner.st.lock().unwrap();
        let Some((_, slot)) = st.jobs.remove(promise) else { return };
        match slot {
            Slot::Queued => {
                for q in &mut st.queues {
                    q.jobs.retain(|j| j.promise != promise);
                }
                st.queues.retain(|q| !q.jobs.is_empty());
            }
            Slot::Sent(key) => {
                st.kills.push(key);
                self.inner.persist(&st);
                drop(st);
                self.inner.wake.notify_one();
            }
        }
    }
}

#[cfg(test)]
mod tests;
