//! The provider: prices offer sets against its cache and queue, runs awarded sets through its
//! backend, and keeps promises, instances and reviews on disk so a restart recovers.

use crate::backend::{Backend, Done, JobSpec};
use crate::config::ProviderConfig;
use crate::index::{normalize, Entry, Flight, Found, Index, Pair};
use crate::jobs::{disk_size, find_cargo, find_on_path, glob_match, is_local_route, q, remove_path, walk_files};
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
    pub fn new(path: PathBuf, listing: Vec<String>) -> Instance {
        let size = disk_size(&path);
        Instance { path, listing, size }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PromiseRec {
    set: OfferSet,
    phase: Phase,
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
}

pub(crate) struct Inner {
    pub(crate) cfg: ProviderConfig,
    state_dir: PathBuf,
    /// Runs compute steps.
    backend: Arc<dyn Backend>,
    /// Runs placement steps on this host: a move needs no queue slot.
    local: LocalBackend,
    pub state: Mutex<State>,
    out: Mutex<Option<mpsc::UnboundedSender<Envelope>>>,
    peer: Mutex<String>,
    jobs: Arc<Mutex<HashMap<PromiseId, oneshot::Sender<i32>>>>,
    cargo: Option<PathBuf>,
    apptainer: Option<PathBuf>,
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

/// The index pairs a staged material answers to: its type's properties when it has one (a
/// held instance with more properties still hits), else its own id.
fn stage_pairs(item: &Value, id: &str) -> Vec<Pair> {
    match serde_json::from_value::<PropSet>(item["type"].clone()) {
        Ok(ty) => normalize(ty),
        Err(_) => vec![("id".into(), id.into())],
    }
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
        let cargo = match &cfg.cargo {
            Some(p) => p.exists().then(|| p.clone()),
            None => find_cargo(),
        };
        let apptainer = match &cfg.apptainer {
            Some(p) => p.exists().then(|| p.clone()),
            None => find_on_path("apptainer", &[]),
        };
        let jobs: JobWaiters = Default::default();
        let (local_tx, local_rx) = mpsc::unbounded_channel();
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
            backend,
            local: LocalBackend::new(local_tx),
            state: Mutex::new(state),
            out: Mutex::new(None),
            peer: Mutex::new("market".into()),
            jobs: jobs.clone(),
            cargo,
            apptainer,
            cargo_ver: OnceCell::new(),
        });
        tokio::spawn(route_done(done, jobs.clone()));
        tokio::spawn(route_done(local_rx, jobs));
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

    pub(crate) fn attach(&self, tx: mpsc::UnboundedSender<Envelope>) {
        *self.inner.out.lock().unwrap() = Some(tx);
    }

    pub(crate) fn detach(&self) {
        *self.inner.out.lock().unwrap() = None;
    }

    pub(crate) fn register(&self) {
        let c = &self.inner.cfg;
        self.inner.send(
            "market",
            Msg::Register(RegisterArgs { provider: c.id.clone(), versions: c.versions, props: c.props.clone() }),
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
        if !matches!(env.selector.as_str(), "return" | "raise" | "doesNotUnderstand") {
            *me.peer.lock().unwrap() = env.from.clone();
        }
        let msg = match env.msg() {
            Ok(m) => m,
            Err(r) => return me.reply_env(env.refusal_reply(&r)),
        };
        match msg {
            Msg::Offer(a) => me.on_offer(&env, a).await,
            Msg::Award(a) => me.on_award(a),
            Msg::Event(s) => me.on_event(s),
            Msg::Status(r) => me.on_status(&env, r).await,
            Msg::Cancel(r) => me.on_cancel(&env, r).await,
            Msg::Dispose(a) => me.on_dispose(&env, a),
            Msg::Import(a) => {
                let reply = match self.import_materials(&a.materials) {
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
        if let Some(tx) = self.out.lock().unwrap().as_ref() {
            let _ = tx.send(env);
        }
    }

    fn peer(&self) -> String {
        self.peer.lock().unwrap().clone()
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

    fn est(&self, step: &Step) -> f64 {
        step.spec.get("est").and_then(Value::as_f64).unwrap_or(self.cfg.default_secs)
    }

    fn transfer_secs(&self, route: &str, bytes: f64) -> f64 {
        if is_local_route(route) {
            bytes / LOCAL_BANDWIDTH
        } else {
            bytes / self.cfg.bandwidth + self.cfg.route_overhead_s
        }
    }

    /// The reachable holder listed in `start` for `id` that costs least to fetch from: its cost,
    /// route and the path its fact names.
    fn nearest<'a>(&'a self, set: &'a OfferSet, id: &str, bytes: f64) -> Option<(f64, &'a str, Option<&'a str>)> {
        set.start
            .iter()
            .filter_map(|f| f.as_at().map(|(i, p)| (i, p, f.path())))
            .filter(|(i, p, _)| *i == id && *p != self.cfg.id)
            .filter_map(|(_, p, path)| self.cfg.routes.get(p).map(|r| (self.transfer_secs(r, bytes), r.as_str(), path)))
            .min_by(|a, b| a.0.total_cmp(&b.0))
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
            Some(env) if !self.sif(&env.id).exists() => self.cfg.env_pull_secs,
            _ => 0.0,
        })
    }

    fn fetch_cost(&self, st: &State, set: &OfferSet, inputs: &[Id], made: &HashSet<Id>) -> Result<f64, String> {
        let each = set.size.unwrap_or(0) as f64 / inputs.len().max(1) as f64;
        let mut total = 0.0;
        for id in inputs {
            if made.contains(id) || st.instances.contains_key(id) || self.held_here(set, id).is_some() {
                continue;
            }
            total += self.nearest(set, id, each).ok_or_else(|| format!("no source for input {id}"))?.0;
        }
        Ok(total)
    }

    fn place_cost(&self, st: &State, set: &OfferSet, step: &Step, inputs: &[Id]) -> Result<f64, String> {
        let to = step.to.as_deref().ok_or("place without a destination")?;
        if to == self.cfg.id {
            return Ok(0.0);
        }
        let route = self.cfg.routes.get(to).ok_or_else(|| format!("no route to {to}"))?;
        let each = set.size.unwrap_or(0) as f64 / inputs.len().max(1) as f64;
        let bytes: f64 = inputs
            .iter()
            .map(|i| st.instances.get(i).map(|n| n.size as f64).unwrap_or(each))
            .sum();
        Ok(self.transfer_secs(route, bytes))
    }

    fn quote(&self, promise: &str, set: &OfferSet) -> Result<Quote, String> {
        if set.steps.is_empty() {
            return Err("empty set".into());
        }
        if self.cfg.placement_only && set.steps.iter().any(|s| !s.is_move()) {
            return Err("placement only".into());
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
                        fetch += self.fetch_cost(&st, set, &inputs, &made)?;
                        seq += pull;
                    }
                }
                "place" => {
                    places = true;
                    fetch += self.fetch_cost(&st, set, &inputs, &made)?;
                    seq += self.place_cost(&st, set, step, &inputs)?;
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
                        let e = self.est(step) + self.pull_secs(step)?;
                        work += e;
                        seq += e;
                        fetch += self.fetch_cost(&st, set, &inputs, &made)?;
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
        let bid = match self.quote(&promise, &a.set) {
            Ok(Quote { time, hit, work }) => {
                let eta = if work > 0.0 { self.backend.start_eta(&a.set.resources).await } else { 0.0 };
                let mut values: BTreeMap<String, f64> = a.set.axes.iter().map(|x| (x.name.clone(), 0.0)).collect();
                values.insert(TIME_AXIS.into(), time + eta);
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

    fn on_award(self: &Arc<Self>, a: AwardArgs) {
        {
            let mut st = self.state.lock().unwrap();
            if st.promises.get(&a.promise).is_some_and(|r| !r.phase.is_final()) {
                return;
            }
            let work = st.open.remove(&a.promise).unwrap_or(0.0);
            st.running.insert(a.promise.clone(), work);
            let phase = Phase::Started { provider: self.cfg.id.clone() };
            st.promises.insert(a.promise.clone(), PromiseRec { set: a.set, phase });
            self.persist(&st);
        }
        self.send(&self.peer(), Msg::Started(StartedArgs { promise: a.promise.clone(), provider: self.cfg.id.clone() }));
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
            Outcome::Kept { facts, sizes, listing } => Phase::Settled {
                provider: self.cfg.id.clone(),
                facts: facts.clone(),
                sizes: sizes.clone(),
                listing: listing.clone(),
            },
            Outcome::Broken { message } => {
                Phase::Failed { name: raise::BROKEN_PROMISE.into(), message: message.clone() }
            }
        };
        {
            let mut st = self.state.lock().unwrap();
            st.running.remove(&promise);
            st.tasks.remove(&promise);
            match st.promises.get_mut(&promise) {
                Some(r) if !r.phase.is_final() => r.phase = phase,
                _ => return,
            }
            self.persist(&st);
        }
        self.send(
            &self.peer(),
            Msg::Settle(SettleArgs { promise, provider: self.cfg.id.clone(), outcome }),
        );
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
        let st = self.state.lock().unwrap();
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
        Ok(Outcome::Kept { facts, sizes, listing })
    }

    async fn cargo_version(&self) -> Result<String, String> {
        let path = self.cargo.as_ref().ok_or("no cargo")?;
        self.cargo_ver
            .get_or_try_init(|| async {
                let out = tokio::process::Command::new(path)
                    .arg("--version")
                    .output()
                    .await
                    .map_err(|e| format!("cargo --version: {e}"))?;
                Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
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
                            flight: Some(Flight { since: Instant::now(), est: self.est(step), owner: promise.into() }),
                        });
                        st.waiters.insert(eid, watch::channel(()).0);
                        let kept = typed_outputs(step)
                            .into_iter()
                            .map(|(id, pairs, _)| {
                                st.index.drop_ids(std::slice::from_ref(&id));
                                let flight = Some(Flight { since: Instant::now(), est: self.est(step), owner: promise.into() });
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
                    let made = self.run_job(promise, idx, step, set, resolved).await;
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
        let workdir = self.state_dir.join("work").join(format!("{promise}-{idx}"));
        if !workdir.join("run.sh").exists() {
            std::fs::create_dir_all(&workdir).map_err(|e| e.to_string())?;
            let script = match step.op.as_str() {
                "cargo" => self.compile_cargo(step)?,
                _ => self.compile_task(set, step, resolved, &workdir)?,
            };
            std::fs::write(workdir.join("run.sh"), script).map_err(|e| e.to_string())?;
        }
        let code = self.exec(&*self.backend, promise, &workdir, &set.resources).await?;
        if code != 0 {
            return Err(format!("{} exited {code}", step.op));
        }
        let slots = step.slot_ids();
        match step.op.as_str() {
            "cargo" => {
                let bin = workdir.join("bin");
                if !bin.is_file() {
                    return Err("build produced no binary".into());
                }
                let made = Instance::new(bin, vec![]);
                Ok(slots.into_iter().map(|(_, id)| (id, made.clone())).collect())
            }
            _ => {
                let globs: Vec<&str> = step.spec["outputs"].as_array().into_iter().flatten().filter_map(Value::as_str).collect();
                const BOOKKEEPING: [&str; 5] = ["run.sh", "exit", "exit.tmp", "pid", "log"];
                let listing: Vec<String> = walk_files(&workdir)
                    .into_iter()
                    .filter(|f| !BOOKKEEPING.contains(&f.as_str()) && globs.iter().any(|g| glob_match(g, f)))
                    .collect();
                if !globs.is_empty() && listing.is_empty() {
                    return Err("task made none of its outputs".into());
                }
                let mut kept = vec![];
                for (id, _, path) in typed_outputs(step) {
                    let at = workdir.join(&path);
                    if !at.exists() {
                        return Err(format!("task made no {path}, typed output"));
                    }
                    let files = if at.is_dir() { walk_files(&at) } else { vec![] };
                    kept.push((id, Instance::new(at, files)));
                }
                let made = Instance::new(workdir, listing);
                Ok(slots.into_iter().map(|(_, id)| (id, made.clone())).chain(kept).collect())
            }
        }
    }

    /// Runs `run.sh` in `workdir` through the backend and returns its exit code. A work dir that
    /// already holds `exit` (a job from before a restart) is not launched again.
    async fn exec(&self, backend: &dyn Backend, promise: &str, workdir: &Path, res: &Resources) -> Result<i32, String> {
        if let Some(code) = std::fs::read_to_string(workdir.join("exit")).ok().and_then(|s| s.trim().parse().ok()) {
            return Ok(code);
        }
        let (tx, rx) = oneshot::channel();
        self.jobs.lock().unwrap().insert(promise.to_string(), tx);
        let spec = JobSpec { promise: promise.to_string(), workdir: workdir.to_path_buf(), res: res.clone() };
        if let Err(e) = backend.launch(spec).await {
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
        let (_, route, held_at) = self.nearest(set, id, 0.0).ok_or_else(|| format!("cannot stage {id}: no route to a holder"))?;
        let src = src.or(held_at).ok_or_else(|| format!("cannot stage {id}: no path in its fact or spec"))?;
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
        self.state_dir.join("env").join(format!("{id}.sif"))
    }

    /// Shell lines that pull the environment's image into the cache unless it is there.
    fn pull_lines(&self, env: &Env, apptainer: &Path) -> Vec<String> {
        let (app, sif) = (q(&apptainer.to_string_lossy()), q(&self.sif(&env.id).to_string_lossy()));
        vec![
            format!("mkdir -p {}", q(&self.state_dir.join("env").to_string_lossy())),
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

    /// Settles a `prep` step: fetches each staged material not already held into `held/<id>/`,
    /// holds it, and pulls the environment image. Returns the ids now held here.
    async fn prep(self: &Arc<Self>, promise: &str, idx: usize, step: &Step, set: &OfferSet, resolved: &mut Resolved) -> Result<Vec<Id>, String> {
        self.resolve_staged(step, resolved).await;
        let mut lines = vec!["set -e".to_string(), "cd \"$(dirname \"$0\")\"".to_string()];
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
                let (_, route, held_at) = self.nearest(set, id, 0.0).ok_or_else(|| format!("cannot stage {id}: no route to a holder"))?;
                let src = item["src"].as_str().or(held_at).ok_or_else(|| format!("cannot stage {id}: no path in its fact or spec"))?;
                let dir = self.state_dir.join("held").join(id);
                lines.push(format!("mkdir -p {}", q(&dir.to_string_lossy())));
                lines.push(if is_local_route(route) {
                    format!("cp -a {} {}/", q(src), q(&dir.to_string_lossy()))
                } else {
                    format!("rsync -a {} {}/", q(&format!("{route}:{src}")), q(&dir.to_string_lossy()))
                });
                let base = Path::new(src.trim_end_matches('/')).file_name().ok_or_else(|| format!("{src} has no base name"))?;
                let pairs = stage_pairs(item, id);
                let flight = Some(Flight { since: Instant::now(), est: self.est(step), owner: promise.into() });
                let eid = st.index.insert(Entry { pairs, slots: [(OUT.to_string(), id.to_string())].into(), flight });
                st.waiters.insert(eid, watch::channel(()).0);
                fetched.push((id.to_string(), dir.join(base), eid));
            }
        }
        if let (Some(env), Some(apptainer)) = (self.env_of(step)?, &self.apptainer) {
            lines.extend(self.pull_lines(&env, apptainer));
        }
        let ran = if lines.len() > 2 {
            let workdir = self.state_dir.join("work").join(format!("{promise}-{idx}"));
            std::fs::create_dir_all(&workdir).map_err(|e| e.to_string())?;
            if !workdir.join("run.sh").exists() {
                std::fs::write(workdir.join("run.sh"), lines.join("\n") + "\n").map_err(|e| e.to_string())?;
            }
            match self.exec(&self.local, promise, &workdir, &set.resources).await {
                Ok(0) => Ok(()),
                Ok(code) => Err(format!("prep exited {code}")),
                Err(e) => Err(e),
            }
        } else {
            Ok(())
        };
        let mut st = self.state.lock().unwrap();
        for (id, path, eid) in fetched {
            match &ran {
                Ok(()) => {
                    let listing = if path.is_dir() { walk_files(&path) } else { vec![] };
                    st.instances.insert(id, Instance::new(path, listing));
                    st.index.finish(eid);
                }
                Err(_) => st.index.remove(eid),
            }
            st.waiters.remove(&eid);
        }
        self.persist(&st);
        ran.map(|()| ids)
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
        let route = if to == self.cfg.id {
            ""
        } else {
            self.cfg.routes.get(to).ok_or_else(|| format!("no route to {to}"))?.as_str()
        };
        let ids: Vec<Id> = step.inputs.iter().map(|i| resolve(resolved, i)).collect();
        let workdir = self.state_dir.join("work").join(format!("{promise}-{idx}"));
        let mut lines = vec!["set -e".to_string(), "cd \"$(dirname \"$0\")\"".to_string()];
        let mut placed = vec![];
        let files_only = step.spec["files"].as_bool().unwrap_or(false);
        std::fs::create_dir_all(&workdir).map_err(|e| e.to_string())?;
        {
            let st = self.state.lock().unwrap();
            for (n, id) in ids.iter().enumerate() {
                if files_only {
                    let inst = st.instances.get(id).ok_or_else(|| format!("{id} is not held here"))?;
                    let list = workdir.join(format!("files-{n}.txt"));
                    std::fs::write(&list, inst.listing.join("\n") + "\n").map_err(|e| e.to_string())?;
                    let (from, list) = (q(&inst.path.to_string_lossy()), q(&list.to_string_lossy()));
                    if is_local_route(route) {
                        lines.push(format!("mkdir -p {d} && tar -C {from} -cf - -T {list} | tar -C {d} -xf -", d = q(dest)));
                    } else {
                        lines.push(format!(
                            "rsync -a --files-from={list} --rsync-path={} {from}/ {}",
                            q(&format!("mkdir -p {} && rsync", q(dest))),
                            q(&format!("{route}:{dest}/"))
                        ));
                    }
                    placed.push((id.clone(), dest.to_string()));
                    continue;
                }
                let src = step.spec["src"][id].as_str();
                let from = self.locate(&st, set, id, src, &workdir.join("in").join(id), &mut lines)?;
                if is_local_route(route) {
                    lines.push(format!("mkdir -p {d} && cp -a {} {d}/", q(&from), d = q(dest)));
                } else {
                    lines.push(format!(
                        "rsync -a --rsync-path={} {} {}",
                        q(&format!("mkdir -p {} && rsync", q(dest))),
                        q(&from),
                        q(&format!("{route}:{dest}/"))
                    ));
                }
                let name = Path::new(&from).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                placed.push((id.clone(), format!("{dest}/{name}")));
            }
        }
        if !workdir.join("run.sh").exists() {
            std::fs::create_dir_all(&workdir).map_err(|e| e.to_string())?;
            std::fs::write(workdir.join("run.sh"), lines.join("\n") + "\n").map_err(|e| e.to_string())?;
        }
        match self.exec(&self.local, promise, &workdir, &set.resources).await? {
            0 => Ok(placed),
            code => Err(format!("place exited {code}")),
        }
    }

    // ---- caller verbs --------------------------------------------------------------------

    async fn on_status(&self, env: &Envelope, r: PromiseRef) {
        let rec = self.state.lock().unwrap().promises.get(&r.promise).map(|p| (p.phase.clone(), p.set.steps.iter().all(Step::is_move)));
        let Some((phase, moves_only)) = rec else {
            return self.reply_env(env.reply(&raise_msg(raise::UNKNOWN_PROMISE, r.promise)));
        };
        let runner: &dyn Backend = if moves_only { &self.local } else { &*self.backend };
        let life = if phase.is_final() { None } else { Some(runner.life(&r.promise).await) };
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
        self.local.kill(&r.promise).await;
        self.reply_env(env.reply(&Msg::Return(Value::Null)));
    }

    fn on_dispose(&self, env: &Envelope, a: DisposeArgs) {
        let paths: Vec<PathBuf> = {
            let mut st = self.state.lock().unwrap();
            let held: Vec<Id> = a.ids.iter().filter(|i| st.instances.contains_key(*i)).cloned().collect();
            st.index.drop_ids(&held);
            let paths = held.iter().filter_map(|i| st.instances.remove(i)).map(|n| n.path).collect();
            self.persist(&st);
            paths
        };
        for p in paths {
            remove_path(&p);
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
