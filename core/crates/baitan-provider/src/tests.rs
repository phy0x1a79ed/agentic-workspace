//! Provider behaviour over a real `serve` loop with scripted jobs: no network, no real builds.

use crate::{serve, Provider, ProviderConfig};
use baitan_carrier::Link;
use baitan_proto::*;
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
        self.send(to, Msg::Offer(OfferArgs { set: set.clone(), promise: Some(promise.into()) }));
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
        Outcome::Kept { facts, sizes, listing } => (facts, sizes, listing),
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
    let ids = p1.import(&write_library(&d.path().join("lib"), &[("data.txt", "1", "hello")])).unwrap();

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
    let placed: Vec<_> = std::fs::read_dir(&dest).unwrap().flatten().collect();
    assert!(placed.len() == 1 && placed[0].path() == held_at("beta").unwrap() && placed[0].path().join("out.txt").exists());

    // A provider that never held `out` fetches it from the path in the settled facts, with no `src`.
    let elsewhere = d.path().join("gamma-ws");
    let mut again = set_of(vec![Step { spec: json!({"path": elsewhere}), to: Some("gamma".into()), ..place }]);
    again.start = facts.clone();
    m.run("gamma", "q", &again).await;
    assert!(held_at("alpha").map(|p| elsewhere.join(p.file_name().unwrap()).join("out.txt")).unwrap().exists());
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
    let (ida, idb) = (a.import(&ia).unwrap(), b.import(&ib).unwrap());
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
}

#[tokio::test]
async fn a11_a19_declines() {
    let d = tempfile::tempdir().unwrap();
    let mut nocargo = cfg(d.path(), "nocargo");
    nocargo.cargo = Some(d.path().join("missing-cargo"));
    let mut po = cfg(d.path(), "po");
    po.placement_only = true;
    po.routes = [("src".into(), "local".into()), ("dest".into(), "local".into())].into();
    let mut m = Mkt::start(vec![Provider::local(cfg(d.path(), "p")), Provider::local(nocargo), Provider::local(po)]);

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
    m.send("b", Msg::Offer(OfferArgs { set, promise: Some("p".into()) }));
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
