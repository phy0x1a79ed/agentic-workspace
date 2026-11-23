//! Slurm backend run off-cluster: every cluster call is one shell script over the provider's
//! `Host` (a one-shot ssh) and passes one per-cluster gate. Queries are cached, launched jobs are
//! batched into job arrays, and completion is read from `exit` files in the gated tick. Nothing
//! outsource runs on the cluster but the job scripts.

use anyhow::bail;
use async_trait::async_trait;
use outsource_proto::{PromiseId, Resources};
use outsource_provider::backend::{Backend, DoneTx, JobSpec, RUN_LINE};
use outsource_provider::{q, Host};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::Notify;
use tokio::task::JoinHandle;
use tokio::time::{sleep_until, Instant};

/// Minimum spacing between calls to one cluster, sbatch included.
const GATE: Duration = Duration::from_secs(60);
/// A queue waits this long after its first job so a burst of launches lands in one array.
const HOLD: Duration = Duration::from_secs(2);
/// How long a task may stay unlisted by `squeue` with no `exit` before it counts as failed.
const MISSING_GRACE: Duration = Duration::from_secs(150);
/// Queue wait at fair-share factor 0; the wait scales with `(1 - factor)^2`.
const MAX_QUEUE_WAIT: f64 = 3600.0;
/// Extra wait per own task already pending.
const PER_PENDING: f64 = 5.0;
/// Fair-share factor assumed before the first `sshare` answer.
const DEFAULT_SHARE: f64 = 0.5;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SlurmConfig {
    pub account: String,
    #[serde(default)]
    pub partition: Option<String>,
    /// `--time` for every array, e.g. `02:00:00`.
    pub walltime: String,
    /// On the cluster: holds each batch's list file and task script, and every call runs from it.
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
    /// Work dir on the cluster and slot.
    jobs: HashMap<PromiseId, (PathBuf, Slot)>,
    queues: Vec<Queue>,
    kills: Vec<String>,
    squeue: Option<(Instant, HashMap<String, String>)>,
    share: Option<(Instant, f64)>,
    /// A bid or sign-of-life asked about a stale cache; refresh at the next free slot.
    wanted: bool,
    next_ok: Option<Instant>,
    calls: Vec<(Instant, String)>,
    /// When `squeue` first stopped listing a task that has no `exit` yet.
    missing: HashMap<PromiseId, Instant>,
    /// Why the last call failed, until one succeeds; a dead ssh master shows here.
    error: Option<String>,
}

struct Inner {
    cfg: SlurmConfig,
    host: Host,
    /// `jobs.json`, on this host.
    saved: PathBuf,
    done: DoneTx,
    st: Mutex<State>,
    wake: Notify,
}

enum Plan {
    Submit(usize),
    Kill,
    Tick,
    Sshare,
    At(Instant),
    Idle,
}

pub struct SlurmBackend {
    inner: Arc<Inner>,
    tasks: Vec<JoinHandle<()>>,
}

impl SlurmBackend {
    /// Starts the scheduler; must run inside a tokio runtime. `saved` is where submitted tasks
    /// persist, so a restarted provider picks them up.
    pub fn new(cfg: SlurmConfig, host: Host, saved: PathBuf, done: DoneTx) -> Self {
        let inner = Arc::new(Inner { cfg, host, saved, done, st: Mutex::default(), wake: Notify::new() });
        inner.recover();
        let tasks = vec![tokio::spawn(schedule(inner.clone()))];
        SlurmBackend { inner, tasks }
    }

    /// Every cluster call made so far: start time and label.
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
            Plan::Tick
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

/// The only place cluster calls run: it owns the gate. One loop, one call per slot, so
/// nothing else can reach the cluster.
async fn schedule(inner: Arc<Inner>) {
    loop {
        let plan = inner.st.lock().unwrap().plan(Instant::now());
        match plan {
            Plan::Submit(i) => inner.submit(i).await,
            Plan::Kill => inner.cancel().await,
            Plan::Tick => inner.tick().await,
            Plan::Sshare => inner.refresh_share().await,
            Plan::At(t) => tokio::select! { _ = sleep_until(t) => {}, _ = inner.wake.notified() => {} },
            Plan::Idle => inner.wake.notified().await,
        }
    }
}

impl Inner {
    /// Rewrites `jobs.json` with the submitted tasks.
    fn persist(&self, st: &State) {
        let saved: Vec<Saved> = st
            .jobs
            .iter()
            .filter_map(|(p, (w, s))| match s {
                Slot::Sent(k) => Some(Saved { key: k.clone(), promise: p.clone(), workdir: w.clone() }),
                Slot::Queued => None,
            })
            .collect();
        if let Err(e) = outsource_provider::backend::write_json(&self.saved, &saved) {
            tracing::warn!("persist jobs: {e}");
        }
    }

    /// Reloads persisted tasks; the next tick settles them.
    fn recover(&self) {
        let Ok(bytes) = std::fs::read(&self.saved) else { return };
        let Ok(saved) = serde_json::from_slice::<Vec<Saved>>(&bytes) else { return };
        let mut st = self.st.lock().unwrap();
        for s in saved {
            st.jobs.insert(s.promise, (s.workdir, Slot::Sent(s.key)));
        }
    }

    /// A Slurm command line, its arguments quoted.
    fn cmd(&self, name: &str, args: &[String]) -> String {
        let prog = match &self.cfg.bin_dir {
            Some(d) => d.join(name).to_string_lossy().into_owned(),
            None => name.to_string(),
        };
        std::iter::once(q(&prog)).chain(args.iter().map(|a| q(a))).collect::<Vec<_>>().join(" ")
    }

    /// One gated call: `body` runs on the cluster from scratch, as one ssh. `label` is what
    /// `calls` records.
    async fn call(&self, label: String, body: &str) -> anyhow::Result<String> {
        {
            let mut st = self.st.lock().unwrap();
            let now = Instant::now();
            st.next_ok = Some(now + GATE);
            st.calls.push((now, label.clone()));
        }
        // sockeye refuses an sbatch run from /arc/home, so every call runs from scratch.
        let scratch = q(&self.cfg.scratch.to_string_lossy());
        let out = self.host.sh(&format!("set -e\nmkdir -p {scratch} && cd {scratch}\n{body}")).await;
        let mut st = self.st.lock().unwrap();
        match out {
            Ok(text) => {
                st.error = None;
                Ok(text)
            }
            Err(e) => {
                let e = format!("{label}: {e}");
                tracing::warn!("{e}");
                st.error = Some(e.clone());
                bail!(e)
            }
        }
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
        let queue = self.st.lock().unwrap().queues.remove(idx);
        let dir = self.cfg.scratch.join(format!("batch-{}", outsource_proto::new_id()));
        let list: String = queue.jobs.iter().map(|j| format!("{}\n", j.workdir.display())).collect();
        let list_path = dir.join("workdirs");
        // A task whose work dir already holds `exit` (resubmitted
        // after a restart) keeps it rather than running again.
        let task = format!(
            "#!/bin/sh\nd=$(sed -n \"$((SLURM_ARRAY_TASK_ID + 1))p\" {})\ncd \"$d\" || exit 1\n[ -f exit ] && exit 0\n{}\necho $? > exit.tmp && mv exit.tmp exit\n",
            q(&list_path.to_string_lossy()),
            RUN_LINE
        );
        let script_path = dir.join("task.sh");
        let write = |path: &Path, text: &str| format!("printf '%s' {} > {}\n", q(text), q(&path.to_string_lossy()));
        let mut body = format!("mkdir -p {}\n", q(&dir.to_string_lossy()));
        for j in &queue.jobs {
            body += &format!("mkdir -p {}\n", q(&j.workdir.to_string_lossy()));
            body += &write(&j.workdir.join("run.sh"), &j.script);
        }
        body += &write(&list_path, &list);
        body += &write(&script_path, &task);
        let mut args = vec![format!("--array=0-{}", queue.jobs.len() - 1), "--nodes=1".into()];
        if !self.cfg.whole_node {
            args.extend([format!("--cpus-per-task={}", queue.profile.0), format!("--mem={}M", queue.profile.1)]);
        }
        args.extend([
            format!("--account={}", self.cfg.account),
            format!("--time={}", self.cfg.walltime),
            "--job-name=outsource".into(),
            format!("--output={}/slurm-%A_%a.out", dir.display()),
        ]);
        if let Some(p) = &self.cfg.partition {
            args.push(format!("--partition={p}"));
        }
        args.push(script_path.display().to_string());
        body += &self.cmd("sbatch", &args);
        let out = self.call(format!("sbatch {}", args.join(" ")), &body).await;
        let id = out.ok().and_then(|o| o.split_whitespace().last().and_then(|t| t.parse::<u64>().ok()));
        let Some(id) = id else { return self.fail(queue.jobs) };
        {
            let mut st = self.st.lock().unwrap();
            for (i, j) in queue.jobs.iter().enumerate() {
                let key = format!("{id}_{i}");
                match st.jobs.get_mut(&j.promise) {
                    Some(e) => e.1 = Slot::Sent(key),
                    None => st.kills.push(key), // killed while sbatch was running
                }
            }
            self.persist(&st);
        }
    }

    async fn cancel(&self) {
        let keys = std::mem::take(&mut self.st.lock().unwrap().kills);
        let _ = self.call(format!("scancel {}", keys.join(" ")), &self.cmd("scancel", &keys)).await;
    }

    /// One call that runs `squeue`, then reads the `exit` file of every submitted task: a task
    /// with an `exit` is done. Slurm gives a login node no completion event and compute-node
    /// writes on a parallel filesystem raise no inotify event, so this is the completion check.
    async fn tick(&self) {
        let sent: Vec<(String, PathBuf)> = {
            let st = self.st.lock().unwrap();
            st.jobs
                .values()
                .filter_map(|(w, s)| match s {
                    Slot::Sent(k) => Some((k.clone(), w.join("exit"))),
                    Slot::Queued => None,
                })
                .collect()
        };
        let mut body = self.cmd("squeue", &["-h", "-r", "--me", "-o", "%i %T"].map(String::from)) + "\necho '#exits'\n";
        for (key, exit) in &sent {
            let e = q(&exit.to_string_lossy());
            body += &format!("[ -f {e} ] && echo \"{key} $(cat {e})\"\n");
        }
        body += "exit 0\n";
        let Ok(text) = self.call("squeue -h -r --me -o %i %T".into(), &body).await else { return };
        let (queue, exits) = text.split_once("#exits\n").unwrap_or((&text, ""));
        let map: HashMap<String, String> = queue
            .lines()
            .filter_map(|l| l.split_once(' '))
            .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
            .collect();
        let exits: HashMap<&str, i32> =
            exits.lines().filter_map(|l| l.split_once(' ')).filter_map(|(k, c)| Some((k, c.trim().parse().ok()?))).collect();
        let now = Instant::now();
        let mut st = self.st.lock().unwrap();
        let mut ended: Vec<(PromiseId, i32)> = vec![];
        let mut missing = HashMap::new();
        for (promise, (_, slot)) in &st.jobs {
            let Slot::Sent(key) = slot else { continue };
            if let Some(&code) = exits.get(key.as_str()) {
                ended.push((promise.clone(), code));
            } else if !map.contains_key(key) {
                // Unlisted with no `exit`: walltime, OOM or node loss. squeue can omit a task just
                // submitted, and a compute node's `exit` reaches the login node late on a shared
                // filesystem, so a task fails only once it stays missing past the grace.
                let since = st.missing.get(promise).copied().unwrap_or(now);
                if now < since + MISSING_GRACE {
                    missing.insert(promise.clone(), since);
                } else {
                    ended.push((promise.clone(), -1));
                }
            }
        }
        if !ended.is_empty() {
            for (promise, code) in ended {
                st.jobs.remove(&promise);
                let _ = self.done.send((promise, code));
            }
            self.persist(&st);
        }
        st.squeue = Some((now, map));
        st.missing = missing;
        st.wanted &= stale(st.share.as_ref().map(|c| c.0), now);
    }

    async fn refresh_share(&self) {
        // `$USER` expands on the cluster, where the account name differs from this host's.
        let args = ["-h", "-P", "-A", &self.cfg.account, "-o", "FairShare"].map(String::from);
        let body = format!("{} -u \"$USER\"", self.cmd("sshare", &args));
        let out = self.call(format!("sshare {}", args.join(" ")), &body).await;
        let mut st = self.st.lock().unwrap();
        if let Ok(text) = out {
            let f = text.lines().rev().find_map(|l| l.trim().parse::<f64>().ok());
            st.share = Some((Instant::now(), f.unwrap_or(DEFAULT_SHARE).clamp(0.0, 1.0)));
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

    /// A promise already submitted (recovered from `jobs.json`) is left to the tick.
    async fn launch(&self, job: JobSpec) -> anyhow::Result<()> {
        {
            let mut st = self.inner.st.lock().unwrap();
            if matches!(st.jobs.get(&job.promise), Some((_, Slot::Sent(_)))) {
                return Ok(());
            }
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
        let (state, task, error, want) = {
            let mut st = self.inner.st.lock().unwrap();
            let want = st.touch(now);
            let Some((_, slot)) = st.jobs.get(promise) else {
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
            (state, task, st.error.clone(), want)
        };
        if want {
            self.inner.wake.notify_one();
        }
        json!({"backend": "slurm", "state": state, "task": task, "error": error})
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
