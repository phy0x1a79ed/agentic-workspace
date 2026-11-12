//! The baitan market: runs bid rounds, awards one provider per promise and keeps promise state in memory.

mod picker;
#[cfg(test)]
mod tests;

use baitan_proto::{
    raise, raise_msg, AwardArgs, BidArgs, DnuArgs, DisposeArgs, Envelope, Fact, Id, Msg, OfferArgs, OfferSet, Outcome,
    Phase, PromiseId, PromiseRef, PromiseStatus, PropSet, ProviderId, ReviewArgs, SettleArgs, StartedArgs, TIME_AXIS,
};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::{Arc, Mutex, MutexGuard, Weak};
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender};
use tokio::sync::oneshot;
use tokio::time::{Duration, Instant};

pub type LinkId = u64;
type Values = BTreeMap<String, f64>;

const RATIO_CLAMP: (f64, f64) = (0.1, 10.0);
const UNAVAILABLE: &str = "unavailable";

/// Cheap handle to one market. Carriers attach links and feed it envelopes.
#[derive(Clone)]
pub struct Market {
    inner: Arc<Mutex<Inner>>,
}

struct Inner {
    name: String,
    me: Weak<Mutex<Inner>>,
    next_link: LinkId,
    links: HashMap<LinkId, UnboundedSender<Envelope>>,
    providers: HashMap<ProviderId, Prov>,
    promises: HashMap<PromiseId, Promise>,
    holders: HashMap<Id, ProviderId>,
    stats: HashMap<(ProviderId, String), Stats>,
    watchers: Vec<Watch>,
    pending: HashMap<String, (LinkId, oneshot::Sender<Envelope>)>,
}

struct Prov {
    link: Option<LinkId>,
    versions: [u32; 2],
    props: PropSet,
}

struct Watch {
    link: LinkId,
    to: String,
    promises: Vec<PromiseId>,
}

struct Promise {
    set: OfferSet,
    v: u32,
    contract: String,
    phase: Phase,
    round: u32,
    eligible: BTreeSet<ProviderId>,
    bids: BTreeMap<ProviderId, Option<Values>>,
    broken_by: BTreeSet<ProviderId>,
    award: Option<AwardInfo>,
}

struct AwardInfo {
    at: Instant,
    bid: Values,
}

/// Track record of one provider on one contract.
#[derive(Default)]
struct Stats {
    kept: u32,
    broken: u32,
    err: BTreeMap<String, (f64, u32)>,
}

impl Stats {
    fn success(&self) -> f64 {
        (self.kept as f64 + 1.0) / ((self.kept + self.broken) as f64 + 2.0)
    }

    fn ratio(&self, axis: &str) -> f64 {
        self.err.get(axis).map_or(1.0, |(sum, n)| sum / *n as f64)
    }

    fn add_err(&mut self, axis: &str, measured: f64, bid: f64) {
        if bid > 0.0 {
            let e = self.err.entry(axis.to_string()).or_default();
            e.0 += (measured / bid).clamp(RATIO_CLAMP.0, RATIO_CLAMP.1);
            e.1 += 1;
        }
    }
}

enum Answer {
    Status(PromiseStatus),
    Lost,
    Down,
}

fn answer(env: &Envelope) -> Answer {
    match env.msg() {
        Ok(Msg::Return(v)) => serde_json::from_value(v).map_or(Answer::Down, Answer::Status),
        Ok(Msg::Raise(r)) if r.name == raise::UNKNOWN_PROMISE => Answer::Lost,
        _ => Answer::Down,
    }
}

fn ok() -> Msg {
    Msg::Return(Value::Null)
}

fn contract_of(set: &OfferSet) -> String {
    let step = set.steps.iter().find(|s| !s.is_placement()).or(set.steps.first()).expect("steps checked");
    step.props.get("contract").cloned().unwrap_or_else(|| step.op.clone())
}

impl Market {
    pub fn new(name: &str) -> Market {
        let inner = Arc::new(Mutex::new(Inner {
            name: name.into(),
            me: Weak::new(),
            next_link: 0,
            links: HashMap::new(),
            providers: HashMap::new(),
            promises: HashMap::new(),
            holders: HashMap::new(),
            stats: HashMap::new(),
            watchers: Vec::new(),
            pending: HashMap::new(),
        }));
        inner.lock().unwrap().me = Arc::downgrade(&inner);
        Market { inner }
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap()
    }

    pub fn attach(&self, tx: UnboundedSender<Envelope>) -> LinkId {
        let mut g = self.lock();
        g.next_link += 1;
        let link = g.next_link;
        g.links.insert(link, tx);
        link
    }

    pub fn detach(&self, link: LinkId) {
        let g = &mut *self.lock();
        g.links.remove(&link);
        for p in g.providers.values_mut().filter(|p| p.link == Some(link)) {
            p.link = None;
        }
        g.watchers.retain(|w| w.link != link);
        g.pending.retain(|_, (l, _)| *l != link);
        let open: Vec<PromiseId> = g.promises.iter().filter(|(_, p)| matches!(p.phase, Phase::Open)).map(|(id, _)| id.clone()).collect();
        for id in open {
            let down: Vec<ProviderId> = g.promises[&id].eligible.iter().filter(|e| g.providers.get(*e).is_none_or(|p| p.link.is_none())).cloned().collect();
            let p = g.promises.get_mut(&id).unwrap();
            for d in down {
                if !p.bids.contains_key(&d) {
                    p.eligible.remove(&d);
                }
            }
            g.check_complete(&id);
        }
    }

    /// Attach, feed inbound envelopes until `rx` closes, then detach.
    pub async fn serve(&self, tx: UnboundedSender<Envelope>, mut rx: UnboundedReceiver<Envelope>) {
        let link = self.attach(tx);
        while let Some(env) = rx.recv().await {
            if env.reply_to.is_none() && matches!(env.selector.as_str(), "status" | "dispose") {
                let m = self.clone();
                tokio::spawn(async move { m.inbound(link, env).await });
            } else {
                self.inbound(link, env).await;
            }
        }
        self.detach(link);
    }

    pub async fn inbound(&self, link: LinkId, env: Envelope) {
        if let Some(rid) = &env.reply_to {
            if let Some((_, tx)) = self.lock().pending.remove(rid) {
                let _ = tx.send(env);
            }
            return;
        }
        let msg = match env.msg() {
            Ok(m) => m,
            Err(r) => return self.lock().send(link, env.refusal_reply(&r)),
        };
        let reply = match &msg {
            Msg::Status(r) => Some(self.status(r).await),
            Msg::Dispose(a) => Some(self.dispose(a).await),
            _ => self.lock().handle(link, &env, msg.clone()),
        };
        if let Some(r) = reply {
            self.lock().send(link, env.reply(&r));
        }
        if let Msg::Register(a) = msg {
            let open = self.lock().open_awards(&a.provider);
            for id in open {
                let m = self.clone();
                tokio::spawn(async move { m.probe(&id).await });
            }
        }
    }

    /// Ask the awarded provider about a promise and apply its answer. Returns the provider's `life`.
    async fn probe(&self, id: &str) -> Option<Value> {
        let (provider, rx) = self.lock().ask_status(id)?;
        let ans = match rx {
            Some(rx) => rx.await.map_or(Answer::Down, |e| answer(&e)),
            None => Answer::Down,
        };
        let mut g = self.lock();
        match ans {
            Answer::Status(ps) => {
                g.apply_status(id, &provider, &ps.phase);
                ps.life
            }
            Answer::Lost => {
                g.settle(id, &provider, Outcome::Broken { message: "provider lost the promise".into() });
                None
            }
            Answer::Down => Some(json!(UNAVAILABLE)),
        }
    }

    async fn status(&self, r: &PromiseRef) -> Msg {
        if !self.lock().promises.contains_key(&r.promise) {
            return unknown(&r.promise);
        }
        let life = self.probe(&r.promise).await;
        let g = self.lock();
        let phase = g.promises[&r.promise].phase.clone();
        Msg::Return(serde_json::to_value(PromiseStatus { promise: r.promise.clone(), phase, life }).unwrap())
    }

    async fn dispose(&self, a: &DisposeArgs) -> Msg {
        let asks = {
            let mut g = self.lock();
            let mut groups: BTreeMap<ProviderId, Vec<Id>> = BTreeMap::new();
            for id in &a.ids {
                if let Some(h) = a.holder.clone().or_else(|| g.holders.get(id).cloned()) {
                    groups.entry(h).or_default().push(id.clone());
                }
            }
            groups
                .into_iter()
                .map(|(h, ids)| {
                    let rx = g.ask_provider(&h, &Msg::Dispose(DisposeArgs { ids: ids.clone(), holder: None }), None);
                    (ids, rx)
                })
                .collect::<Vec<_>>()
        };
        let mut failure = None;
        for (ids, rx) in asks {
            let reply = match rx {
                Some(rx) => rx.await.ok().and_then(|e| e.msg().ok()),
                None => None,
            };
            match reply {
                Some(Msg::Return(_)) => {
                    let mut g = self.lock();
                    for id in ids {
                        g.holders.remove(&id);
                    }
                }
                Some(Msg::Raise(r)) => failure = Some(r.name),
                _ => failure = Some(UNAVAILABLE.to_string()),
            }
        }
        match failure {
            None => ok(),
            Some(name) => raise_msg(&name, "a holder did not dispose"),
        }
    }
}

fn unknown(id: &str) -> Msg {
    raise_msg(raise::UNKNOWN_PROMISE, id)
}

impl Inner {
    fn send(&self, link: LinkId, env: Envelope) {
        if let Some(tx) = self.links.get(&link) {
            let _ = tx.send(env);
        }
    }

    fn provider_link(&self, pid: &str) -> Option<LinkId> {
        self.providers.get(pid)?.link
    }

    fn to_provider(&self, pid: &str, v: Option<u32>, msg: &Msg) -> bool {
        let Some(link) = self.provider_link(pid) else { return false };
        let mut env = Envelope::new(&self.name, pid, msg);
        if let Some(v) = v {
            env.v = v;
        }
        self.send(link, env);
        true
    }

    fn ask_provider(&mut self, pid: &str, msg: &Msg, v: Option<u32>) -> Option<oneshot::Receiver<Envelope>> {
        let link = self.provider_link(pid)?;
        let mut env = Envelope::new(&self.name, pid, msg);
        if let Some(v) = v {
            env.v = v;
        }
        let (tx, rx) = oneshot::channel();
        self.pending.insert(env.id.clone(), (link, tx));
        self.send(link, env);
        Some(rx)
    }

    fn ask_status(&mut self, id: &str) -> Option<(ProviderId, Option<oneshot::Receiver<Envelope>>)> {
        let p = self.promises.get(id)?;
        let (Phase::Awarded { provider } | Phase::Started { provider }) = &p.phase else { return None };
        let (provider, v) = (provider.clone(), p.v);
        let rx = self.ask_provider(&provider, &Msg::Status(PromiseRef { promise: id.into() }), Some(v));
        Some((provider, rx))
    }

    fn open_awards(&self, pid: &str) -> Vec<PromiseId> {
        let held = |p: &Promise| matches!(&p.phase, Phase::Awarded { provider } | Phase::Started { provider } if provider == pid);
        self.promises.iter().filter(|(_, p)| held(p)).map(|(id, _)| id.clone()).collect()
    }

    fn handle(&mut self, link: LinkId, env: &Envelope, msg: Msg) -> Option<Msg> {
        Some(match msg {
            Msg::Register(a) => {
                self.providers.insert(a.provider, Prov { link: Some(link), versions: a.versions, props: a.props });
                ok()
            }
            Msg::Offer(a) => self.offer(env.v, a),
            Msg::Cancel(r) => self.cancel(&r.promise),
            Msg::Review(a) => self.review(a),
            Msg::Watch(a) => {
                self.watchers.push(Watch { link, to: env.from.clone(), promises: a.promises });
                ok()
            }
            Msg::Bid(a) => self.bid(a),
            Msg::Started(StartedArgs { promise, provider }) => self.started(&promise, &provider),
            Msg::Settle(SettleArgs { promise, provider, outcome }) => {
                if self.promises.contains_key(&promise) {
                    self.settle(&promise, &provider, outcome);
                    ok()
                } else {
                    unknown(&promise)
                }
            }
            Msg::Return(_) | Msg::Raise(_) | Msg::DoesNotUnderstand(_) => return None,
            Msg::Status(_) | Msg::Dispose(_) | Msg::Award(_) | Msg::Event(_) => {
                Msg::DoesNotUnderstand(DnuArgs { selector: env.selector.clone() })
            }
        })
    }

    fn emit(&self, id: &str, life: Option<Value>) {
        let status = PromiseStatus { promise: id.into(), phase: self.promises[id].phase.clone(), life };
        for w in self.watchers.iter().filter(|w| w.promises.is_empty() || w.promises.iter().any(|p| p == id)) {
            self.send(w.link, Envelope::new(&self.name, &w.to, &Msg::Event(status.clone())));
        }
    }

    fn set_phase(&mut self, id: &str, phase: Phase) {
        self.promises.get_mut(id).unwrap().phase = phase;
        self.emit(id, None);
    }

    fn fail(&mut self, id: &str, name: &str, message: &str) {
        self.set_phase(id, Phase::Failed { name: name.into(), message: message.into() });
    }

    fn offer(&mut self, v: u32, a: OfferArgs) -> Msg {
        if !picker::PICKERS.contains(&a.set.picker.as_str()) {
            return raise_msg(raise::BAD_ARGS, format!("unknown picker {}", a.set.picker));
        }
        if a.set.steps.is_empty() {
            return raise_msg(raise::BAD_ARGS, "offer has no steps");
        }
        let id = baitan_proto::new_id();
        let contract = contract_of(&a.set);
        self.promises.insert(
            id.clone(),
            Promise {
                set: a.set,
                v,
                contract,
                phase: Phase::Open,
                round: 0,
                eligible: BTreeSet::new(),
                bids: BTreeMap::new(),
                broken_by: BTreeSet::new(),
                award: None,
            },
        );
        self.emit(&id, None);
        self.start_round(&id);
        Msg::Return(json!({ "promise": id }))
    }

    fn start_round(&mut self, id: &str) {
        let p = self.promises.get_mut(id).unwrap();
        p.round += 1;
        p.bids.clear();
        let (v, round, window) = (p.v, p.round, p.set.window_ms);
        let eligible: BTreeSet<ProviderId> = self
            .providers
            .iter()
            .filter(|(pid, pr)| {
                let p = &self.promises[id];
                pr.link.is_some()
                    && pr.versions[0] <= v
                    && v <= pr.versions[1]
                    && !p.broken_by.contains(*pid)
                    && p.set.place.as_ref().is_none_or(|pl| pl.iter().all(|(k, val)| pr.props.get(k) == Some(val)))
            })
            .map(|(pid, _)| pid.clone())
            .collect();
        if eligible.is_empty() {
            return self.fail(id, raise::NO_BIDS, "no eligible provider");
        }
        let set = self.promises[id].set.clone();
        for pid in &eligible {
            self.to_provider(pid, Some(v), &Msg::Offer(OfferArgs { set: set.clone(), promise: Some(id.into()) }));
        }
        self.promises.get_mut(id).unwrap().eligible = eligible;
        let (me, id) = (self.me.clone(), id.to_string());
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(window)).await;
            if let Some(m) = me.upgrade() {
                m.lock().unwrap().close_round(&id, round);
            }
        });
    }

    fn check_complete(&mut self, id: &str) {
        let p = &self.promises[id];
        if matches!(p.phase, Phase::Open) && p.eligible.iter().all(|e| p.bids.contains_key(e)) {
            let round = p.round;
            self.close_round(id, round);
        }
    }

    fn corrected(&self, pid: &str, contract: &str, bid: &Values) -> Values {
        let stats = self.stats.get(&(pid.to_string(), contract.to_string()));
        bid.iter()
            .map(|(axis, v)| {
                let mut v = v * stats.map_or(1.0, |s| s.ratio(axis));
                if axis == TIME_AXIS {
                    v /= stats.map_or(0.5, Stats::success);
                }
                (axis.clone(), v)
            })
            .collect()
    }

    fn close_round(&mut self, id: &str, round: u32) {
        let Some(p) = self.promises.get(id) else { return };
        if p.round != round || !matches!(p.phase, Phase::Open) {
            return;
        }
        let live: Vec<(ProviderId, Values, Values)> = p
            .bids
            .iter()
            .filter_map(|(pid, b)| Some((pid, b.as_ref()?)))
            .filter(|(pid, _)| self.provider_link(pid).is_some())
            .map(|(pid, raw)| (pid.clone(), raw.clone(), self.corrected(pid, &p.contract, raw)))
            .collect();
        if live.is_empty() {
            return self.fail(id, raise::NO_BIDS, "no bids");
        }
        let scored: Vec<Values> = live.iter().map(|l| l.2.clone()).collect();
        let win = picker::pick(&p.set.picker, &p.set.axes, &scored);
        let (winner, raw, _) = live[win].clone();
        tracing::info!("{id} awarded to {winner} out of {} bids", live.len());
        let (set, v) = (p.set.clone(), p.v);
        self.promises.get_mut(id).unwrap().award = Some(AwardInfo { at: Instant::now(), bid: raw });
        let phase = Phase::Awarded { provider: winner.clone() };
        for (pid, _, _) in live.iter().filter(|l| l.0 != winner) {
            let ev = PromiseStatus { promise: id.into(), phase: phase.clone(), life: None };
            self.to_provider(pid, Some(v), &Msg::Event(ev));
        }
        self.to_provider(&winner, Some(v), &Msg::Award(AwardArgs { promise: id.into(), set }));
        self.set_phase(id, phase);
    }

    fn bid(&mut self, a: BidArgs) -> Msg {
        let Some(p) = self.promises.get_mut(&a.promise) else { return unknown(&a.promise) };
        match (&a.values, &a.reason) {
            (Some(v), _) => tracing::info!("{} bids {:?} on {}", a.provider, v, a.promise),
            (None, r) => tracing::info!("{} declines {}: {}", a.provider, a.promise, r.as_deref().unwrap_or("-")),
        }
        if matches!(p.phase, Phase::Open) && p.eligible.contains(&a.provider) && !p.bids.contains_key(&a.provider) {
            p.bids.insert(a.provider, a.values);
            self.check_complete(&a.promise);
        }
        ok()
    }

    fn started(&mut self, id: &str, provider: &str) -> Msg {
        match self.promises.get(id).map(|p| &p.phase) {
            None => return unknown(id),
            Some(Phase::Awarded { provider: q }) if q == provider => {
                self.set_phase(id, Phase::Started { provider: provider.into() })
            }
            _ => {}
        }
        ok()
    }

    fn apply_status(&mut self, id: &str, provider: &str, phase: &Phase) {
        match phase {
            Phase::Settled { facts, sizes, listing, .. } => {
                let outcome = Outcome::Kept { facts: facts.clone(), sizes: sizes.clone(), listing: listing.clone() };
                self.settle(id, provider, outcome)
            }
            Phase::Failed { message, .. } => self.settle(id, provider, Outcome::Broken { message: message.clone() }),
            _ => {}
        }
    }

    /// Applies a settle from the provider holding the live award; anything else is stale and ignored.
    fn settle(&mut self, id: &str, provider: &str, outcome: Outcome) {
        let p = self.promises.get_mut(id).unwrap();
        let held = matches!(&p.phase, Phase::Awarded { provider: q } | Phase::Started { provider: q } if q == provider);
        let Some(award) = p.award.as_ref().filter(|_| held) else { return };
        let elapsed = award.at.elapsed().as_secs_f64();
        let time_bid = award.bid.get(TIME_AXIS).copied().unwrap_or(0.0);
        let stats = self.stats.entry((provider.to_string(), p.contract.clone())).or_default();
        match outcome {
            Outcome::Kept { facts, sizes, listing } => {
                stats.kept += 1;
                stats.add_err(TIME_AXIS, elapsed, time_bid);
                for (fid, at) in facts.iter().filter_map(Fact::as_at) {
                    self.holders.insert(fid.to_string(), at.to_string());
                }
                self.set_phase(id, Phase::Settled { provider: provider.into(), facts, sizes, listing });
            }
            Outcome::Broken { .. } => {
                stats.broken += 1;
                p.broken_by.insert(provider.into());
                p.award = None;
                self.set_phase(id, Phase::Open);
                self.start_round(id);
            }
        }
    }

    fn cancel(&mut self, id: &str) -> Msg {
        let Some(p) = self.promises.get(id) else { return unknown(id) };
        let v = p.v;
        match p.phase.clone() {
            Phase::Open => {}
            Phase::Awarded { provider } | Phase::Started { provider } => {
                self.to_provider(&provider, Some(v), &Msg::Cancel(PromiseRef { promise: id.into() }));
            }
            _ => return ok(),
        }
        self.fail(id, raise::CANCELLED, "cancelled by caller");
        ok()
    }

    fn review(&mut self, a: ReviewArgs) -> Msg {
        let Some(p) = self.promises.get(&a.promise) else { return unknown(&a.promise) };
        let (Phase::Settled { provider, .. }, Some(award)) = (&p.phase, &p.award) else {
            return raise_msg("NotSettled", "review needs a settled promise");
        };
        let (provider, v, key) = (provider.clone(), p.v, (provider.clone(), p.contract.clone()));
        let stats = self.stats.entry(key).or_default();
        for (axis, score) in a.scores.iter().filter(|(axis, _)| *axis != TIME_AXIS) {
            if let Some(bid) = award.bid.get(axis) {
                stats.add_err(axis, *score, *bid);
            }
        }
        self.to_provider(&provider, Some(v), &Msg::Review(a));
        ok()
    }
}
