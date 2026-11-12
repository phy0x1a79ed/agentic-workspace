use super::*;
use baitan_proto::{Axis, Hit, RegisterArgs, WatchArgs};
use std::collections::VecDeque;
use tokio::sync::mpsc::unbounded_channel;

struct Peer {
    m: Market,
    id: String,
    link: LinkId,
    rx: UnboundedReceiver<Envelope>,
    stash: VecDeque<Envelope>,
}

impl Peer {
    fn new(m: &Market, id: &str) -> Peer {
        let (tx, rx) = unbounded_channel();
        Peer { m: m.clone(), id: id.into(), link: m.attach(tx), rx, stash: VecDeque::new() }
    }

    async fn send(&self, msg: &Msg) {
        self.m.inbound(self.link, Envelope::new(&self.id, "market", msg)).await;
    }

    async fn ask_env(&mut self, e: Envelope) -> Msg {
        let id = e.id.clone();
        self.m.inbound(self.link, e).await;
        loop {
            let r = self.rx.try_recv().expect("reply");
            if r.reply_to.as_deref() == Some(&id) {
                return r.msg().unwrap();
            }
            self.stash.push_back(r);
        }
    }

    async fn ask(&mut self, msg: Msg) -> Msg {
        let e = Envelope::new(&self.id, "market", &msg);
        self.ask_env(e).await
    }

    async fn reg(&mut self, versions: [u32; 2], props: &[(&str, &str)]) {
        let args = RegisterArgs { provider: self.id.clone(), versions, props: baitan_proto::props(props.iter().copied()) };
        assert_eq!(self.ask(Msg::Register(args)).await, ok());
    }

    async fn next(&mut self) -> Msg {
        self.next_env().await.msg().unwrap()
    }

    /// The next envelope that is not a reply to one of this peer's asks.
    async fn next_env(&mut self) -> Envelope {
        loop {
            let e = match self.stash.pop_front() {
                Some(e) => e,
                None => self.rx.recv().await.unwrap(),
            };
            if e.reply_to.is_none() {
                return e;
            }
        }
    }

    fn none_pending(&mut self) -> bool {
        while let Ok(e) = self.rx.try_recv() {
            self.stash.push_back(e);
        }
        self.stash.retain(|e| e.reply_to.is_none());
        self.stash.is_empty()
    }

    async fn reply(&self, to: &Envelope, msg: Msg) {
        self.m.inbound(self.link, to.reply(&msg)).await;
    }

    async fn bid(&self, promise: &str, values: Option<Values>) {
        let (hit, reason) = (None, values.is_none().then(|| "no".to_string()));
        self.send(&Msg::Bid(BidArgs { promise: promise.into(), provider: self.id.clone(), values, hit, reason })).await;
    }

    async fn settle(&self, promise: &str, outcome: Outcome) {
        self.send(&Msg::Settle(SettleArgs { promise: promise.into(), provider: self.id.clone(), outcome })).await;
    }

    async fn started(&self, promise: &str) {
        self.send(&Msg::Started(StartedArgs { promise: promise.into(), provider: self.id.clone() })).await;
    }

    async fn expect_offer(&mut self) -> PromiseId {
        match self.next().await {
            Msg::Offer(a) => a.promise.unwrap(),
            m => panic!("expected offer, got {m:?}"),
        }
    }

    async fn expect_award(&mut self) -> PromiseId {
        match self.next().await {
            Msg::Award(a) => a.promise,
            m => panic!("expected award, got {m:?}"),
        }
    }

    async fn event(&mut self) -> PromiseStatus {
        match self.next().await {
            Msg::Event(ps) => ps,
            m => panic!("expected event, got {m:?}"),
        }
    }

    async fn offer(&mut self, set: OfferSet) -> PromiseId {
        match self.ask(Msg::Offer(OfferArgs { set, promise: None })).await {
            Msg::Return(v) => v["promise"].as_str().unwrap().into(),
            m => panic!("{m:?}"),
        }
    }

    async fn status(&mut self, id: &str) -> PromiseStatus {
        match self.ask(Msg::Status(PromiseRef { promise: id.into() })).await {
            Msg::Return(v) => serde_json::from_value(v).unwrap(),
            m => panic!("{m:?}"),
        }
    }

    async fn watch_all(&mut self) {
        assert_eq!(self.ask(Msg::Watch(WatchArgs { promises: vec![] })).await, ok());
    }
}

fn set(extra: Value) -> OfferSet {
    let mut base = json!({"steps": [{"op": "task", "props": {"contract": "c"}}]});
    base.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
    serde_json::from_value(base).unwrap()
}

fn vals(kv: &[(&str, f64)]) -> Values {
    kv.iter().map(|(k, v)| (k.to_string(), *v)).collect()
}

fn t(x: f64) -> Values {
    vals(&[("time", x)])
}

fn kept(id: &str, provider: &str, size: u64) -> Outcome {
    Outcome::Kept { facts: vec![Fact::at(id, provider)], sizes: [(id.to_string(), size)].into(), listing: BTreeMap::new() }
}

fn failed_name(ps: &PromiseStatus) -> &str {
    match &ps.phase {
        Phase::Failed { name, .. } => name,
        p => panic!("not failed: {p:?}"),
    }
}

async fn award_to(p: &mut Peer, c: &mut Peer, time: f64) -> PromiseId {
    let id = c.offer(set(json!({}))).await;
    assert_eq!(p.expect_offer().await, id);
    p.bid(&id, Some(t(time))).await;
    assert_eq!(p.expect_award().await, id);
    id
}

#[tokio::test(start_paused = true)]
async fn a2_version_ranges_and_place_gate_offers() {
    let m = Market::new("market");
    let (mut c, mut a, mut b, mut far) = (Peer::new(&m, "c"), Peer::new(&m, "a"), Peer::new(&m, "b"), Peer::new(&m, "far"));
    a.reg([1, 1], &[]).await;
    b.reg([2, 3], &[]).await;
    far.reg([1, 1], &[("site", "x")]).await;

    let id = c.offer(set(json!({}))).await;
    assert_eq!(a.expect_offer().await, id);
    assert_eq!(far.expect_offer().await, id);
    assert!(b.none_pending());
    a.bid(&id, None).await;
    far.bid(&id, None).await;
    assert_eq!(failed_name(&c.status(&id).await), raise::NO_BIDS);

    let id = c.offer(set(json!({"place": {"site": "y"}}))).await;
    assert_eq!(failed_name(&c.status(&id).await), raise::NO_BIDS);
    assert!(a.none_pending() && far.none_pending());

    let mut e = Envelope::new("c", "market", &Msg::Watch(WatchArgs { promises: vec![] }));
    e.v = 7;
    match c.ask_env(e).await {
        Msg::Raise(r) => assert_eq!((r.name.as_str(), r.supported), (raise::UNSUPPORTED_VERSION, Some(baitan_proto::SUPPORTED))),
        m => panic!("{m:?}"),
    }
}

#[tokio::test]
async fn a9_pickers() {
    let axes: Vec<Axis> = serde_json::from_value(json!([
        {"name": "time", "weight": 0.2},
        {"name": "quality", "weight": 1.0, "higher_better": true}
    ]))
    .unwrap();
    let cached = vals(&[("time", 0.0), ("quality", 0.5)]);
    let better = vals(&[("time", 30.0), ("quality", 0.95)]);
    let third = vals(&[("time", 30.0), ("quality", 0.9)]);
    assert_eq!(picker::pick("robust-z", &axes, &[cached.clone(), better.clone()]), 1);
    assert_eq!(picker::pick("robust-z", &axes, &[cached.clone(), better.clone(), third.clone()]), 1);
    assert_eq!(picker::pick("ratio", &axes, &[cached, better, third]), 1);

    let time_only = &axes[..1];
    assert_eq!(picker::pick("robust-z", time_only, &[t(12.0), t(10.0), t(50.0)]), 1);
    assert_eq!(picker::pick("robust-z", time_only, &[t(7.0)]), 0);

    let m = Market::new("market");
    let mut c = Peer::new(&m, "c");
    match c.ask(Msg::Offer(OfferArgs { set: set(json!({"picker": "vibes"})), promise: None })).await {
        Msg::Raise(r) => assert_eq!(r.name, raise::BAD_ARGS),
        m => panic!("{m:?}"),
    }
}

#[tokio::test(start_paused = true)]
async fn a7_a8_a10_a12_round_lifecycle() {
    let m = Market::new("market");
    let (mut c, mut p1, mut p2) = (Peer::new(&m, "c"), Peer::new(&m, "p1"), Peer::new(&m, "p2"));
    p1.reg([1, 1], &[]).await;
    p2.reg([1, 1], &[]).await;
    c.watch_all().await;

    let t0 = Instant::now();
    let id = c.offer(set(json!({}))).await;
    assert!(matches!(c.event().await.phase, Phase::Open));
    assert_eq!(p1.expect_offer().await, id);
    assert_eq!(p2.expect_offer().await, id);
    p1.bid(&id, Some(t(5.0))).await;
    p2.bid(&id, Some(t(9.0))).await;
    assert!(t0.elapsed() < Duration::from_millis(500), "closed early");

    assert_eq!(p1.expect_award().await, id);
    assert_eq!(p2.event().await.phase, Phase::Awarded { provider: "p1".into() });
    assert_eq!(c.event().await.phase, Phase::Awarded { provider: "p1".into() });
    p1.started(&id).await;
    assert_eq!(c.event().await.phase, Phase::Started { provider: "p1".into() });

    let life = PromiseStatus { promise: id.clone(), phase: Phase::Started { provider: "p1".into() }, life: Some(json!("running")) };
    let (st, ()) = tokio::join!(c.status(&id), async {
        let e = p1.next_env().await;
        assert!(matches!(e.msg().unwrap(), Msg::Status(_)));
        p1.reply(&e, Msg::Return(serde_json::to_value(&life).unwrap())).await;
    });
    assert_eq!((st.phase, st.life), (Phase::Started { provider: "p1".into() }, Some(json!("running"))));

    p1.settle(&id, kept("out", "p1", 1234)).await;
    let want = Phase::Settled {
        provider: "p1".into(),
        facts: vec![Fact::at("out", "p1")],
        sizes: [("out".to_string(), 1234)].into(),
        listing: BTreeMap::new(),
    };
    assert_eq!(c.event().await.phase, want);
    assert_eq!(c.status(&id).await.phase, want);

    let id2 = c.offer(set(json!({}))).await;
    c.event().await;
    p1.expect_offer().await;
    p2.expect_offer().await;
    let hit = Msg::Bid(BidArgs { promise: id2.clone(), provider: "p1".into(), values: Some(t(0.0)), hit: Some(Hit::Done), reason: None });
    p1.send(&hit).await;
    p2.bid(&id2, Some(t(9.0))).await;
    assert_eq!(p1.expect_award().await, id2);
    p1.started(&id2).await;
    p1.settle(&id2, kept("out2", "p1", 5)).await;
    assert!(matches!(c.status(&id2).await.phase, Phase::Settled { .. }));
}

#[tokio::test(start_paused = true)]
async fn a10_window_and_no_bids() {
    let m = Market::new("market");
    let (mut c, mut p1, mut p2) = (Peer::new(&m, "c"), Peer::new(&m, "p1"), Peer::new(&m, "p2"));
    p1.reg([1, 1], &[]).await;
    p2.reg([1, 1], &[]).await;

    let t0 = Instant::now();
    let id = c.offer(set(json!({"window_ms": 500}))).await;
    p1.expect_offer().await;
    p2.expect_offer().await;
    p1.bid(&id, Some(t(5.0))).await;
    assert!(p1.none_pending(), "p2 is still out, so the round stays open");
    assert_eq!(p1.expect_award().await, id);
    assert!(t0.elapsed() >= Duration::from_millis(500));

    let t1 = Instant::now();
    let id = c.offer(set(json!({}))).await;
    p1.expect_offer().await;
    p2.expect_offer().await;
    p1.bid(&id, None).await;
    p2.bid(&id, None).await;
    let st = c.status(&id).await;
    assert_eq!(failed_name(&st), raise::NO_BIDS);
    assert!(t1.elapsed() < Duration::from_millis(500));
    tokio::time::sleep(Duration::from_secs(5)).await;
    assert!(p1.none_pending() && p2.none_pending(), "no retry");
}

#[derive(Default)]
struct Shared {
    live: HashMap<String, u32>,
    max_live: u32,
    awards: Vec<(String, String)>,
}

async fn spawn_mock(m: &Market, id: &'static str, bid: f64, run: u64, breaks: fn(u32) -> bool, sh: Arc<Mutex<Shared>>) {
    let mut p = Peer::new(m, id);
    p.reg([1, 1], &[]).await;
    tokio::spawn(async move {
        let mut n = 0;
        while let Some(e) = p.rx.recv().await {
            match e.msg() {
                Ok(Msg::Offer(a)) => p.bid(&a.promise.unwrap(), Some(t(bid))).await,
                Ok(Msg::Award(a)) => {
                    n += 1;
                    let brk = breaks(n);
                    {
                        let mut s = sh.lock().unwrap();
                        let live = s.live.entry(a.promise.clone()).or_default();
                        *live += 1;
                        let live = *live;
                        s.max_live = s.max_live.max(live);
                        s.awards.push((a.promise.clone(), id.to_string()));
                    }
                    p.started(&a.promise).await;
                    let (m, link, sh) = (p.m.clone(), p.link, sh.clone());
                    tokio::spawn(async move {
                        tokio::time::sleep(Duration::from_secs(run)).await;
                        *sh.lock().unwrap().live.get_mut(&a.promise).unwrap() -= 1;
                        let outcome = if brk { Outcome::Broken { message: "boom".into() } } else { kept(&format!("o-{}", a.promise), id, 1) };
                        let settle = Msg::Settle(SettleArgs { promise: a.promise, provider: id.into(), outcome });
                        m.inbound(link, Envelope::new(id, "market", &settle)).await;
                    });
                }
                _ => {}
            }
        }
    });
}

#[tokio::test(start_paused = true)]
async fn a13_flaky_provider_loses_awards_without_duplicates() {
    let m = Market::new("market");
    let sh = Arc::new(Mutex::new(Shared::default()));
    spawn_mock(&m, "a-flaky", 10.0, 10, |n| n % 4 != 0, sh.clone()).await;
    spawn_mock(&m, "b-reliable", 20.0, 20, |_| false, sh.clone()).await;
    let mut c = Peer::new(&m, "c");
    c.watch_all().await;

    let mut winners = vec![];
    let mut ids = vec![];
    for _ in 0..20 {
        let id = c.offer(set(json!({}))).await;
        loop {
            let ev = c.event().await;
            match ev.phase {
                Phase::Settled { provider, .. } if ev.promise == id => break winners.push(provider),
                Phase::Failed { .. } => panic!("promise failed"),
                _ => {}
            }
        }
        ids.push(id);
    }

    let s = sh.lock().unwrap();
    assert_eq!(s.max_live, 1, "one live award per promise");
    let providers_of = |id: &str| s.awards.iter().filter(|a| a.0 == id).map(|a| a.1.clone()).collect::<Vec<_>>();
    assert_eq!(providers_of(&ids[0]), ["a-flaky", "b-reliable"], "one fresh round without the flaky provider");
    assert!(ids.iter().all(|id| providers_of(id).len() <= 2));
    assert!(winners[10..].iter().all(|w| w == "b-reliable"), "{winners:?}");
}

#[tokio::test(start_paused = true)]
async fn a14_a15_a16_caller_verbs() {
    let m = Market::new("market");
    let (mut c, mut p) = (Peer::new(&m, "c"), Peer::new(&m, "p"));
    p.reg([1, 1], &[]).await;

    let id = c.offer(set(json!({}))).await;
    p.expect_offer().await;
    assert_eq!(c.ask(Msg::Cancel(PromiseRef { promise: id.clone() })).await, ok());
    assert_eq!(failed_name(&c.status(&id).await), raise::CANCELLED);
    tokio::time::sleep(Duration::from_secs(1)).await;
    assert!(p.none_pending(), "no award after a cancel before award");

    let id = award_to(&mut p, &mut c, 1.0).await;
    assert_eq!(c.ask(Msg::Cancel(PromiseRef { promise: id.clone() })).await, ok());
    assert!(matches!(p.next().await, Msg::Cancel(r) if r.promise == id));
    assert_eq!(failed_name(&c.status(&id).await), raise::CANCELLED);
    p.settle(&id, kept("late", "p", 1)).await;
    assert_eq!(failed_name(&c.status(&id).await), raise::CANCELLED);

    let id = award_to(&mut p, &mut c, 1.0).await;
    p.started(&id).await;
    let review = Msg::Review(ReviewArgs { promise: id.clone(), scores: [("quality".to_string(), 0.9)].into() });
    assert!(matches!(c.ask(review.clone()).await, Msg::Raise(r) if r.name == "NotSettled"));
    p.settle(&id, kept("x", "p", 3)).await;
    assert_eq!(c.ask(review.clone()).await, ok());
    assert_eq!(p.next().await, review);

    let dispose = |ids: &[&str], holder: Option<&str>| Msg::Dispose(DisposeArgs { ids: ids.iter().map(|s| s.to_string()).collect(), holder: holder.map(Into::into) });
    let (r, ()) = tokio::join!(c.ask(dispose(&["x", "nope"], None)), async {
        let e = p.next_env().await;
        assert!(matches!(e.msg().unwrap(), Msg::Dispose(a) if a.ids == ["x"]));
        p.reply(&e, ok()).await;
    });
    assert_eq!(r, ok());
    assert_eq!(c.ask(dispose(&["x"], None)).await, ok());
    assert!(p.none_pending(), "x is no longer held");
    let (r, ()) = tokio::join!(c.ask(dispose(&["y"], Some("p"))), async {
        let e = p.next_env().await;
        assert!(matches!(e.msg().unwrap(), Msg::Dispose(a) if a.ids == ["y"]));
        p.reply(&e, ok()).await;
    });
    assert_eq!(r, ok());

    let id = award_to(&mut p, &mut c, 1.0).await;
    p.settle(&id, Outcome::Broken { message: "boom".into() }).await;
    let st = c.status(&id).await;
    assert_eq!(failed_name(&st), raise::NO_BIDS);
}

#[tokio::test(start_paused = true)]
async fn a20_dropped_link_and_reregister() {
    let m = Market::new("market");
    let (mut c, mut p) = (Peer::new(&m, "c"), Peer::new(&m, "p"));
    p.reg([1, 1], &[]).await;
    let (ida, idb) = (award_to(&mut p, &mut c, 1.0).await, award_to(&mut p, &mut c, 1.0).await);
    p.started(&ida).await;
    c.watch_all().await;

    m.detach(p.link);
    let st = c.status(&ida).await;
    assert_eq!((st.phase, st.life), (Phase::Started { provider: "p".into() }, Some(json!("unavailable"))));
    let idn = c.offer(set(json!({}))).await;
    assert_eq!(failed_name(&c.status(&idn).await), raise::NO_BIDS);

    let mut p2 = Peer::new(&m, "p");
    p2.reg([1, 1], &[]).await;
    for _ in 0..2 {
        let e = p2.next_env().await;
        let Msg::Status(r) = e.msg().unwrap() else { panic!("expected status ask") };
        let answer = if r.promise == ida {
            let phase = Phase::Settled { provider: "p".into(), facts: vec![Fact::at("o", "p")], sizes: [("o".to_string(), 7)].into(), listing: BTreeMap::new() };
            Msg::Return(serde_json::to_value(PromiseStatus { promise: ida.clone(), phase, life: None }).unwrap())
        } else {
            raise_msg(raise::UNKNOWN_PROMISE, "gone")
        };
        p2.reply(&e, answer).await;
    }
    let mut seen = (false, false);
    while seen != (true, true) {
        let ev = c.event().await;
        seen.0 |= ev.promise == ida && matches!(ev.phase, Phase::Settled { .. });
        seen.1 |= ev.promise == idb && matches!(&ev.phase, Phase::Failed { name, .. } if name == raise::NO_BIDS);
    }
    assert!(matches!(c.status(&ida).await.phase, Phase::Settled { .. }));
}
