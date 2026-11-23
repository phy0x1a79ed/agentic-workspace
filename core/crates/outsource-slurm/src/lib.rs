//! Slurm backend run off-cluster: every cluster call is one shell script over the provider's
//! `Host` (a one-shot ssh) and passes one per-cluster gate. Queries are cached, launched jobs are
//! batched into job arrays, and completion is read from `exit` files in the gated tick. Nothing
//! outsource runs on the cluster but the job scripts.

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

/// Minimum spacing between calls to one cluster. Each call does everything that is due.
const GATE: Duration = Duration::from_secs(10);
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
/// Share of the cap a job's walltime may reach: twice its estimate.
const WALLTIME_FACTOR: f64 = 2.0;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SlurmConfig {
    pub account: String,
    #[serde(default)]
    pub partition: Option<String>,
    /// Longest `--time` an array gets, e.g. `02:00:00`; also the walltime of a job with no estimate.
    pub walltime: String,
    /// On the cluster: holds each batch's list file and task script, and every call runs from it.
    pub scratch: PathBuf,
    /// Directory of the Slurm binaries; the plain command names on `PATH` when `None`.
    #[serde(default)]
    pub bin_dir: Option<PathBuf>,
    /// The cluster schedules whole nodes and refuses `--mem` (trillium), so arrays name no CPU or memory.
    #[serde(default)]
    pub whole_node: bool,
    /// Cores of a whole node: what a job is allocated there, for the `waste` bid axis.
    #[serde(default)]
    pub node_cpus: Option<u32>,
}

/// What one array shares: cpus, memory and `--time`.
type Profile = (u32, u64, String);

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

fn due_at(fetched: Option<Instant>, now: Instant) -> Instant {
    fetched.map_or(now, |t| t + GATE)
}

fn stale(fetched: Option<Instant>, now: Instant) -> bool {
    due_at(fetched, now) <= now
}

/// Seconds in a Slurm time limit: `[d-]h:m:s`, `m:s` or minutes.
fn limit_secs(t: &str) -> Option<u64> {
    let (days, rest) = t.split_once('-').map_or((0, t), |(d, r)| (d.parse().unwrap_or(0), r));
    let f: Vec<u64> = rest.split(':').map(|x| x.parse().ok()).collect::<Option<_>>()?;
    let secs = match f[..] {
        [m] => m * 60,
        [m, s] => m * 60 + s,
        [h, m, s] => h * 3600 + m * 60 + s,
        _ => return None,
    };
    Some(days * 86400 + secs)
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

    /// The `squeue` listing is due: outstanding tasks keep it fresh, which is how a vanished task
    /// is noticed.
    fn want_squeue(&self) -> bool {
        self.wanted || self.has_sent()
    }

    /// When the next slot is worth taking, `None` if nothing is due ever.
    fn next_slot(&self, now: Instant) -> Option<Instant> {
        let kills = (!self.kills.is_empty()).then_some(now);
        let holds = self.queues.iter().map(|q| q.since + HOLD).min();
        let squeue = self.want_squeue().then(|| due_at(self.squeue.as_ref().map(|c| c.0), now));
        let share = self.wanted.then(|| due_at(self.share.as_ref().map(|c| c.0), now));
        let t = [kills, holds, squeue, share].into_iter().flatten().min()?;
        Some(t.max(self.next_ok.unwrap_or(now)))
    }
}

/// The only place cluster calls run: it owns the gate. One loop, one call per slot, so
/// nothing else can reach the cluster.
async fn schedule(inner: Arc<Inner>) {
    loop {
        let next = inner.st.lock().unwrap().next_slot(Instant::now());
        match next {
            Some(t) if t <= Instant::now() => inner.slot().await,
            Some(t) => tokio::select! { _ = sleep_until(t) => {}, _ = inner.wake.notified() => {} },
            None => inner.wake.notified().await,
        }
    }
}

/// What came back from one slot script, split by its marker lines.
#[derive(Default)]
struct Reply {
    /// `sbatch` answer per group key: the job id or Slurm's error text.
    sbatch: HashMap<String, String>,
    /// `<jobid>_<index> <state>`; `None` when `squeue` itself failed.
    queue: Option<HashMap<String, String>>,
    exits: HashMap<String, i32>,
    share: Option<f64>,
}

fn parse_reply(text: &str) -> Reply {
    let mut r = Reply::default();
    let mut section = "";
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("#sbatch ") {
            if let Some((key, ans)) = rest.split_once(' ') {
                r.sbatch.insert(key.into(), ans.trim().into());
            }
        } else if let Some(name) = line.strip_prefix('#') {
            section = match name {
                "queue" => {
                    r.queue = Some(HashMap::new());
                    "queue"
                }
                "exits" => "exits",
                "share" => "share",
                _ => "",
            };
        } else if let Some((k, v)) = line.split_once(' ').map(|(k, v)| (k.trim(), v.trim())) {
            match (section, r.queue.as_mut()) {
                ("queue", Some(m)) => {
                    m.insert(k.into(), v.into());
                }
                ("exits", _) => {
                    if let Ok(c) = v.parse() {
                        r.exits.insert(k.into(), c);
                    }
                }
                _ => {}
            }
        } else if section == "share" {
            r.share = line.trim().parse().ok().or(r.share);
        }
    }
    r
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

    /// Reloads persisted tasks; the next slot settles them.
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

    /// `--time` for a job: twice its estimate rounded up to whole hours, never past the cluster
    /// cap; the cap itself when there is no estimate.
    fn walltime(&self, est_s: Option<f64>) -> String {
        let cap = limit_secs(&self.cfg.walltime);
        match (est_s.filter(|e| *e > 0.0), cap) {
            (Some(e), Some(cap)) => {
                let hours = (WALLTIME_FACTOR * e / 3600.0).ceil().max(1.0) as u64;
                if hours * 3600 < cap { format!("{hours}:00:00") } else { self.cfg.walltime.clone() }
            }
            _ => self.cfg.walltime.clone(),
        }
    }

    fn profile(&self, job: &JobSpec) -> Profile {
        let (cpus, mem) = if self.cfg.whole_node { (0, 0) } else { (job.res.cpus, job.res.mem_mb) };
        (cpus, mem, self.walltime(job.est_s))
    }

    /// The script for one array: its files, then one `sbatch` answering `#sbatch <key> <answer>`.
    fn batch(&self, key: &str, queue: &Queue) -> String {
        let dir = self.cfg.scratch.join(format!("batch-{}", outsource_proto::new_id()));
        let list: String = queue.jobs.iter().map(|j| format!("{}\n", j.workdir.display())).collect();
        let list_path = dir.join("workdirs");
        // A task whose work dir already holds `exit` (resubmitted
        // after a restart) keeps it rather than running again.
        let task = format!(
            "#!/bin/sh\nd=$(sed -n \"$((SLURM_ARRAY_TASK_ID + 1))p\" {})\ncd \"$d\" || exit 1\n[ -f exit ] && exit 0\n{{ {}; }} >log 2>&1\necho $? > exit.tmp && mv exit.tmp exit\n",
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
        let (cpus, mem, time) = &queue.profile;
        let mut args = vec![format!("--array=0-{}", queue.jobs.len() - 1), "--nodes=1".into()];
        if !self.cfg.whole_node {
            args.extend([format!("--cpus-per-task={cpus}"), format!("--mem={mem}M")]);
        }
        args.extend([
            format!("--account={}", self.cfg.account),
            format!("--time={time}"),
            "--job-name=outsource".into(),
            format!("--output={}/slurm-%A_%a.out", dir.display()),
        ]);
        if let Some(p) = &self.cfg.partition {
            args.push(format!("--partition={p}"));
        }
        args.push(script_path.display().to_string());
        body + &format!("r=$({} 2>&1 | tr '\\n' ' ')\necho \"#sbatch {key} $r\"\n", self.cmd("sbatch", &args))
    }

    /// One slot: everything due, as one script in one ssh. Sections run in order and none stops
    /// the others: pending `scancel`, one `sbatch` per due group, the `squeue` and exit-file
    /// listing, `sshare` when stale. Slurm gives a login node no completion event and
    /// compute-node writes on a parallel filesystem raise no inotify event, so the listing is
    /// also the completion check.
    async fn slot(&self) {
        let now = Instant::now();
        let (kills, queues, sent, share) = {
            let mut st = self.st.lock().unwrap();
            let due: Vec<Queue> = {
                let (due, rest) = std::mem::take(&mut st.queues).into_iter().partition(|q| q.since + HOLD <= now);
                st.queues = rest;
                due
            };
            let sent: Vec<(String, PathBuf)> = st
                .jobs
                .values()
                .filter_map(|(w, s)| match s {
                    Slot::Sent(k) => Some((k.clone(), w.join("exit"))),
                    Slot::Queued => None,
                })
                .collect();
            let share = st.wanted && stale(st.share.as_ref().map(|c| c.0), now);
            (std::mem::take(&mut st.kills), due, sent, share)
        };
        let mut body = String::new();
        if !kills.is_empty() {
            body += &(self.cmd("scancel", &kills) + "\n");
        }
        let keyed: Vec<(String, Queue)> = queues.into_iter().enumerate().map(|(i, q)| (format!("g{i}"), q)).collect();
        for (key, queue) in &keyed {
            body += &self.batch(key, queue);
        }
        let squeue = self.cmd("squeue", &["-h", "-r", "--me", "-o", "%i %T"].map(String::from));
        body += &format!("l=$({squeue}) && {{ echo '#queue'; echo \"$l\"; }}\necho '#exits'\n");
        for (key, exit) in &sent {
            let e = q(&exit.to_string_lossy());
            body += &format!("[ -f {e} ] && echo \"{key} $(cat {e})\"\n");
        }
        if share {
            // `$USER` expands on the cluster, where the account name differs from this host's.
            let args = ["-h", "-P", "-A", &self.cfg.account, "-o", "FairShare"].map(String::from);
            body += &format!("s=$({} -u \"$USER\") && {{ echo '#share'; echo \"$s\"; }}\n", self.cmd("sshare", &args));
        }
        let label = format!("slot {} sbatch, {} scancel, {} tracked", keyed.len(), kills.len(), sent.len());
        let Some(text) = self.call(label, &body).await else {
            // Nothing came back (the header failed): requeue and retry next slot.
            let mut st = self.st.lock().unwrap();
            st.kills.extend(kills);
            st.queues.extend(keyed.into_iter().map(|(_, q)| q));
            return;
        };
        self.settle(parse_reply(&text), keyed, share);
    }

    /// One gated call: `body` runs on the cluster from scratch, as one ssh. `label` is what
    /// `calls` records. A script that dies midway still returns what it printed, so the groups
    /// it submitted are not lost; `None` means it printed nothing.
    async fn call(&self, label: String, body: &str) -> Option<String> {
        {
            let mut st = self.st.lock().unwrap();
            let now = Instant::now();
            st.next_ok = Some(now + GATE);
            st.calls.push((now, label.clone()));
        }
        // sockeye refuses an sbatch run from /arc/home, so every call runs from scratch. No `set -e`:
        // a failing section must not skip the rest.
        let scratch = q(&self.cfg.scratch.to_string_lossy());
        let ran = self.host.run(&format!("mkdir -p {scratch} && cd {scratch} || exit 1\n{body}exit 0\n")).await;
        let mut st = self.st.lock().unwrap();
        let (ok, text, err) = match ran {
            Ok(r) => (r.ok, r.stdout, r.stderr),
            Err(e) => (false, String::new(), e.to_string()),
        };
        st.error = (!ok).then(|| format!("{label}: {}", err.trim()));
        if let Some(e) = &st.error {
            tracing::warn!("{e}");
        }
        (ok || !text.is_empty()).then_some(text)
    }

    fn settle(&self, reply: Reply, queues: Vec<(String, Queue)>, share_asked: bool) {
        let now = Instant::now();
        let mut st = self.st.lock().unwrap();
        for (key, queue) in queues {
            let id = reply.sbatch.get(&key).and_then(|a| a.split("Submitted batch job ").nth(1)).and_then(|t| t.split_whitespace().next()?.parse::<u64>().ok());
            let Some(id) = id else {
                match reply.sbatch.get(&key) {
                    // Slurm answered with an error: the group's jobs fail with its text.
                    Some(err) => {
                        tracing::warn!("sbatch: {err}");
                        st.error = Some(format!("sbatch: {err}"));
                        for j in &queue.jobs {
                            if st.jobs.remove(&j.promise).is_some() {
                                let _ = self.done.send((j.promise.clone(), -1));
                            }
                        }
                    }
                    // No answer at all: the script ended before it; try the group again.
                    None => st.queues.push(queue),
                }
                continue;
            };
            for (i, j) in queue.jobs.iter().enumerate() {
                let task = format!("{id}_{i}");
                match st.jobs.get_mut(&j.promise) {
                    Some(e) => e.1 = Slot::Sent(task),
                    None => st.kills.push(task), // killed while sbatch was running
                }
            }
        }
        if share_asked {
            let f = reply.share.unwrap_or(DEFAULT_SHARE).clamp(0.0, 1.0);
            st.share = Some((now, f));
        }
        let mut ended: Vec<(PromiseId, i32)> = vec![];
        let mut missing = HashMap::new();
        for (promise, (_, slot)) in &st.jobs {
            let Slot::Sent(key) = slot else { continue };
            if let Some(&code) = reply.exits.get(key.as_str()) {
                ended.push((promise.clone(), code));
            } else if reply.queue.as_ref().is_some_and(|m| !m.contains_key(key)) {
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
        for (promise, _) in &ended {
            st.jobs.remove(promise);
        }
        for end in ended {
            let _ = self.done.send(end);
        }
        if let Some(map) = reply.queue {
            st.squeue = Some((now, map));
            st.missing = missing;
        }
        st.wanted = false;
        self.persist(&st);
    }
}

#[async_trait]
impl Backend for SlurmBackend {
    /// Queue wait plus batching wait, from the caches only. Queue wait is
    /// `MAX_QUEUE_WAIT * (1 - fair-share)^2` plus `PER_PENDING` per own pending task. Batching
    /// wait is the time to the next slot: the hold of the oldest queued launch, or the gate.
    async fn start_eta(&self, _res: &Resources) -> f64 {
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
            let ready = st.queues.iter().map(|q| q.since).min().unwrap_or(now) + HOLD;
            let slot = ready.max(st.next_ok.unwrap_or(now));
            (queue_wait, (slot - now).as_secs_f64(), want)
        };
        if want {
            self.inner.wake.notify_one();
        }
        queue_wait + batch_wait
    }

    /// Whole-node clusters allocate `node_cpus` whatever the job asks for.
    fn waste(&self, res: &Resources) -> f64 {
        match self.inner.cfg.node_cpus.filter(|_| self.inner.cfg.whole_node) {
            Some(node) => (1.0 - res.cpus as f64 / node as f64).clamp(0.0, 1.0),
            None => 0.0,
        }
    }

    /// A promise already submitted (recovered from `jobs.json`) is left to the next slot.
    async fn launch(&self, job: JobSpec) -> anyhow::Result<()> {
        {
            let mut st = self.inner.st.lock().unwrap();
            if matches!(st.jobs.get(&job.promise), Some((_, Slot::Sent(_)))) {
                return Ok(());
            }
            st.jobs.insert(job.promise.clone(), (job.workdir.clone(), Slot::Queued));
            let p = self.inner.profile(&job);
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
