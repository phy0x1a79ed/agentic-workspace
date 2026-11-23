//! The provider: prices offer sets against its cache and queue, runs awarded sets through its
//! backend, and keeps promises, instances and reviews on disk so a restart recovers.

use crate::backend::{parse_stats, Backend, Done, JobSpec};
use crate::config::ProviderConfig;
use crate::host::Host;
use crate::index::{normalize, Entry, Flight, Found, Index, Pair};
use crate::jobs::{find_cargo, find_on_path, glob_match, is_local_route, q};
use crate::local::LocalBackend;
use outsource_proto::*;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use tokio::sync::{mpsc, oneshot, watch, OnceCell};

pub type DoneRx = mpsc::UnboundedReceiver<Done>;

const LOCAL_BANDWIDTH: f64 = 1e9;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct Instance {
    pub path: PathBuf,
    pub listing: Vec<String>,
    /// Bytes on disk, measured once when the instance is made.
    #[serde(default)]
    pub size: u64,
}

impl Instance {
    /// From `Host::files` of `path`: the bytes of every file, listing those `keep` accepts.
    pub fn of(path: PathBuf, files: &[(String, u64)], keep: impl Fn(&str) -> bool) -> Instance {
        let listing = files.iter().map(|f| &f.0).filter(|f| keep(f)).cloned().collect();
        Instance { path, listing, size: files.iter().map(|f| f.1).sum() }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PromiseRec {
    set: OfferSet,
    phase: Phase,
    /// The entry that awarded it, which every message about it goes back to.
    #[serde(default)]
    entry: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Review {
    promise: PromiseId,
    scores: BTreeMap<String, f64>,
}

#[derive(Default, Deserialize)]
struct Saved {
    promises: BTreeMap<PromiseId, PromiseRec>,
    entries: Vec<Entry>,
    instances: BTreeMap<Id, Instance>,
    reviews: BTreeMap<String, Vec<Review>>,
}

#[derive(Serialize)]
struct SavedView<'a> {
    promises: &'a BTreeMap<PromiseId, PromiseRec>,
    entries: Vec<&'a Entry>,
    instances: &'a BTreeMap<Id, Instance>,
    reviews: &'a BTreeMap<String, Vec<Review>>,
}

#[derive(Default)]
pub(crate) struct State {
    promises: BTreeMap<PromiseId, PromiseRec>,
    pub instances: BTreeMap<Id, Instance>,
    reviews: BTreeMap<String, Vec<Review>>,
    pub index: Index,
    /// Work each open bid would add if it won.
    open: HashMap<PromiseId, f64>,
    running: HashMap<PromiseId, f64>,
    tasks: HashMap<PromiseId, tokio::task::AbortHandle>,
    /// One per running entry; dropping the sender wakes everyone waiting on it.
    waiters: HashMap<u64, watch::Sender<()>>,
    /// What the run steps of each live promise measured, folded as they finish.
    run_stats: HashMap<PromiseId, RunStats>,
}

pub(crate) struct Inner {
    pub(crate) cfg: ProviderConfig,
    state_dir: PathBuf,
    /// Holds the work dirs and instances, and runs prep and placement steps: a move needs no queue slot.
    pub host: Host,
    work_dir: PathBuf,
    /// Runs compute steps.
    backend: Arc<dyn Backend>,
    pub state: Mutex<State>,
    /// Each attached entry's link, by entry name.
    out: Mutex<HashMap<String, mpsc::UnboundedSender<Envelope>>>,
    jobs: Arc<Mutex<HashMap<PromiseId, oneshot::Sender<i32>>>>,
    /// This provider's own asks to entries, by envelope id, with the entry asked, awaiting replies.
    asks: Mutex<HashMap<String, (String, oneshot::Sender<Msg>)>>,
    cargo: Option<PathBuf>,
    apptainer: Option<PathBuf>,
    /// The most a job may ask for; `None` is no limit.
    cpus: Option<u32>,
    mem_mb: Option<u64>,
    cargo_ver: OnceCell<String>,
}

#[derive(Clone)]
pub struct Provider {
    pub(crate) inner: Arc<Inner>,
}

struct Quote {
    /// Seconds to settle, before any backend start wait.
    time: f64,
    hit: Option<Hit>,
    /// Compute seconds the set adds; zero when nothing queues on the backend.
    work: f64,
}

type JobWaiters = Arc<Mutex<HashMap<PromiseId, oneshot::Sender<i32>>>>;

struct Env {
    id: Id,
    container: String,
}

type Resolved = HashMap<Id, Id>;

/// Whether a holder with no route here can push to this provider.
type Pushes<'a> = &'a dyn Fn(&str) -> bool;

/// Where a material comes from: `route` is `None` when the holder must push it.
struct Source<'a> {
    cost: f64,
    holder: &'a str,
    route: Option<&'a str>,
    path: Option<&'a str>,
}

fn resolve(resolved: &Resolved, id: &str) -> Id {
    resolved.get(id).cloned().unwrap_or_else(|| id.to_string())
}

fn request_pairs(step: &Step, inputs: &[Id]) -> Vec<Pair> {
    normalize(
        step.props
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .chain([("op".to_string(), step.op.clone())])
            .chain(inputs.iter().map(|i| ("input".to_string(), i.clone()))),
    )
}

const OUT: &str = "out";
const TRANSFER_FAILED: &str = "TransferFailed";

/// The shell line that copies `src` into the directory `dest`: both on this host over a local
/// route, else from this host to `route` (`push`) or from `route` to this host. With `list`, a
/// shell word naming a file of relative paths under `src`, only those paths are copied.
fn copy_line(route: &str, push: bool, src: &str, dest: &str, list: Option<&str>) -> String {
    let (d, src) = (q(dest), if list.is_some() { format!("{}/", src.trim_end_matches('/')) } else { src.to_string() });
    let only = list.map(|l| format!(" --files-from={l}")).unwrap_or_default();
    match (is_local_route(route), list) {
        (true, Some(l)) => format!("mkdir -p {d} && tar -C {} -cf - -T {l} | tar -C {d} -xf -", q(&src)),
        (true, None) => format!("mkdir -p {d} && cp -a {} {d}/", q(&src)),
        _ if push => format!("rsync -a{only} --rsync-path={} {} {}", q(&format!("mkdir -p {d} && rsync")), q(&src), q(&format!("{route}:{dest}/"))),
        _ => format!("mkdir -p {d} && rsync -a{only} {} {d}/", q(&format!("{route}:{src}"))),
    }
}

/// The index pairs a staged material answers to: its type's properties when it has one (a
/// held instance with more properties still hits), else its own id.
fn stage_pairs(item: &Value, id: &str) -> Vec<Pair> {
    match serde_json::from_value::<PropSet>(item["type"].clone()) {
        Ok(ty) => normalize(ty),
        Err(_) => vec![("id".into(), id.into())],
    }
}

/// The `(id, src, pairs)` of each material a step stages, with ids as `resolved` maps them.
fn stage_sources(step: &Step, resolved: &Resolved) -> Vec<(Id, Option<String>, Vec<Pair>)> {
    step.spec["stage"]
        .as_object()
        .into_iter()
        .flat_map(|m| m.values())
        .filter_map(|item| {
            let id = item["id"].as_str()?;
            Some((resolve(resolved, id), item["src"].as_str().map(String::from), stage_pairs(item, id)))
        })
        .collect()
}

/// The `(id, pairs)` of each material a step stages.
fn staged(step: &Step) -> Vec<(Id, Vec<Pair>)> {
    step.spec["stage"]
        .as_object()
        .into_iter()
        .flat_map(|m| m.values())
        .filter_map(|item| item["id"].as_str().map(|id| (id.to_string(), stage_pairs(item, id))))
        .collect()
}

#[derive(Deserialize)]
struct Typed {
    path: String,
    #[serde(rename = "type")]
    ty: PropSet,
}

/// The outputs a task keeps under a type: `(id, pairs, path)`.
fn typed_outputs(step: &Step) -> Vec<(Id, Vec<Pair>, String)> {
    let typed: Vec<Typed> = serde_json::from_value(step.spec["typed"].clone()).unwrap_or_default();
    typed.into_iter().map(|t| (instance_id(&t.ty), normalize(t.ty), t.path)).collect()
}

fn contract_of(set: &OfferSet) -> String {
    set.steps
        .iter()
        .find(|s| !s.is_placement())
        .map(|s| s.props.get("contract").cloned().unwrap_or_else(|| s.op.clone()))
        .unwrap_or_else(|| "place".into())
}

impl Provider {
    /// `done` receives the `Done`s of the channel `backend` was built with.
    pub fn new(cfg: ProviderConfig, backend: Arc<dyn Backend>, done: DoneRx) -> Provider {
        let state_dir = cfg.resolved_state_dir();
        let _ = std::fs::create_dir_all(&state_dir);
        let state_dir = state_dir.canonicalize().unwrap_or(state_dir);
        let saved: Saved = std::fs::read(state_dir.join("state.json"))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        let host = cfg.host();
        // A remote host's tools are taken on trust: a bid cannot afford the ssh to check them.
        let trusted = |p: &PathBuf| (!host.is_local() || p.exists()).then(|| p.clone());
        let cargo = match &cfg.cargo {
            Some(p) => trusted(p),
            None if host.is_local() => find_cargo(),
            None => None,
        };
        let apptainer = match &cfg.apptainer {
            Some(p) => trusted(p),
            None if host.is_local() => find_on_path("apptainer", &[]),
            None => None,
        };
        let cpus = cfg.cpus.or_else(|| host.is_local().then(local_cpus));
        let mem_mb = cfg.mem_mb.or_else(|| host.is_local().then(local_mem_mb).flatten());
        let work_dir = if cfg.work_dir.as_os_str().is_empty() { state_dir.join("work") } else { cfg.work_dir.clone() };
        let jobs: JobWaiters = Default::default();
        let state = State {
            promises: saved.promises,
            instances: saved.instances,
            reviews: saved.reviews,
            index: Index::from_entries(saved.entries),
            ..Default::default()
        };
        let inner = Arc::new(Inner {
            cfg,
            state_dir,
            host,
            work_dir,
            backend,
            state: Mutex::new(state),
            out: Mutex::default(),
            jobs: jobs.clone(),
            asks: Mutex::default(),
            cargo,
            apptainer,
            cpus,
            mem_mb,
            cargo_ver: OnceCell::new(),
        });
        tokio::spawn(route_done(done, jobs));
        let me = Provider { inner };
        me.resume();
        me
    }

    /// A provider whose jobs run on this host.
    pub fn local(cfg: ProviderConfig) -> Provider {
        let (tx, rx) = mpsc::unbounded_channel();
        Provider::new(cfg, Arc::new(LocalBackend::new(tx)), rx)
    }

    pub fn id(&self) -> &str {
        &self.inner.cfg.id
    }

    /// Stops this process's work on running promises. Jobs keep running; a new `Provider` on the
    /// same state dir picks them up.
    pub fn shutdown(&self) {
        let mut st = self.inner.state.lock().unwrap();
        for (_, h) in st.tasks.drain() {
            h.abort();
        }
    }

    pub(crate) fn attach(&self, entry: &str, tx: mpsc::UnboundedSender<Envelope>) {
        self.inner.out.lock().unwrap().insert(entry.into(), tx);
    }

    /// Forgets `entry` unless it has attached again on a newer link than `tx`.
    pub(crate) fn detach(&self, entry: &str, tx: &mpsc::UnboundedSender<Envelope>) {
        let mut out = self.inner.out.lock().unwrap();
        if out.get(entry).is_some_and(|t| t.same_channel(tx)) {
            out.remove(entry);
            self.inner.asks.lock().unwrap().retain(|_, (e, _)| e != entry);
        }
    }

    pub(crate) fn register(&self, entry: &str) {
        let c = &self.inner.cfg;
        self.inner.send(
            entry,
            Msg::Register(RegisterArgs { provider: c.id.clone(), versions: c.versions, props: c.props.clone(), routes: c.routes.keys().cloned().collect() }),
        );
    }

    fn resume(&self) {
        let open: Vec<PromiseId> = {
            let mut st = self.inner.state.lock().unwrap();
            let ids: Vec<_> =
                st.promises.iter().filter(|(_, r)| !r.phase.is_final()).map(|(p, _)| p.clone()).collect();
            for p in &ids {
                st.running.insert(p.clone(), 0.0);
            }
            ids
        };
        for p in open {
            self.inner.spawn_run(p);
        }
    }

    /// Handles one envelope addressed to this provider.
    pub(crate) async fn handle(&self, env: Envelope) {
        let me = &self.inner;
        let msg = match env.msg() {
            Ok(m) => m,
            Err(r) => return me.reply_env(env.refusal_reply(&r)),
        };
        if let Some((_, waiter)) = env.reply_to.as_ref().and_then(|id| me.asks.lock().unwrap().remove(id)) {
            let _ = waiter.send(msg);
            return;
        }
        match msg {
            Msg::Transfer(a) => {
                let p = self.clone();
                tokio::spawn(async move {
                    let reply = p.inner.on_transfer(a).await;
                    p.inner.reply_env(env.reply(&reply));
                });
            }
            Msg::Offer(a) => me.on_offer(&env, a).await,
            Msg::Award(a) => me.on_award(&env.from, a),
            Msg::Event(s) => me.on_event(s),
            Msg::Status(r) => me.on_status(&env, r).await,
            Msg::Cancel(r) => me.on_cancel(&env, r).await,
            Msg::Dispose(a) => me.on_dispose(&env, a).await,
            Msg::Import(a) => {
                let reply = match self.import_materials(&a.materials).await {
                    Ok(v) => Msg::Return(v),
                    Err(e) => raise_msg(raise::BAD_ARGS, e),
                };
                me.reply_env(env.reply(&reply));
            }
            Msg::Review(a) => me.on_review(&env, a),
            Msg::Return(_) => {}
            Msg::Raise(r) => tracing::warn!("{} raised {}: {}", env.from, r.name, r.message),
            Msg::DoesNotUnderstand(d) => tracing::warn!("{} does not understand {}", env.from, d.selector),
            _ => me.reply_env(env.reply(&raise_msg(raise::BAD_ARGS, format!("a provider does not take {}", env.selector)))),
        }
    }
}

fn local_cpus() -> u32 {
    std::thread::available_parallelism().map_or(1, |n| n.get() as u32)
}

fn local_mem_mb() -> Option<u64> {
    let info = std::fs::read_to_string("/proc/meminfo").ok()?;
    let kb = info.lines().find_map(|l| l.strip_prefix("MemTotal:"))?.trim().trim_end_matches("kB").trim().parse::<u64>().ok()?;
    Some(kb / 1024)
}

async fn route_done(mut rx: DoneRx, jobs: JobWaiters) {
    while let Some((promise, code)) = rx.recv().await {
        if let Some(tx) = jobs.lock().unwrap().remove(&promise) {
            let _ = tx.send(code);
        }
    }
}

impl Inner {
    fn send(&self, to: &str, msg: Msg) {
        self.reply_env(Envelope::new(&self.cfg.id, to, &msg));
    }

    fn reply_env(&self, env: Envelope) {
        if let Some(tx) = self.out.lock().unwrap().get(&env.to) {
            let _ = tx.send(env);
        }
    }

    fn entry_of(&self, promise: &str) -> String {
        self.state.lock().unwrap().promises.get(promise).map(|r| r.entry.clone()).unwrap_or_default()
    }

    pub(crate) fn persist(&self, st: &State) {
        let view = SavedView {
            promises: &st.promises,
            entries: st.index.finished(),
            instances: &st.instances,
            reviews: &st.reviews,
        };
        if let Err(e) = crate::backend::write_json(&self.state_dir.join("state.json"), &view) {
            tracing::error!("persisting state failed: {e}");
        }
    }

    // ---- pricing -------------------------------------------------------------------------

    /// A step's `est`, else `default_secs` scaled by Amdahl's law with a serial share of 1/cpus:
    /// the declared size takes `default_secs`, more provider cpus take less, one cpu is the same everywhere.
    fn est(&self, step: &Step, res: &Resources) -> f64 {
        if let Some(e) = step.spec.get("est").and_then(Value::as_f64) {
            return e;
        }
        let job = res.cpus.max(1) as f64;
        let here = self.cpus.map_or(job, |c| job.max(c as f64));
        self.cfg.default_secs * (1.0 + (job - 1.0) / here) / (1.0 + (job - 1.0) / job)
    }

    fn fits(&self, res: &Resources) -> Result<(), String> {
        if let Some(c) = self.cpus.filter(|c| res.cpus > *c) {
            return Err(format!("needs {} cpus, has {c}", res.cpus));
        }
        if let Some(m) = self.mem_mb.filter(|m| res.mem_mb > *m) {
            return Err(format!("needs {} MB, has {m} MB", res.mem_mb));
        }
        Ok(())
    }

    /// Seconds to move `bytes` over `route`; `None` is a holder pushing over its own route.
    fn transfer_secs(&self, route: Option<&str>, bytes: f64) -> f64 {
        match route {
            Some(r) if is_local_route(r) => bytes / LOCAL_BANDWIDTH,
            _ => bytes / self.cfg.bandwidth + self.cfg.route_overhead_s,
        }
    }

    /// The holder listed in `start` for `id` that costs least to move from, preferring one this
    /// provider pulls from over one that `pushes` says can push to it.
    fn nearest<'a>(&'a self, set: &'a OfferSet, id: &str, bytes: f64, pushes: Pushes) -> Option<Source<'a>> {
        set.start
            .iter()
            .filter_map(|f| f.as_at().map(|(i, holder)| (i, holder, f.path())))
            .filter(|(i, holder, _)| *i == id && *holder != self.cfg.id)
            .filter_map(|(_, holder, path)| {
                let route = self.cfg.routes.get(holder).map(String::as_str);
                (route.is_some() || pushes(holder)).then(|| Source { cost: self.transfer_secs(route, bytes), holder, route, path })
            })
            .min_by(|a, b| a.route.is_none().cmp(&b.route.is_none()).then(a.cost.total_cmp(&b.cost)))
    }

    /// The path a `start` fact gives for `id` on this provider's own filesystem.
    fn held_here<'a>(&self, set: &'a OfferSet, id: &str) -> Option<&'a str> {
        set.start.iter().find(|f| f.as_at() == Some((id, self.cfg.id.as_str()))).and_then(Fact::path)
    }

    /// Maps each material the step stages to the instance this provider holds under its type, and
    /// returns the longest time a transfer of one still has to run.
    fn resolve_stage(&self, st: &State, step: &Step, resolved: &mut Resolved, made: &mut HashSet<Id>) -> Option<f64> {
        let mut flying: Option<f64> = None;
        for (id, pairs) in staged(step) {
            let slots = match st.index.lookup(&pairs, &[OUT.to_string()]) {
                Some(Found::Done(slots)) => slots,
                Some(Found::Flying { remaining, slots, .. }) => {
                    flying = Some(flying.map_or(remaining, |f| f.max(remaining)));
                    slots
                }
                None => continue,
            };
            made.insert(slots[OUT].clone());
            resolved.insert(id, slots[OUT].clone());
        }
        flying
    }

    /// Like `resolve_stage` at run time: waits out each transfer in flight, then maps what is held.
    async fn resolve_staged(&self, step: &Step, resolved: &mut Resolved) {
        for (id, pairs) in staged(step) {
            loop {
                let found = self.state.lock().unwrap().index.lookup(&pairs, &[OUT.to_string()]);
                match found {
                    Some(Found::Done(slots)) => {
                        resolved.insert(id, slots[OUT].clone());
                        break;
                    }
                    Some(Found::Flying { eid, .. }) => {
                        let Some(mut rx) = self.state.lock().unwrap().waiters.get(&eid).map(|w| w.subscribe()) else { break };
                        while rx.changed().await.is_ok() {}
                    }
                    None => break,
                }
            }
        }
    }

    /// Seconds a bid adds for pulling the step's environment, zero when cached or absent.
    fn pull_secs(&self, step: &Step) -> Result<f64, String> {
        Ok(match self.env_of(step)? {
            Some(env) if !(self.host.is_local() && self.sif(&env.id).exists()) => self.cfg.env_pull_secs,
            _ => 0.0,
        })
    }

    fn fetch_cost(&self, st: &State, set: &OfferSet, inputs: &[Id], made: &HashSet<Id>, pushes: Pushes) -> Result<f64, String> {
        let each = set.size.unwrap_or(0) as f64 / inputs.len().max(1) as f64;
        let mut total = 0.0;
        for id in inputs {
            if made.contains(id) || st.instances.contains_key(id) || self.held_here(set, id).is_some() {
                continue;
            }
            total += self.nearest(set, id, each, pushes).ok_or_else(|| format!("no source for input {id}"))?.cost;
        }
        Ok(total)
    }

    fn place_cost(&self, st: &State, set: &OfferSet, step: &Step, inputs: &[Id], pushes: Pushes) -> Result<f64, String> {
        let to = step.to.as_deref().ok_or("place without a destination")?;
        if to == self.cfg.id {
            return Ok(0.0);
        }
        let route = self.cfg.routes.get(to).map(String::as_str);
        if route.is_none() && !pushes(to) {
            return Err(format!("no route to {to}"));
        }
        let each = set.size.unwrap_or(0) as f64 / inputs.len().max(1) as f64;
        let bytes: f64 = inputs
            .iter()
            .map(|i| st.instances.get(i).map(|n| n.size as f64).unwrap_or(each))
            .sum();
        Ok(self.transfer_secs(route, bytes))
    }

    /// `inbound` names the providers that can push to this one.
    fn quote(&self, promise: &str, set: &OfferSet, inbound: &[ProviderId]) -> Result<Quote, String> {
        let pushes: Pushes = &|p| inbound.iter().any(|i| i == p);
        if set.steps.is_empty() {
            return Err("empty set".into());
        }
        if set.steps.iter().any(|s| !s.is_move()) {
            if self.cfg.placement_only {
                return Err("placement only".into());
            }
            self.fits(&set.resources)?;
        }
        let mut st = self.state.lock().unwrap();
        let mut resolved = Resolved::new();
        let mut made: HashSet<Id> = HashSet::new();
        let (mut seq, mut flying, mut work, mut fetch) = (0.0f64, 0.0f64, 0.0f64, 0.0f64);
        let (mut all_hit, mut all_done, mut places) = (true, true, false);
        for step in &set.steps {
            if let Some(remaining) = self.resolve_stage(&st, step, &mut resolved, &mut made) {
                all_done = false;
                flying = flying.max(remaining);
            }
            let inputs: Vec<Id> = step.inputs.iter().map(|i| resolve(&resolved, i)).collect();
            match step.op.as_str() {
                "prep" => {
                    let lacks = inputs.iter().any(|i| !made.contains(i) && !st.instances.contains_key(i) && self.held_here(set, i).is_none());
                    let pull = self.pull_secs(step)?;
                    if lacks || pull > 0.0 {
                        all_hit = false;
                        all_done = false;
                        fetch += self.fetch_cost(&st, set, &inputs, &made, pushes)?;
                        seq += pull;
                    }
                }
                "place" => {
                    places = true;
                    fetch += self.fetch_cost(&st, set, &inputs, &made, pushes)?;
                    seq += self.place_cost(&st, set, step, &inputs, pushes)?;
                }
                "task" | "cargo" => {
                    if step.op == "cargo" && self.cargo.is_none() {
                        return Err("no cargo".into());
                    }
                    let found = st.index.lookup(&request_pairs(step, &inputs), &step.slots);
                    let stored = match found {
                        Some(Found::Done(s)) => Some(s),
                        Some(Found::Flying { remaining, slots, .. }) => {
                            all_done = false;
                            flying = flying.max(remaining);
                            Some(slots)
                        }
                        None => None,
                    };
                    for (slot, rid) in step.slot_ids() {
                        made.insert(rid.clone());
                        if let Some(sid) = stored.as_ref().and_then(|s| s.get(&slot)) {
                            made.insert(sid.clone());
                            resolved.insert(rid, sid.clone());
                        }
                    }
                    if stored.is_none() {
                        all_hit = false;
                        all_done = false;
                        let e = self.est(step, &set.resources) + self.pull_secs(step)?;
                        work += e;
                        seq += e;
                        fetch += self.fetch_cost(&st, set, &inputs, &made, pushes)?;
                    }
                }
                other => return Err(format!("unknown op {other}")),
            }
        }
        let committed: f64 = st.open.iter().filter(|(p, _)| *p != promise).map(|(_, w)| w).sum::<f64>()
            + st.running.values().sum::<f64>();
        let queue = if work > 0.0 { committed / self.cfg.slots.max(1) as f64 } else { 0.0 };
        let hit = (all_hit && !places).then_some(if all_done { Hit::Done } else { Hit::InFlight });
        let time = if hit.is_some() { flying } else { queue + fetch + seq + flying };
        st.open.insert(promise.to_string(), work);
        Ok(Quote { time, hit, work })
    }

    async fn on_offer(&self, env: &Envelope, a: OfferArgs) {
        let Some(promise) = a.promise else {
            return self.reply_env(env.reply(&raise_msg(raise::BAD_ARGS, "offer carries no promise")));
        };
        let bid = match self.quote(&promise, &a.set, &a.inbound) {
            Ok(Quote { time, hit, work }) => {
                let eta = if work > 0.0 { self.backend.start_eta(&a.set.resources).await } else { 0.0 };
                let mut values: BTreeMap<String, f64> = a.set.axes.iter().map(|x| (x.name.clone(), 0.0)).collect();
                values.insert(TIME_AXIS.into(), time + eta);
                values.insert(WASTE_AXIS.into(), self.backend.waste(&a.set.resources));
                BidArgs { promise, provider: self.cfg.id.clone(), values: Some(values), hit, reason: None }
            }
            Err(reason) => {
                BidArgs { promise, provider: self.cfg.id.clone(), values: None, hit: None, reason: Some(reason) }
            }
        };
        self.send(&env.from, Msg::Bid(bid));
    }

    fn on_event(&self, s: PromiseStatus) {
        let release = match &s.phase {
            Phase::Awarded { provider } => *provider != self.cfg.id,
            Phase::Failed { .. } => true,
            _ => false,
        };
        if release {
            self.state.lock().unwrap().open.remove(&s.promise);
        }
    }

    // ---- running -------------------------------------------------------------------------

    fn on_award(self: &Arc<Self>, entry: &str, a: AwardArgs) {
        {
            let mut st = self.state.lock().unwrap();
            if st.promises.get(&a.promise).is_some_and(|r| !r.phase.is_final()) {
                return;
            }
            let work = st.open.remove(&a.promise).unwrap_or(0.0);
            st.running.insert(a.promise.clone(), work);
            let phase = Phase::Started { provider: self.cfg.id.clone() };
            st.promises.insert(a.promise.clone(), PromiseRec { set: a.set, phase, entry: entry.into() });
            self.persist(&st);
        }
        self.send(entry, Msg::Started(StartedArgs { promise: a.promise.clone(), provider: self.cfg.id.clone() }));
        self.spawn_run(a.promise);
    }

    fn spawn_run(self: &Arc<Self>, promise: PromiseId) {
        let me = self.clone();
        let p = promise.clone();
        let h = tokio::spawn(async move { me.run_promise(p).await });
        self.state.lock().unwrap().tasks.insert(promise, h.abort_handle());
    }

    async fn run_promise(self: Arc<Self>, promise: PromiseId) {
        let Some(set) = self.state.lock().unwrap().promises.get(&promise).map(|r| r.set.clone()) else { return };
        let outcome = match self.execute(&promise, &set).await {
            Ok(o) => o,
            Err(message) => Outcome::Broken { message },
        };
        let phase = match &outcome {
            Outcome::Kept { facts, sizes, listing, stats } => Phase::Settled {
                provider: self.cfg.id.clone(),
                facts: facts.clone(),
                sizes: sizes.clone(),
                listing: listing.clone(),
                stats: stats.clone(),
            },
            Outcome::Broken { message } => {
                Phase::Failed { name: raise::BROKEN_PROMISE.into(), message: message.clone() }
            }
        };
        let entry = {
            let mut st = self.state.lock().unwrap();
            st.running.remove(&promise);
            st.tasks.remove(&promise);
            let entry = match st.promises.get_mut(&promise) {
                Some(r) if !r.phase.is_final() => {
                    r.phase = phase;
                    r.entry.clone()
                }
                _ => return,
            };
            self.persist(&st);
            entry
        };
        self.send(&entry, Msg::Settle(SettleArgs { promise, provider: self.cfg.id.clone(), outcome }));
    }

    async fn execute(self: &Arc<Self>, promise: &str, set: &OfferSet) -> Result<Outcome, String> {
        let mut resolved = Resolved::new();
        let mut facts = vec![];
        let mut here: Vec<Id> = vec![];
        let mut placed: Vec<Id> = vec![];
        for (idx, step) in set.steps.iter().enumerate() {
            match step.op.as_str() {
                "place" => {
                    let to = step.to.clone().ok_or("place without a destination")?;
                    for (id, path) in self.place(promise, idx, step, set, &resolved).await? {
                        facts.push(Fact::at_path(&id, &to, &path));
                        placed.push(id);
                    }
                }
                "task" | "cargo" => here.extend(self.compute(promise, idx, step, set, &mut resolved).await?),
                "prep" => here.extend(self.prep(promise, idx, step, set, &mut resolved).await?),
                other => return Err(format!("unknown op {other}")),
            }
        }
        let mut st = self.state.lock().unwrap();
        let stats = st.run_stats.remove(promise);
        let mut sizes = BTreeMap::new();
        let mut listing = BTreeMap::new();
        for id in &here {
            match st.instances.get(id) {
                Some(n) => facts.push(Fact::at_path(id, &self.cfg.id, &n.path.to_string_lossy())),
                None => facts.push(Fact::at(id, &self.cfg.id)),
            }
        }
        for id in here.iter().chain(&placed) {
            if let Some(n) = st.instances.get(id) {
                sizes.insert(id.clone(), n.size);
                if !n.listing.is_empty() {
                    listing.insert(id.clone(), n.listing.clone());
                }
            }
        }
        Ok(Outcome::Kept { facts, sizes, listing, stats })
    }

    async fn cargo_version(&self) -> Result<String, String> {
        let path = self.cargo.as_ref().ok_or("no cargo")?;
        self.cargo_ver
            .get_or_try_init(|| async {
                let out = self.host.sh(&format!("{} --version", q(&path.to_string_lossy()))).await;
                Ok(out.map_err(|e| format!("cargo --version: {e}"))?.trim().to_string())
            })
            .await
            .cloned()
    }

    /// Settles one compute step: a stored result, a wait on a running duplicate, or a run.
    /// Returns the instance ids of its slots.
    async fn compute(
        self: &Arc<Self>,
        promise: &str,
        idx: usize,
        step: &Step,
        set: &OfferSet,
        resolved: &mut Resolved,
    ) -> Result<Vec<Id>, String> {
        self.resolve_staged(step, resolved).await;
        let inputs: Vec<Id> = step.inputs.iter().map(|i| resolve(resolved, i)).collect();
        let pairs = request_pairs(step, &inputs);
        let extras: Vec<Pair> =
            if step.op == "cargo" { vec![("cargo".into(), self.cargo_version().await?)] } else { vec![] };
        let wanted = step.slot_ids();
        loop {
            enum Act {
                Hit(BTreeMap<String, Id>),
                Wait(watch::Receiver<()>),
                Run(u64, Vec<u64>),
            }
            let act = {
                let mut st = self.state.lock().unwrap();
                match st.index.lookup(&pairs, &step.slots) {
                    Some(Found::Done(s)) => Act::Hit(s),
                    Some(Found::Flying { eid, .. }) => Act::Wait(st.waiters[&eid].subscribe()),
                    None => {
                        let eid = st.index.insert(Entry {
                            pairs: normalize(pairs.iter().cloned().chain(extras.iter().cloned())),
                            slots: wanted.iter().cloned().collect(),
                            flight: Some(Flight { since: Instant::now(), est: self.est(step, &set.resources), owner: promise.into() }),
                        });
                        st.waiters.insert(eid, watch::channel(()).0);
                        let kept = typed_outputs(step)
                            .into_iter()
                            .map(|(id, pairs, _)| {
                                st.index.drop_ids(std::slice::from_ref(&id));
                                let flight = Some(Flight { since: Instant::now(), est: self.est(step, &set.resources), owner: promise.into() });
                                let teid = st.index.insert(Entry { pairs, slots: [(OUT.to_string(), id)].into(), flight });
                                st.waiters.insert(teid, watch::channel(()).0);
                                teid
                            })
                            .collect();
                        Act::Run(eid, kept)
                    }
                }
            };
            match act {
                Act::Hit(stored) => {
                    let mut ids = vec![];
                    for (slot, rid) in &wanted {
                        let sid = stored[slot].clone();
                        resolved.insert(rid.clone(), sid.clone());
                        ids.push(sid);
                    }
                    let st = self.state.lock().unwrap();
                    ids.extend(typed_outputs(step).into_iter().map(|t| t.0).filter(|id| st.instances.contains_key(id)));
                    return Ok(ids);
                }
                Act::Wait(mut rx) => while rx.changed().await.is_ok() {},
                Act::Run(eid, typed) => {
                    let made = match self.receive(promise, set, stage_sources(step, resolved)).await {
                        Ok(()) => self.run_job(promise, idx, step, set, resolved).await,
                        Err(e) => Err(e),
                    };
                    let mut st = self.state.lock().unwrap();
                    for e in std::iter::once(&eid).chain(&typed) {
                        match &made {
                            Ok(_) => st.index.finish(*e),
                            Err(_) => st.index.remove(*e),
                        }
                        st.waiters.remove(e);
                    }
                    if let Ok(instances) = &made {
                        st.instances.extend(instances.iter().cloned());
                    }
                    self.persist(&st);
                    return made.map(|m| m.into_iter().map(|(id, _)| id).collect());
                }
            }
        }
    }

    async fn run_job(
        &self,
        promise: &str,
        idx: usize,
        step: &Step,
        set: &OfferSet,
        resolved: &Resolved,
    ) -> Result<Vec<(Id, Instance)>, String> {
        let workdir = self.work_dir.join(format!("{promise}-{idx}"));
        let script = match step.op.as_str() {
            "cargo" => self.compile_cargo(step)?,
            _ => self.compile_task(set, step, resolved, &workdir)?,
        };
        let code = self.exec(promise, &workdir, script, &set.resources, step.spec.get("est").and_then(Value::as_f64)).await?;
        let stats = self.host.sh(&format!("cat {} 2>/dev/null || true", q(&workdir.join("stats").to_string_lossy()))).await;
        if let Some(run) = stats.ok().as_deref().and_then(parse_stats) {
            self.state.lock().unwrap().run_stats.entry(promise.to_string()).or_default().merge(&run);
        }
        if code != 0 {
            return Err(format!("{} exited {code}", step.op));
        }
        let slots = step.slot_ids();
        if step.op == "cargo" {
            let bin = workdir.join("bin");
            let files = self.host.files(&bin).await.map_err(|_| "build produced no binary".to_string())?;
            let made = Instance::of(bin, &files, |_| false);
            return Ok(slots.into_iter().map(|(_, id)| (id, made.clone())).collect());
        }
        let globs: Vec<&str> = step.spec["outputs"].as_array().into_iter().flatten().filter_map(Value::as_str).collect();
        const BOOKKEEPING: [&str; 6] = ["run.sh", "exit", "exit.tmp", "pid", "log", "stats"];
        let files = self.host.files(&workdir).await.map_err(|e| format!("listing outputs: {e}"))?;
        let made = Instance::of(workdir.clone(), &files, |f| !BOOKKEEPING.contains(&f) && globs.iter().any(|g| glob_match(g, f)));
        if !globs.is_empty() && made.listing.is_empty() {
            return Err("task made none of its outputs".into());
        }
        let mut kept = vec![];
        for (id, _, path) in typed_outputs(step) {
            let at = workdir.join(&path);
            let files = self.host.files(&at).await.map_err(|_| format!("task made no {path}, typed output"))?;
            kept.push((id, Instance::of(at, &files, |f| !f.is_empty())));
        }
        Ok(slots.into_iter().map(|(_, id)| (id, made.clone())).chain(kept).collect())
    }

    /// Hands `script` to the backend as `workdir/run.sh` and returns its exit code. Backends
    /// report a work dir that already holds `exit` (a job from before a restart) without rerunning it.
    async fn exec(&self, promise: &str, workdir: &Path, script: String, res: &Resources, est_s: Option<f64>) -> Result<i32, String> {
        let (tx, rx) = oneshot::channel();
        self.jobs.lock().unwrap().insert(promise.to_string(), tx);
        let spec = JobSpec { promise: promise.to_string(), workdir: workdir.to_path_buf(), script, res: res.clone(), est_s };
        if let Err(e) = self.backend.launch(spec).await {
            self.jobs.lock().unwrap().remove(promise);
            return Err(format!("launch failed: {e}"));
        }
        rx.await.map_err(|_| "job result lost".to_string())
    }

    // ---- job scripts ---------------------------------------------------------------------

    /// A local path for `id`, appending fetch lines to `lines` when it is held elsewhere.
    fn locate(
        &self,
        st: &State,
        set: &OfferSet,
        id: &str,
        src: Option<&str>,
        fetched: &Path,
        lines: &mut Vec<String>,
    ) -> Result<String, String> {
        if let Some(n) = st.instances.get(id) {
            return Ok(n.path.to_string_lossy().into_owned());
        }
        if let Some(path) = self.held_here(set, id) {
            return Ok(path.to_string());
        }
        let Some(Source { route: Some(route), path, .. }) = self.nearest(set, id, 0.0, &|_| false) else {
            return Err(format!("cannot stage {id}: no route to a holder"));
        };
        let src = src.or(path).ok_or_else(|| format!("cannot stage {id}: no path in its fact or spec"))?;
        if is_local_route(route) {
            return Ok(src.to_string());
        }
        let dest = fetched.to_string_lossy().into_owned();
        lines.push(format!("mkdir -p {}", q(&fetched.parent().unwrap_or(Path::new(".")).to_string_lossy())));
        lines.push(format!("rsync -a {} {}", q(&format!("{route}:{src}")), q(&dest)));
        Ok(dest)
    }

    /// The environment a task runs in, if its spec names one. Declines without apptainer.
    fn env_of(&self, step: &Step) -> Result<Option<Env>, String> {
        let Some(env) = step.spec.get("env") else { return Ok(None) };
        if self.apptainer.is_none() {
            return Err("no apptainer".into());
        }
        let stage = env["stage"].as_str().ok_or("env names no stage")?;
        let id = step.spec["stage"][stage]["id"].as_str().ok_or_else(|| format!("env stage {stage} has no id"))?;
        let container = env["container"].as_str().ok_or("env names no container")?;
        Ok(Some(Env { id: id.to_string(), container: container.to_string() }))
    }

    /// Where the pulled image for an environment id is cached.
    fn sif(&self, id: &str) -> PathBuf {
        self.work_dir.with_file_name("env").join(format!("{id}.sif"))
    }

    /// Shell lines that pull the environment's image into the cache unless it is there.
    fn pull_lines(&self, env: &Env, apptainer: &Path) -> Vec<String> {
        let (app, sif) = (q(&apptainer.to_string_lossy()), q(&self.sif(&env.id).to_string_lossy()));
        vec![
            format!("mkdir -p {}", q(&self.work_dir.with_file_name("env").to_string_lossy())),
            format!("[ -f {sif} ] || {{ {app} pull --force {sif}.$$ {} && mv {sif}.$$ {sif}; }}", q(&env.container)),
        ]
    }

    fn compile_task(&self, set: &OfferSet, step: &Step, resolved: &Resolved, workdir: &Path) -> Result<String, String> {
        let script = step.spec["script"].as_str().ok_or("task needs a script")?;
        let env = self.env_of(step)?;
        let mut lines = vec!["set -e".to_string(), "cd \"$(dirname \"$0\")\"".to_string()];
        let mut binds = vec![];
        let st = self.state.lock().unwrap();
        for (name, item) in step.spec["stage"].as_object().into_iter().flatten() {
            let id = resolve(resolved, item["id"].as_str().ok_or_else(|| format!("stage {name} has no id"))?);
            let base = self.locate(&st, set, &id, item["src"].as_str(), &workdir.join("in").join(name), &mut lines)?;
            let target = match item["path"].as_str() {
                Some(p) => format!("{base}/{p}"),
                None => base,
            };
            lines.push(format!("ln -s {} {}", q(&target), q(name)));
            binds.push(format!("--bind {}", q(&target)));
        }
        match (env, &self.apptainer) {
            (Some(env), Some(apptainer)) => {
                lines.extend(self.pull_lines(&env, apptainer));
                let (app, sif) = (q(&apptainer.to_string_lossy()), q(&self.sif(&env.id).to_string_lossy()));
                lines.push(format!("{app} exec {} {sif} bash -c {}", binds.join(" "), q(script)));
            }
            _ => lines.push(script.to_string()),
        }
        Ok(lines.join("\n") + "\n")
    }

    fn compile_cargo(&self, step: &Step) -> Result<String, String> {
        let cargo = self.cargo.as_ref().ok_or("no cargo")?;
        let s = &step.spec;
        let field = |k: &str| s[k].as_str().ok_or_else(|| format!("cargo spec needs {k}"));
        let (git, rev, bin) = (field("git")?, field("rev")?, field("bin")?);
        let target = s["target"].as_str();
        let mut build = format!("{} build --release", q(&cargo.to_string_lossy()));
        if let Some(t) = target {
            build += &format!(" --target {}", q(t));
        }
        if let Some(f) = s["features"].as_str() {
            build += &format!(" --features {}", q(f));
        }
        let built = match target {
            Some(t) => format!("target/{t}/release/{bin}"),
            None => format!("target/release/{bin}"),
        };
        Ok(format!(
            "set -e\ncd \"$(dirname \"$0\")\"\ngit clone -q {} src\ncd src\ngit checkout -q {}\n{build}\ncp {} ../bin\n",
            q(git),
            q(rev),
            q(&built)
        ))
    }

    fn held_dir(&self, id: &str) -> PathBuf {
        self.work_dir.with_file_name("held").join(id)
    }

    /// Has each holder this provider has no route to push its material into `held/<id>/` here,
    /// through the market, and holds it under `pairs`. Items are `(id, src, pairs)`.
    async fn receive(&self, promise: &str, set: &OfferSet, items: Vec<(Id, Option<String>, Vec<Pair>)>) -> Result<(), String> {
        for (id, src, pairs) in items {
            let (holder, src) = {
                let st = self.state.lock().unwrap();
                if st.instances.contains_key(&id) || self.held_here(set, &id).is_some() {
                    continue;
                }
                match self.nearest(set, &id, 0.0, &|_| true) {
                    Some(Source { route: None, holder, path, .. }) => {
                        let src = src.or(path.map(String::from)).ok_or_else(|| format!("cannot stage {id}: no path in its fact or spec"))?;
                        (holder.to_string(), src)
                    }
                    _ => continue,
                }
            };
            let dir = self.held_dir(&id);
            let ask = TransferArgs { from: holder, to: self.cfg.id.clone(), src: src.clone(), dest: dir.to_string_lossy().into(), files: vec![] };
            self.ask_market(promise, Msg::Transfer(ask)).await.map_err(|e| format!("cannot stage {id}: {e}"))?;
            let path = dir.join(Path::new(src.trim_end_matches('/')).file_name().ok_or_else(|| format!("{src} has no base name"))?);
            let files = self.host.files(&path).await.map_err(|e| format!("cannot stage {id}: {e}"))?;
            let mut st = self.state.lock().unwrap();
            st.index.insert(Entry { pairs, slots: [(OUT.to_string(), id.clone())].into(), flight: None });
            st.instances.insert(id, Instance::of(path, &files, |f| !f.is_empty()));
            self.persist(&st);
        }
        Ok(())
    }

    /// Settles a `prep` step: fetches each staged material not already held into `held/<id>/`,
    /// holds it, and pulls the environment image. Returns the ids now held here.
    async fn prep(self: &Arc<Self>, promise: &str, idx: usize, step: &Step, set: &OfferSet, resolved: &mut Resolved) -> Result<Vec<Id>, String> {
        self.resolve_staged(step, resolved).await;
        self.receive(promise, set, stage_sources(step, resolved)).await?;
        let mut lines = self.script_head(promise, idx);
        let (mut ids, mut fetched) = (vec![], vec![]);
        {
            let mut st = self.state.lock().unwrap();
            for item in step.spec["stage"].as_object().into_iter().flat_map(|m| m.values()) {
                let id = item["id"].as_str().ok_or("stage item has no id")?;
                let held = resolve(resolved, id);
                ids.push(held.clone());
                if st.instances.contains_key(&held) || self.held_here(set, id).is_some() {
                    continue;
                }
                let Some(Source { route: Some(route), path, .. }) = self.nearest(set, id, 0.0, &|_| false) else {
                    return Err(format!("cannot stage {id}: no route to a holder"));
                };
                let src = item["src"].as_str().or(path).ok_or_else(|| format!("cannot stage {id}: no path in its fact or spec"))?;
                let dir = self.held_dir(id);
                lines.push(format!("mkdir -p {}", q(&dir.to_string_lossy())));
                lines.push(if is_local_route(route) {
                    format!("cp -a {} {}/", q(src), q(&dir.to_string_lossy()))
                } else {
                    format!("rsync -a {} {}/", q(&format!("{route}:{src}")), q(&dir.to_string_lossy()))
                });
                let base = Path::new(src.trim_end_matches('/')).file_name().ok_or_else(|| format!("{src} has no base name"))?;
                let pairs = stage_pairs(item, id);
                let flight = Some(Flight { since: Instant::now(), est: self.est(step, &set.resources), owner: promise.into() });
                let eid = st.index.insert(Entry { pairs, slots: [(OUT.to_string(), id.to_string())].into(), flight });
                st.waiters.insert(eid, watch::channel(()).0);
                fetched.push((id.to_string(), dir.join(base), eid));
            }
        }
        if let (Some(env), Some(apptainer)) = (self.env_of(step)?, &self.apptainer) {
            lines.extend(self.pull_lines(&env, apptainer));
        }
        let mut ran = if lines.len() > 2 { self.host.sh(&(lines.join("\n") + "\n")).await.map(drop).map_err(|e| format!("prep failed: {e}")) } else { Ok(()) };
        let mut made = vec![];
        for (id, path, _) in &fetched {
            if ran.is_ok() {
                match self.host.files(path).await {
                    Ok(files) => made.push((id.clone(), Instance::of(path.clone(), &files, |f| !f.is_empty()))),
                    Err(e) => ran = Err(format!("prep: {e}")),
                }
            }
        }
        let mut st = self.state.lock().unwrap();
        if ran.is_ok() {
            st.instances.extend(made);
        }
        for (_, _, eid) in fetched {
            match &ran {
                Ok(()) => st.index.finish(eid),
                Err(_) => st.index.remove(eid),
            }
            st.waiters.remove(&eid);
        }
        self.persist(&st);
        ran.map(|()| ids)
    }

    /// The first lines of a script run on the host: stop at the first failure, in the step's work dir.
    fn script_head(&self, promise: &str, idx: usize) -> Vec<String> {
        let w = q(&self.work_dir.join(format!("{promise}-{idx}")).to_string_lossy());
        vec!["set -e".to_string(), format!("mkdir -p {w} && cd {w}")]
    }

    async fn place(
        &self,
        promise: &str,
        idx: usize,
        step: &Step,
        set: &OfferSet,
        resolved: &Resolved,
    ) -> Result<Vec<(Id, String)>, String> {
        let to = step.to.as_deref().ok_or("place without a destination")?;
        let dest = step.spec["path"].as_str().ok_or("place needs a path")?;
        let route = if to == self.cfg.id { Some("") } else { self.cfg.routes.get(to).map(String::as_str) };
        let ids: Vec<Id> = step.inputs.iter().map(|i| resolve(resolved, i)).collect();
        let files_only = step.spec["files"].as_bool().unwrap_or(false);
        if !files_only {
            let items = ids.iter().map(|id| (id.clone(), step.spec["src"][id].as_str().map(String::from), stage_pairs(&Value::Null, id))).collect();
            self.receive(promise, set, items).await?;
        }
        let workdir = self.work_dir.join(format!("{promise}-{idx}"));
        let mut lines = self.script_head(promise, idx);
        let (mut placed, mut pulls) = (vec![], vec![]);
        {
            let st = self.state.lock().unwrap();
            for (n, id) in ids.iter().enumerate() {
                let (from, files) = if files_only {
                    let inst = st.instances.get(id).ok_or_else(|| format!("{id} is not held here"))?;
                    placed.push((id.clone(), dest.to_string()));
                    if inst.listing.is_empty() {
                        continue;
                    }
                    (inst.path.to_string_lossy().into_owned(), inst.listing.clone())
                } else {
                    let from = self.locate(&st, set, id, step.spec["src"][id].as_str(), &workdir.join("in").join(id), &mut lines)?;
                    let name = Path::new(&from).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                    placed.push((id.clone(), format!("{dest}/{name}")));
                    (from, vec![])
                };
                let Some(route) = route else {
                    pulls.push(TransferArgs { from: self.cfg.id.clone(), to: to.into(), src: from, dest: dest.into(), files });
                    continue;
                };
                let list = (!files.is_empty()).then(|| q(&workdir.join(format!("files-{n}.txt")).to_string_lossy()));
                if let Some(list) = &list {
                    lines.push(format!("printf '%s\\n' {} > {list}", files.iter().map(|f| q(f)).collect::<Vec<_>>().join(" ")));
                }
                lines.push(copy_line(route, true, &from, dest, list.as_deref()));
            }
        }
        self.host.sh(&(lines.join("\n") + "\n")).await.map_err(|e| format!("place failed: {e}"))?;
        for pull in pulls {
            self.ask_market(promise, Msg::Transfer(pull)).await.map_err(|e| format!("place failed: {e}"))?;
        }
        Ok(placed)
    }

    /// Runs the copy a transfer asks of this provider, over its own route to the other end.
    async fn on_transfer(&self, a: TransferArgs) -> Msg {
        let me = self.cfg.id.as_str();
        let (other, push) = match me {
            m if m == a.from => (&a.to, true),
            m if m == a.to => (&a.from, false),
            _ => return raise_msg(raise::BAD_ARGS, format!("{me} is neither end of the transfer")),
        };
        let Some(route) = self.cfg.routes.get(other) else { return raise_msg(TRANSFER_FAILED, format!("{me} has no route to {other}")) };
        let mut lines = vec!["set -e".to_string()];
        if !a.files.is_empty() {
            lines.push("l=$(mktemp) && trap 'rm -f \"$l\"' EXIT".into());
            lines.push(format!("printf '%s\\n' {} > \"$l\"", a.files.iter().map(|f| q(f)).collect::<Vec<_>>().join(" ")));
        }
        lines.push(copy_line(route, push, &a.src, &a.dest, (!a.files.is_empty()).then_some("\"$l\"")));
        match self.host.sh(&(lines.join("\n") + "\n")).await {
            Ok(_) => Msg::Return(Value::Null),
            Err(e) => raise_msg(TRANSFER_FAILED, e.to_string()),
        }
    }

    /// Asks the entry that owns `promise` and waits for its reply; `Ok` holds a `return`'s value.
    async fn ask_market(&self, promise: &str, msg: Msg) -> Result<Value, String> {
        let entry = self.entry_of(promise);
        let env = Envelope::new(&self.cfg.id, &entry, &msg);
        let (tx, rx) = oneshot::channel();
        self.asks.lock().unwrap().insert(env.id.clone(), (entry, tx));
        self.reply_env(env);
        match rx.await {
            Ok(Msg::Return(v)) => Ok(v),
            Ok(Msg::Raise(r)) => Err(format!("{}: {}", r.name, r.message)),
            Ok(other) => Err(format!("market answered {other:?}")),
            Err(_) => Err("market link dropped".into()),
        }
    }

    // ---- caller verbs --------------------------------------------------------------------

    async fn on_status(&self, env: &Envelope, r: PromiseRef) {
        let rec = self.state.lock().unwrap().promises.get(&r.promise).map(|p| (p.phase.clone(), p.set.steps.iter().all(Step::is_move)));
        let Some((phase, moves_only)) = rec else {
            return self.reply_env(env.reply(&raise_msg(raise::UNKNOWN_PROMISE, r.promise)));
        };
        let life = match (phase.is_final(), moves_only) {
            (true, _) => None,
            (false, true) => Some(json!({"backend": "place"})),
            (false, false) => Some(self.backend.life(&r.promise).await),
        };
        let status = PromiseStatus { promise: r.promise, phase, life };
        self.reply_env(env.reply(&Msg::Return(serde_json::to_value(status).expect("status serializes"))));
    }

    async fn on_cancel(&self, env: &Envelope, r: PromiseRef) {
        {
            let mut st = self.state.lock().unwrap();
            let Some(rec) = st.promises.get_mut(&r.promise) else {
                drop(st);
                return self.reply_env(env.reply(&raise_msg(raise::UNKNOWN_PROMISE, r.promise)));
            };
            if !rec.phase.is_final() {
                rec.phase = Phase::Failed { name: raise::CANCELLED.into(), message: "cancelled".into() };
            }
            if let Some(h) = st.tasks.remove(&r.promise) {
                h.abort();
            }
            for eid in st.index.remove_owned(&r.promise) {
                st.waiters.remove(&eid);
            }
            st.open.remove(&r.promise);
            st.running.remove(&r.promise);
            self.persist(&st);
        }
        self.jobs.lock().unwrap().remove(&r.promise);
        self.backend.kill(&r.promise).await;
        self.reply_env(env.reply(&Msg::Return(Value::Null)));
    }

    async fn on_dispose(&self, env: &Envelope, a: DisposeArgs) {
        let paths: Vec<PathBuf> = {
            let mut st = self.state.lock().unwrap();
            let held: Vec<Id> = a.ids.iter().filter(|i| st.instances.contains_key(*i)).cloned().collect();
            st.index.drop_ids(&held);
            let paths = held.iter().filter_map(|i| st.instances.remove(i)).map(|n| n.path).collect();
            self.persist(&st);
            paths
        };
        if !paths.is_empty() {
            let rm: Vec<String> = paths.iter().map(|p| q(&p.to_string_lossy())).collect();
            if let Err(e) = self.host.sh(&format!("rm -rf {}", rm.join(" "))).await {
                tracing::warn!("dispose: {e}");
            }
        }
        self.reply_env(env.reply(&Msg::Return(Value::Null)));
    }

    fn on_review(&self, env: &Envelope, a: ReviewArgs) {
        let mut st = self.state.lock().unwrap();
        let Some(contract) = st.promises.get(&a.promise).map(|r| contract_of(&r.set)) else {
            drop(st);
            return self.reply_env(env.reply(&raise_msg(raise::UNKNOWN_PROMISE, a.promise)));
        };
        st.reviews.entry(contract).or_default().push(Review { promise: a.promise, scores: a.scores });
        self.persist(&st);
        drop(st);
        self.reply_env(env.reply(&Msg::Return(json!(null))));
    }
}
