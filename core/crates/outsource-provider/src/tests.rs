//! Provider behaviour over a real `serve` loop with scripted jobs: no network, no real builds.

use crate::{serve, Provider, ProviderConfig};
use outsource_carrier::Link;
use outsource_proto::*;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio::task::JoinHandle;

struct Mkt {
    tx: UnboundedSender<Envelope>,
    rx: UnboundedReceiver<Envelope>,
    serving: JoinHandle<anyhow::Result<()>>,
}

impl Mkt {
    fn start(providers: Vec<Provider>) -> Mkt {
        let (tx, p_rx) = unbounded_channel();
        let (p_tx, rx) = unbounded_channel();
        let serving = tokio::spawn(serve(providers, Link { tx: p_tx, rx: p_rx }));
        Mkt { tx, rx, serving }
    }

    fn send(&self, to: &str, msg: Msg) -> String {
        let env = Envelope::new("market", to, &msg);
        let id = env.id.clone();
        self.tx.send(env).unwrap();
        id
    }

    async fn wait<T>(&mut self, mut f: impl FnMut(&Envelope, Msg) -> Option<T>) -> T {
        loop {
            let env = tokio::time::timeout(Duration::from_secs(20), self.rx.recv())
                .await
                .expect("timed out waiting for the provider")
                .expect("link closed");
            if let Ok(m) = env.msg() {
                if let Some(t) = f(&env, m) {
                    return t;
                }
            }
        }
    }

    async fn bid(&mut self, to: &str, promise: &str, set: &OfferSet) -> BidArgs {
        self.send(to, Msg::Offer(OfferArgs { set: set.clone(), promise: Some(promise.into()), inbound: vec![] }));
        let (to, promise) = (to.to_string(), promise.to_string());
        self.wait(move |_, m| match m {
            Msg::Bid(b) if b.provider == to && b.promise == promise => Some(b),
            _ => None,
        })
        .await
    }

    fn award(&self, to: &str, promise: &str, set: &OfferSet) {
        self.send(to, Msg::Award(AwardArgs { promise: promise.into(), set: set.clone() }));
    }

    async fn settle(&mut self, promise: &str) -> Outcome {
        let promise = promise.to_string();
        self.wait(move |_, m| match m {
            Msg::Settle(s) if s.promise == promise => Some(s.outcome),
            _ => None,
        })
        .await
    }

    async fn run(&mut self, to: &str, promise: &str, set: &OfferSet) -> Outcome {
        self.bid(to, promise, set).await;
        self.award(to, promise, set);
        self.settle(promise).await
    }

    async fn ask(&mut self, to: &str, msg: Msg) -> Msg {
        let id = self.send(to, msg);
        self.wait(move |e, m| (e.reply_to.as_deref() == Some(&id)).then_some(m)).await
    }

    async fn status(&mut self, to: &str, promise: &str) -> Result<PromiseStatus, String> {
        match self.ask(to, Msg::Status(PromiseRef { promise: promise.into() })).await {
            Msg::Return(v) => Ok(serde_json::from_value(v).unwrap()),
            Msg::Raise(r) => Err(r.name),
            m => panic!("unexpected {m:?}"),
        }
    }
}

fn cfg(dir: &Path, id: &str) -> ProviderConfig {
    ProviderConfig { slots: 1, default_secs: 10.0, state_dir: dir.join(id), ..ProviderConfig::new(id) }
}

fn task(props: &[(&str, &str)], script: &str, inputs: Vec<Id>) -> Step {
    Step {
        op: "task".into(),
        props: props.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(),
        inputs,
        slots: vec!["out".into()],
        spec: json!({"script": script, "outputs": ["*.txt"]}),
        to: None,
    }
}

fn set_of(steps: Vec<Step>) -> OfferSet {
    serde_json::from_value(json!({"steps": steps})).unwrap()
}

fn out_id(step: &Step) -> Id {
    step.slot_ids().remove(0).1
}

fn runs(count: &Path) -> usize {
    std::fs::read_to_string(count).map(|s| s.lines().count()).unwrap_or(0)
}

fn kept(o: &Outcome) -> (&Vec<Fact>, &std::collections::BTreeMap<Id, u64>, &std::collections::BTreeMap<Id, Vec<String>>) {
    match o {
        Outcome::Kept { facts, sizes, listing, .. } => (facts, sizes, listing),
        Outcome::Broken { message } => panic!("broken: {message}"),
    }
}

fn stats_of(o: &Outcome) -> Option<&RunStats> {
    match o {
        Outcome::Kept { stats, .. } => stats.as_ref(),
        Outcome::Broken { message } => panic!("broken: {message}"),
    }
}

fn kept_ids(o: &Outcome) -> Vec<Id> {
    kept(o).0.iter().filter_map(Fact::as_at).map(|(i, _)| i.to_string()).collect()
}

async fn until(path: &Path) {
    for _ in 0..400 {
        if path.exists() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    panic!("{} never appeared", path.display());
}

fn gate_script(dir: &Path, begun: &str, gate: &str, count: &str) -> String {
    format!(
        "echo x >> {c}; touch {b}; while [ ! -e {g} ]; do sleep 0.05; done; echo done > out.txt",
        c = dir.join(count).display(),
        b = dir.join(begun).display(),
        g = dir.join(gate).display()
    )
}

fn write_library(root: &Path, files: &[(&str, &str, &str)]) -> PathBuf {
    std::fs::create_dir_all(root.join("_metadata")).unwrap();
    let mut yml = String::from("manifest:\n");
    for (path, ver, bytes) in files {
        std::fs::write(root.join(path), bytes).unwrap();
        yml += &format!(
            "  {path}:\n    type: ref::db\n    props: {{op: task, contract: diamond-db, version: '{ver}'}}\n"
        );
    }
    let index = root.join("_metadata").join("index.yml");
    std::fs::write(&index, yml).unwrap();
    index
}

/// An apptainer that logs its arguments, makes `pull` wait while `<dir>/hold` exists then write the image, and runs `exec` plainly.
fn fake_apptainer(dir: &Path) -> (PathBuf, PathBuf) {
    let (log, fake) = (dir.join("apptainer.log"), dir.join("apptainer"));
    std::fs::write(
        &fake,
        format!(
            "#!/bin/sh\necho \"$@\" >> {log}\ncase \"$1\" in pull) while [ -e {hold} ]; do sleep 0.05; done; shift; [ \"$1\" = --force ] && shift; echo sif > \"$1\";; exec) shift; while [ \"$1\" = --bind ]; do shift 2; done; shift; exec \"$@\";; esac\n",
            log = log.display(),
            hold = dir.join("hold").display()
        ),
    )
    .unwrap();
    std::process::Command::new("chmod").arg("755").arg(&fake).status().unwrap();
    (log, fake)
}

fn ty(kv: &[(&str, &str)]) -> PropSet {
    props(kv.iter().copied())
}

/// A `prep` set staging one material by type, with `start` facts and an optional env stage.
fn prep_of(kv: &[(&str, &str)], start: Vec<Fact>) -> (OfferSet, Id) {
    let (t, id) = (ty(kv), instance_id(&ty(kv)));
    let step = Step {
        op: "prep".into(),
        props: ty(&[("contract", "outsource-prep")]),
        inputs: vec![id.clone()],
        slots: vec![],
        spec: json!({"stage": {"m": {"id": id, "type": t}}}),
        to: None,
    };
    let mut set = set_of(vec![step]);
    set.start = start;
    (set, id)
}

#[tokio::test]
async fn a3_inputs_priced_from_the_nearest_place() {
    let d = tempfile::tempdir().unwrap();
    let p1 = Provider::local(cfg(d.path(), "p1"));
    let mut c2 = cfg(d.path(), "p2");
    c2.routes = [("p1".into(), "local".into()), ("far".into(), "user@far".into())].into();
    c2.bandwidth = 1e6;
    let mut c3 = cfg(d.path(), "p3");
    c3.routes = [("far".into(), "user@far".into())].into();
    c3.bandwidth = 1e6;
    let (p2, p3) = (Provider::local(c2), Provider::local(c3));
    let ids = p1.import(&write_library(&d.path().join("lib"), &[("data.txt", "1", "hello")])).await.unwrap();

    let mut set = set_of(vec![task(&[("contract", "use")], "true", ids.clone())]);
    set.start = vec![Fact::at(&ids[0], "p1"), Fact::at(&ids[0], "far")];
    set.size = Some(4_000_000);
    let mut m = Mkt::start(vec![p1, p2, p3]);
    let t = |b: BidArgs| b.values.unwrap()["time"];
    let (t1, t2, t3) = (t(m.bid("p1", "a", &set).await), t(m.bid("p2", "a", &set).await), t(m.bid("p3", "a", &set).await));
    assert_eq!(t1, 10.0, "holder pays nothing to fetch");
    assert!(t2 > t1 && t2 < 10.5, "p2 fetches from the local route, not from far: {t2}");
    assert!(t3 > 14.0, "p3 only reaches far: {t3}");
}

#[tokio::test]
async fn a4_build_then_place_end_to_end() {
    let d = tempfile::tempdir().unwrap();
    let mut c = cfg(d.path(), "alpha");
    c.routes = [("beta".into(), "local".into())].into();
    let dest = d.path().join("beta-ws");
    let build = task(&[("contract", "mk")], "echo hi > out.txt; echo noise > skip.dat", vec![]);
    let out = out_id(&build);
    let place = Step {
        op: "place".into(),
        props: Default::default(),
        inputs: vec![out.clone()],
        slots: vec![],
        spec: json!({"path": dest}),
        to: Some("beta".into()),
    };
    let set = set_of(vec![build, place.clone()]);
    let mut g = cfg(d.path(), "gamma");
    g.routes = [("alpha".into(), "local".into())].into();
    let mut m = Mkt::start(vec![Provider::local(c), Provider::local(g)]);
    let o = m.run("alpha", "p", &set).await;
    let (facts, sizes, listing) = kept(&o);
    let held_at = |at: &str| facts.iter().find(|f| f.as_at() == Some((out.as_str(), at))).and_then(Fact::path).map(PathBuf::from);
    assert!(held_at("alpha").unwrap().join("out.txt").exists());
    assert!(sizes[&out] > 0);
    assert_eq!(listing[&out], vec!["out.txt".to_string()]);
    // A run is measured where GNU time exists and reports nothing elsewhere; a reused result has no run.
    match (Path::new("/usr/bin/time").exists(), stats_of(&o)) {
        (true, Some(s)) => assert!(s.max_rss_kb > Some(0) && s.cpu_s.is_some() && s.wall_s.is_some(), "{s:?}"),
        (false, None) => {}
        (time, s) => panic!("time {time} but stats {s:?}"),
    }
    let placed: Vec<_> = std::fs::read_dir(&dest).unwrap().flatten().collect();
    assert!(placed.len() == 1 && placed[0].path() == held_at("beta").unwrap() && placed[0].path().join("out.txt").exists());

    // A provider that never held `out` fetches it from the path in the settled facts, with no `src`.
    let elsewhere = d.path().join("gamma-ws");
    let mut again = set_of(vec![Step { spec: json!({"path": elsewhere}), to: Some("gamma".into()), ..place }]);
    again.start = facts.clone();
    m.run("gamma", "q", &again).await;
    assert!(held_at("alpha").map(|p| elsewhere.join(p.file_name().unwrap()).join("out.txt")).unwrap().exists());

    // `files` places only the listed outputs, at their relative paths, with no wrapper directory.
    let flat_dest = d.path().join("beta-files");
    let mut flat = set.clone();
    flat.steps[1].spec = json!({"path": flat_dest, "files": true});
    let reused = m.run("alpha", "r", &flat).await;
    assert!(stats_of(&reused).is_none());
    assert!(flat_dest.join("out.txt").exists() && !flat_dest.join("skip.dat").exists());
}

#[tokio::test]
async fn a5_imports_agree_across_providers_and_rebuilds_hit() {
    let d = tempfile::tempdir().unwrap();
    let (a, b) = (Provider::local(cfg(d.path(), "a")), Provider::local(cfg(d.path(), "b")));
    let (la, lb) = (d.path().join("la"), d.path().join("lb"));
    let ia = write_library(&la, &[("db.dmnd", "2.1", "AAAA")]);
    let ib = write_library(&lb, &[("db.dmnd", "2.1", "BBBBBBBB")]);
    std::fs::File::options().write(true).open(la.join("db.dmnd")).unwrap()
        .set_modified(std::time::SystemTime::UNIX_EPOCH + Duration::from_secs(1000)).unwrap();
    let (ida, idb) = (a.import(&ia).await.unwrap(), b.import(&ib).await.unwrap());
    assert_eq!(ida, idb, "same declared set, same id; mtime and bytes never enter identity");

    let count = d.path().join("count");
    let rebuild = |ver: &str| {
        set_of(vec![task(&[("contract", "diamond-db"), ("version", ver)], &format!("echo x >> {}", count.display()), vec![])])
    };
    let mut m = Mkt::start(vec![a, b]);
    for who in ["a", "b"] {
        let bid = m.bid(who, "p", &rebuild("2.1")).await;
        assert_eq!(bid.hit, Some(Hit::Done));
        assert_eq!(bid.values.unwrap()["time"], 0.0);
    }
    m.award("b", "p", &rebuild("2.1"));
    let o = m.settle("p").await;
    assert_eq!(kept_ids(&o), ida);
    assert_eq!(kept(&o).1[&ida[0]], 8, "true size comes from the filesystem");
    assert_eq!(runs(&count), 0);
    assert_eq!(m.bid("a", "q", &rebuild("2.2")).await.hit, None, "another declared version is another thing");
}

#[tokio::test]
async fn a6_subset_hits_downstream_ids_chain_and_looser_never_answers_stricter() {
    let d = tempfile::tempdir().unwrap();
    let (ca, cb, cc) = (d.path().join("ca"), d.path().join("cb"), d.path().join("cc"));
    let echo = |c: &Path| format!("echo x >> {}; echo r > out.txt", c.display());
    let a1 = task(&[("contract", "checkm2"), ("version", "1.0.2")], &echo(&ca), vec![]);
    let b1 = task(&[("contract", "report")], &echo(&cb), vec![out_id(&a1)]);
    let (a1id, b1id) = (out_id(&a1), out_id(&b1));
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p"))]);
    let first = m.run("p", "one", &set_of(vec![a1, b1])).await;
    assert_eq!(kept_ids(&first), vec![a1id.clone(), b1id.clone()]);

    let a2 = task(&[("contract", "checkm2")], &echo(&ca), vec![]);
    let b2 = task(&[("contract", "report")], &echo(&cb), vec![out_id(&a2)]);
    assert_ne!(out_id(&a2), a1id);
    let second = set_of(vec![a2, b2]);
    assert_eq!(m.bid("p", "two", &second).await.hit, Some(Hit::Done));
    m.award("p", "two", &second);
    assert_eq!(kept_ids(&m.settle("two").await), vec![a1id, b1id], "downstream steps share ids");
    assert_eq!((runs(&ca), runs(&cb)), (1, 1));

    let loose = set_of(vec![task(&[("contract", "lint")], &echo(&cc), vec![])]);
    m.run("p", "loose", &loose).await;
    let strict = set_of(vec![task(&[("contract", "lint"), ("version", "2")], &echo(&cc), vec![])]);
    assert_eq!(m.bid("p", "strict", &strict).await.hit, None);
    m.award("p", "strict", &strict);
    m.settle("strict").await;
    assert_eq!(runs(&cc), 2);
}

#[tokio::test]
async fn a7_relaxed_offer_during_a_run_waits_for_it_instead_of_running_again() {
    let d = tempfile::tempdir().unwrap();
    let script = gate_script(d.path(), "begun", "gate", "count");
    let mut exact = task(&[("contract", "tool"), ("version", "1")], &script, vec![]);
    exact.spec["est"] = json!(5.0);
    let eid = out_id(&exact);
    let exact = set_of(vec![exact]);
    let relaxed = set_of(vec![task(&[("contract", "tool")], &script, vec![])]);
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p"))]);
    m.bid("p", "e", &exact).await;
    m.award("p", "e", &exact);
    until(&d.path().join("begun")).await;

    let b = m.bid("p", "r", &relaxed).await;
    assert_eq!(b.hit, Some(Hit::InFlight));
    let t = b.values.unwrap()["time"];
    assert!(t > 0.0 && t <= 5.0, "remaining time, not a fresh run: {t}");
    m.award("p", "r", &relaxed);
    std::fs::write(d.path().join("gate"), "").unwrap();
    let (oe, or) = (m.settle("e").await, m.settle("r").await);
    assert_eq!(kept_ids(&or), vec![eid.clone()]);
    assert_eq!(kept_ids(&oe), vec![eid]);
    assert_eq!(runs(&d.path().join("count")), 1);
}

#[tokio::test]
async fn a11_price_follows_open_bids_and_a_loss_releases_them() {
    let d = tempfile::tempdir().unwrap();
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p"))]);
    let one = |n: &str| set_of(vec![task(&[("contract", n)], "true", vec![])]);
    let t = |b: BidArgs| b.values.unwrap()["time"];
    assert_eq!(t(m.bid("p", "1", &one("a")).await), 10.0);
    assert_eq!(t(m.bid("p", "2", &one("b")).await), 20.0);
    assert_eq!(t(m.bid("p", "3", &one("c")).await), 30.0);
    let lost = |p: &str, phase| Msg::Event(PromiseStatus { promise: p.into(), phase, life: None });
    m.send("p", lost("1", Phase::Awarded { provider: "other".into() }));
    m.send("p", lost("2", Phase::Failed { name: raise::NO_BIDS.into(), message: String::new() }));
    assert_eq!(t(m.bid("p", "4", &one("d")).await), 20.0, "only bid 3 is still open");

    // With no `est`, a multi-core job quotes less on more cpus; a single-core job quotes the same.
    let sized = |n: u32| {
        let c = ProviderConfig { cpus: Some(n), ..cfg(d.path(), &format!("c{n}")) };
        Provider::local(c)
    };
    let mut m = Mkt::start(vec![sized(8), sized(16)]);
    assert_eq!((t(m.bid("c8", "5", &one("e")).await), t(m.bid("c16", "5", &one("e")).await)), (10.0, 10.0));
    for c in ["c8", "c16"] {
        m.send(c, lost("5", Phase::Awarded { provider: "other".into() }));
    }
    let mut eight = one("f");
    eight.resources.cpus = 8;
    let (on8, on16) = (t(m.bid("c8", "6", &eight).await), t(m.bid("c16", "6", &eight).await));
    assert!(on8 == 10.0 && on16 < on8, "{on8} {on16}");
}

#[tokio::test]
async fn a11_a19_declines() {
    let d = tempfile::tempdir().unwrap();
    let mut nocargo = cfg(d.path(), "nocargo");
    nocargo.cargo = Some(d.path().join("missing-cargo"));
    let mut noapp = cfg(d.path(), "noapp");
    noapp.apptainer = Some(d.path().join("missing-apptainer"));
    let small = ProviderConfig { cpus: Some(2), mem_mb: Some(1000), ..cfg(d.path(), "small") };
    let mut po = cfg(d.path(), "po");
    po.placement_only = true;
    po.routes = [("src".into(), "local".into()), ("dest".into(), "local".into())].into();
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p")), Provider::local(nocargo), Provider::local(noapp), Provider::local(po), Provider::local(small)]);

    let mut weird = task(&[], "true", vec![]);
    weird.op = "frobnicate".into();
    let b = m.bid("p", "1", &set_of(vec![weird])).await;
    assert!(b.values.is_none() && b.reason.unwrap().contains("unknown op"));

    let cargo = Step {
        op: "cargo".into(),
        props: Default::default(),
        inputs: vec![],
        slots: vec!["bin".into()],
        spec: json!({"git": "g", "rev": "r", "bin": "b"}),
        to: None,
    };
    let b = m.bid("nocargo", "2", &set_of(vec![cargo])).await;
    assert_eq!(b.reason.as_deref(), Some("no cargo"));

    let mut in_env = task(&[], "true", vec![]);
    in_env.spec = json!({"script": "true", "stage": {"e.env": {"id": "e"}}, "env": {"stage": "e.env", "container": "docker://x/y:1"}});
    let b = m.bid("noapp", "5", &set_of(vec![in_env])).await;
    assert_eq!(b.reason.as_deref(), Some("no apptainer"));

    let mut big = set_of(vec![task(&[], "true", vec![])]);
    big.resources = Resources { cpus: 8, mem_mb: 500 };
    assert_eq!(m.bid("small", "6", &big).await.reason.as_deref(), Some("needs 8 cpus, has 2"));
    big.resources = Resources { cpus: 2, mem_mb: 4096 };
    assert_eq!(m.bid("small", "7", &big).await.reason.as_deref(), Some("needs 4096 MB, has 1000 MB"));

    let b = m.bid("po", "3", &set_of(vec![task(&[], "true", vec![])])).await;
    assert!(b.values.is_none(), "placement-only declines compute");
    let place = Step {
        op: "place".into(),
        props: Default::default(),
        inputs: vec!["x".into()],
        slots: vec![],
        spec: json!({"path": "/tmp/x"}),
        to: Some("dest".into()),
    };
    let mut set = set_of(vec![place]);
    set.start = vec![Fact::at("x", "src")];
    assert!(m.bid("po", "4", &set).await.values.is_some());
}

#[tokio::test]
async fn a12_a20_a_job_outlives_its_provider_and_the_new_one_settles_it() {
    let d = tempfile::tempdir().unwrap();
    let step = task(&[("contract", "slow")], &gate_script(d.path(), "begun", "gate", "count"), vec![]);
    let (id, set) = (out_id(&step), set_of(vec![step]));
    let p1 = Provider::local(cfg(d.path(), "p"));
    let mut m = Mkt::start(vec![p1.clone()]);
    m.bid("p", "j", &set).await;
    m.award("p", "j", &set);
    until(&d.path().join("begun")).await;
    let st = m.status("p", "j").await.unwrap();
    assert!(matches!(st.phase, Phase::Started { .. }) && st.life.is_some());
    assert_eq!(m.status("p", "nope").await.unwrap_err(), raise::UNKNOWN_PROMISE);

    p1.shutdown();
    m.serving.abort();
    let p2 = Provider::local(cfg(d.path(), "p"));
    let mut m = Mkt::start(vec![p2]);
    assert!(matches!(m.status("p", "j").await.unwrap().phase, Phase::Started { .. }));
    std::fs::write(d.path().join("gate"), "").unwrap();
    let o = m.settle("j").await;
    assert_eq!(kept_ids(&o), vec![id]);
    assert!(matches!(m.status("p", "j").await.unwrap().phase, Phase::Settled { .. }));
    assert_eq!(runs(&d.path().join("count")), 1);
    assert_eq!(m.bid("p", "again", &set).await.hit, Some(Hit::Done), "instances survive a restart");
}

#[tokio::test]
async fn a20_a_job_that_finished_while_the_provider_was_down_is_recovered_from_its_exit_file() {
    let d = tempfile::tempdir().unwrap();
    let set = set_of(vec![task(&[("contract", "quick")], &gate_script(d.path(), "begun", "gate", "count"), vec![])]);
    let p1 = Provider::local(cfg(d.path(), "p"));
    let mut m = Mkt::start(vec![p1.clone()]);
    m.bid("p", "j", &set).await;
    m.award("p", "j", &set);
    until(&d.path().join("begun")).await;
    p1.shutdown();
    m.serving.abort();
    std::fs::write(d.path().join("gate"), "").unwrap();
    let exit = std::fs::read_dir(d.path().join("p/work")).unwrap().flatten().next().unwrap().path().join("exit");
    until(&exit).await;
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p"))]);
    let phase = loop {
        let phase = m.status("p", "j").await.unwrap().phase;
        if phase.is_final() {
            break phase;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    };
    assert!(matches!(phase, Phase::Settled { .. }), "reported at the next status");
    assert_eq!(runs(&d.path().join("count")), 1);
}

#[tokio::test]
async fn a14_broken_leaves_the_index_alone_and_reviews_never_touch_instances() {
    let d = tempfile::tempdir().unwrap();
    let bad = set_of(vec![task(&[("contract", "bad")], "exit 3", vec![])]);
    let unstaged = {
        let mut s = task(&[("contract", "stage")], "true", vec![]);
        s.spec["stage"] = json!({"in": {"id": "nothing-holds-this"}});
        set_of(vec![s])
    };
    let good = set_of(vec![task(&[("contract", "good")], "echo ok > out.txt", vec![])]);
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p"))]);
    for (name, set) in [("bad", &bad), ("unstaged", &unstaged)] {
        let o = m.run("p", name, set).await;
        assert!(matches!(o, Outcome::Broken { .. }), "{name}");
        assert_eq!(m.bid("p", &format!("{name}2"), set).await.hit, None);
        assert!(matches!(m.status("p", name).await.unwrap().phase, Phase::Failed { .. }));
    }
    m.run("p", "good", &good).await;
    for score in [0.5, 0.9] {
        let r = m.ask("p", Msg::Review(ReviewArgs { promise: "good".into(), scores: [("time".to_string(), score)].into() })).await;
        assert!(matches!(r, Msg::Return(Value::Null)));
    }
    assert_eq!(m.bid("p", "again", &good).await.hit, Some(Hit::Done), "a review never fails a result");
    let saved: Value = serde_json::from_slice(&std::fs::read(d.path().join("p/state.json")).unwrap()).unwrap();
    assert_eq!(saved["reviews"]["good"].as_array().unwrap().len(), 2);
}

#[tokio::test]
async fn a16_dispose_deletes_files_and_forgets_the_result() {
    let d = tempfile::tempdir().unwrap();
    let step = task(&[("contract", "tmp")], "echo hi > out.txt", vec![]);
    let (id, set) = (out_id(&step), set_of(vec![step]));
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p"))]);
    m.run("p", "j", &set).await;
    let work = std::fs::read_dir(d.path().join("p/work")).unwrap().flatten().next().unwrap().path();
    assert!(work.exists());
    let r = m.ask("p", Msg::Dispose(DisposeArgs { ids: vec![id, "unknown".into()], holder: None })).await;
    assert!(matches!(r, Msg::Return(Value::Null)));
    assert!(!work.exists());
    assert_eq!(m.bid("p", "again", &set).await.hit, None);
}

#[tokio::test]
async fn a17_one_link_hosts_two_providers() {
    let d = tempfile::tempdir().unwrap();
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "a")), Provider::local(cfg(d.path(), "b"))]);
    let mut seen = vec![];
    for _ in 0..2 {
        seen.push(m.wait(|_, msg| if let Msg::Register(r) = msg { Some(r.provider) } else { None }).await);
    }
    seen.sort();
    assert_eq!(seen, ["a", "b"]);
    let set = set_of(vec![task(&[("contract", "x")], "true", vec![])]);
    m.send("b", Msg::Offer(OfferArgs { set, promise: Some("p".into()), inbound: vec![] }));
    let who = m.wait(|_, msg| if let Msg::Bid(b) = msg { Some(b.provider) } else { None }).await;
    assert_eq!(who, "b");
}

#[tokio::test]
async fn cargo_kind_builds_through_git_and_records_the_cargo_version() {
    let d = tempfile::tempdir().unwrap();
    let sh = |dir: &Path, cmd: &str| {
        let ok = std::process::Command::new("sh").arg("-c").arg(cmd).current_dir(dir).status().unwrap().success();
        assert!(ok, "{cmd}");
    };
    let repo = d.path().join("repo");
    std::fs::create_dir(&repo).unwrap();
    sh(&repo, "git init -q && echo x > f && git add f && git -c user.name=t -c user.email=t@t commit -qm c");
    let rev = String::from_utf8(std::process::Command::new("git").args(["rev-parse", "HEAD"]).current_dir(&repo).output().unwrap().stdout).unwrap();
    let fake = d.path().join("cargo");
    std::fs::write(&fake, "#!/bin/sh\ncase \"$1\" in --version) echo 'cargo 9.9.9-fake';; build) mkdir -p target/release; echo 1234 > target/release/hello;; esac\n").unwrap();
    sh(d.path(), "chmod 755 cargo");
    let mut c = cfg(d.path(), "p");
    c.cargo = Some(fake);
    let step = |extra: &[(&str, &str)]| {
        let mut props = vec![("contract", "hello-build")];
        props.extend_from_slice(extra);
        let mut s = task(&props, "", vec![]);
        s.op = "cargo".into();
        s.slots = vec!["bin".into()];
        s.spec = json!({"git": repo, "rev": rev.trim(), "bin": "hello"});
        set_of(vec![s])
    };
    let mut m = Mkt::start(vec![Provider::local(c)]);
    let o = m.run("p", "b", &step(&[])).await;
    let id = kept_ids(&o).remove(0);
    assert_eq!(kept(&o).1[&id], 5);
    assert_eq!(m.bid("p", "same", &step(&[("cargo", "cargo 9.9.9-fake")])).await.hit, Some(Hit::Done));
    assert_eq!(m.bid("p", "other", &step(&[("cargo", "cargo 1.0.0")])).await.hit, None);
}

#[tokio::test]
async fn env_task_pulls_once_prices_the_pull_and_runs_the_protocol_inside() {
    let d = tempfile::tempdir().unwrap();
    let (log, fake) = fake_apptainer(d.path());
    let mut c = cfg(d.path(), "p");
    c.apptainer = Some(fake);
    let p = Provider::local(c);
    let ids = p.import(&write_library(&d.path().join("lib"), &[("e.env", "1", "container: docker://x/y:1\n")])).await.unwrap();
    // A material the caller's own provider holds is named by a `start` fact alone.
    let data = d.path().join("data.txt");
    std::fs::write(&data, "mine").unwrap();
    let job = |tag: &str, script: &str| {
        let mut s = task(&[("job", tag)], "", [ids.clone(), vec!["data".into()]].concat());
        s.spec = json!({"script": script, "stage": {"e.env": {"id": ids[0]}, "data.txt": {"id": "data"}}, "outputs": ["out.txt"],
                        "env": {"stage": "e.env", "container": "docker://x/y:1"}});
        let mut set = set_of(vec![s]);
        set.start = vec![Fact::at_path("data", "p", &data.to_string_lossy())];
        set
    };
    let mut m = Mkt::start(vec![p]);
    let cold = m.bid("p", "cold", &job("a", "cat data.txt > out.txt")).await.values.unwrap()["time"];
    assert_eq!(cold, 130.0, "default estimate plus the uncached pull");
    let lost = Phase::Failed { name: raise::CANCELLED.into(), message: String::new() };
    m.send("p", Msg::Event(PromiseStatus { promise: "cold".into(), phase: lost, life: None }));
    let o = m.run("p", "a", &job("a", "cat data.txt > out.txt")).await;
    let dir = kept(&o).0.iter().find_map(Fact::path).map(PathBuf::from).unwrap();
    assert_eq!(std::fs::read_to_string(dir.join("out.txt")).unwrap().trim(), "mine");
    let warm = m.bid("p", "warm", &job("b", "echo b > out.txt")).await.values.unwrap()["time"];
    assert_eq!(warm, 10.0, "the image is cached under the env material's id");
    m.run("p", "b", &job("b", "echo b > out.txt")).await;

    let log = std::fs::read_to_string(&log).unwrap();
    let pulls: Vec<_> = log.lines().filter(|l| l.starts_with("pull")).collect();
    assert!(pulls.len() == 1 && pulls[0].ends_with("docker://x/y:1"), "{log}");
    let execs: Vec<_> = log.lines().filter(|l| l.starts_with("exec")).collect();
    assert!(execs.len() == 2 && execs[1].contains(&format!("{}.sif bash -c echo b > out.txt", ids[0])), "{log}");
}

#[tokio::test]
async fn typed_outputs_are_held_by_type_and_a_prep_sees_them_in_flight_then_done() {
    let d = tempfile::tempdir().unwrap();
    let t = [("db", "uniref50"), ("release", "1")];
    let mut dl = task(&[("contract", "dl")], &format!("{}; echo db > db.dmnd", gate_script(d.path(), "begun", "gate", "count")), vec![]);
    dl.spec["typed"] = json!([{"path": "db.dmnd", "type": ty(&t)}]);
    let dl = set_of(vec![dl]);
    let (want, _) = prep_of(&[("db", "uniref50")], vec![]);
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p"))]);

    // While the download runs, a prep for its type waits on it: done at the remaining time.
    m.bid("p", "dl", &dl).await;
    m.award("p", "dl", &dl);
    until(&d.path().join("begun")).await;
    let flying = m.bid("p", "w", &want).await;
    assert_eq!(flying.hit, Some(Hit::InFlight));
    assert!(flying.values.unwrap()["time"] <= 10.0);
    std::fs::write(d.path().join("gate"), "").unwrap();
    let o = m.settle("dl").await;

    // Settled: the output is held under the type's id and a looser request hits it by subset.
    let held = instance_id(&ty(&t));
    let at = kept(&o).0.iter().find(|f| f.as_at() == Some((held.as_str(), "p"))).and_then(Fact::path).map(PathBuf::from).unwrap();
    assert_eq!(std::fs::read_to_string(at).unwrap().trim(), "db");
    let done = m.bid("p", "x", &want).await;
    assert_eq!((done.hit, done.values.unwrap()["time"]), (Some(Hit::Done), 0.0));
    let (other, _) = prep_of(&[("db", "nr")], vec![]);
    assert!(m.bid("p", "y", &other).await.values.is_none(), "an unheld type with no source is declined");
    assert_eq!(runs(&d.path().join("count")), 1);

    // A producer that makes no file at the typed path breaks, and leaves no type behind.
    let mut bad = task(&[("contract", "bad")], "echo x > out.txt", vec![]);
    bad.spec["typed"] = json!([{"path": "nope", "type": ty(&[("db", "never")])}]);
    assert!(matches!(m.run("p", "bad", &set_of(vec![bad])).await, Outcome::Broken { .. }));
    assert!(m.bid("p", "z", &prep_of(&[("db", "never")], vec![]).0).await.values.is_none());
}

#[tokio::test]
async fn prep_stages_and_pulls_the_image_without_running_anything() {
    let d = tempfile::tempdir().unwrap();
    let (log, fake) = fake_apptainer(d.path());
    let mut c2 = cfg(d.path(), "p2");
    c2.routes = [("p1".into(), "local".into())].into();
    c2.apptainer = Some(fake);
    let (p1, p2) = (Provider::local(cfg(d.path(), "p1")), Provider::local(c2));
    let mut m = Mkt::start(vec![p1, p2]);

    // `import` registers what p1 already holds: by type, or by identity; a missing path is an error.
    let db = d.path().join("u.dmnd");
    std::fs::write(&db, "DB").unwrap();
    let import = |items: Value| Msg::Import(serde_json::from_value(json!({"to": "p1", "materials": items})).unwrap());
    let Msg::Return(r) = m.ask("p1", import(json!([{"path": db, "type": {"db": "uniref50", "release": "1"}}, {"path": d.path()}]))).await else { panic!("imports") };
    let plain = crate::material_id(&d.path().canonicalize().unwrap(), "p1", &mut vec![]).unwrap();
    assert_eq!(r["ids"], json!([instance_id(&ty(&[("db", "uniref50"), ("release", "1")])), plain]));
    let facts: Vec<Fact> = serde_json::from_value(r["facts"].clone()).unwrap();
    assert_eq!(facts[0].path(), Some(db.to_str().unwrap()));
    for bad in [json!([{"path": d.path().join("absent")}, {"path": db}]), json!([{"path": "rel"}])] {
        assert!(matches!(m.ask("p1", import(bad)).await, Msg::Raise(r) if r.name == raise::BAD_ARGS));
    }

    // The holder answers a looser type by subset with nothing to do.
    let (want, id) = prep_of(&[("db", "uniref50")], vec![]);
    assert_eq!(m.bid("p1", "h", &want).await.hit, Some(Hit::Done));

    // p2 stages it from p1 and pulls the env image, running no protocol.
    let (mut set, id2) = prep_of(&[("db", "uniref50"), ("release", "1")], vec![Fact::at_path(&instance_id(&ty(&[("db", "uniref50"), ("release", "1")])), "p1", &db.to_string_lossy())]);
    assert_ne!(id, id2);
    let env = d.path().join("e.env");
    std::fs::write(&env, "container: docker://x/y:1\n").unwrap();
    set.steps[0].spec["stage"]["e.env"] = json!({"id": "env1"});
    set.steps[0].spec["env"] = json!({"stage": "e.env", "container": "docker://x/y:1"});
    set.steps[0].inputs.push("env1".into());
    set.start.push(Fact::at_path("env1", "p1", &env.to_string_lossy()));
    assert_eq!(m.bid("p2", "cold", &set).await.hit, None);
    let o = m.run("p2", "stage", &set).await;
    let staged = kept(&o).0.iter().find_map(|f| f.path().filter(|p| p.ends_with("u.dmnd"))).map(PathBuf::from).unwrap();
    assert!(staged.starts_with(d.path().join("p2/held")) && std::fs::read_to_string(&staged).unwrap() == "DB");
    let log = std::fs::read_to_string(&log).unwrap();
    assert!(log.lines().count() == 1 && log.starts_with("pull"), "one pull, no exec: {log}");
    assert_eq!(m.bid("p2", "warm", &set).await.hit, Some(Hit::Done));
}

#[tokio::test]
async fn a_second_prep_of_a_typed_material_waits_on_the_transfer_in_flight() {
    let d = tempfile::tempdir().unwrap();
    let (log, fake) = fake_apptainer(d.path());
    let mut c2 = cfg(d.path(), "p2");
    c2.routes = [("p1".into(), "local".into())].into();
    c2.apptainer = Some(fake);
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p1")), Provider::local(c2)]);

    // The first prep copies the material, then stalls pulling the image: the transfer is in flight.
    let db = d.path().join("u.dmnd");
    std::fs::write(&db, "DB").unwrap();
    let t = [("db", "uniref50"), ("release", "1")];
    let (mut first, id) = prep_of(&t, vec![Fact::at_path(&instance_id(&ty(&t)), "p1", &db.to_string_lossy())]);
    first.steps[0].spec["stage"]["e.env"] = json!({"id": "env1"});
    first.steps[0].spec["env"] = json!({"stage": "e.env", "container": "docker://x/y:1"});
    first.steps[0].inputs.push("env1".into());
    first.start.push(Fact::at_path("env1", "p1", &db.to_string_lossy()));
    std::fs::write(d.path().join("hold"), "").unwrap();
    m.bid("p2", "one", &first).await;
    m.award("p2", "one", &first);
    until(&log).await;

    // A second prep of the same type, with no source to fetch from, bids done at T and starts nothing.
    let (second, _) = prep_of(&t, vec![]);
    let bid = m.bid("p2", "two", &second).await;
    assert_eq!(bid.hit, Some(Hit::InFlight));
    m.award("p2", "two", &second);
    std::fs::remove_file(d.path().join("hold")).unwrap();
    let mut settled = std::collections::BTreeMap::new();
    while settled.len() < 2 {
        let (p, o) = m
            .wait(|_, msg| match msg {
                Msg::Settle(s) => Some((s.promise, s.outcome)),
                _ => None,
            })
            .await;
        settled.insert(p, o);
    }
    assert!(kept_ids(&settled["one"]).contains(&id) && kept_ids(&settled["two"]) == [id]);
    let work = d.path().join("p2/work");
    assert!(work.join("one-0").exists() && !work.join("two-0").exists(), "the second prep ran no transfer");
    assert_eq!(std::fs::read_to_string(&log).unwrap().lines().count(), 1, "one image pull");
}
