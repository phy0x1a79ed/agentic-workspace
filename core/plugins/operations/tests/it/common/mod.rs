//! Shared fakes for the operations tests: a settable clock, alcor's store and approver in a
//! temp dir, a link to them that can drop, and recording fakes of the front's and Arbutus's
//! ways out. Every id here is synthetic.
#![allow(dead_code)]

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use awm_plugin_operations::approver::{Approver, Config, Tools};
use awm_plugin_operations::arbutus::ArbutusIo;
use awm_plugin_operations::front::{Outside, Reach};
use awm_plugin_operations::home::Home;
use awm_plugin_operations::keys::Keys;
use awm_plugin_operations::relay::async_trait_free::BoxFut;
use awm_plugin_operations::relay::{ClientError, StoreClient};
use awm_plugin_operations::store::{Clock, Store};
use serde_json::{json, Value};

pub const ORIGIN: &str = "https://auth.example.test";
pub const A: &str = "ccaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
pub const B: &str = "ccbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
pub const ME: &str = "cc11111111111111111111111111111111";

/// A clock a test moves by hand.
#[derive(Clone)]
pub struct TestClock(pub Arc<Mutex<f64>>);

impl TestClock {
    pub fn at(t: f64) -> TestClock {
        TestClock(Arc::new(Mutex::new(t)))
    }

    pub fn now() -> TestClock {
        TestClock::at(awm_plugin_operations::now().floor())
    }

    pub fn clock(&self) -> Clock {
        let c = self.0.clone();
        Arc::new(move || *c.lock().unwrap())
    }

    pub fn set(&self, t: f64) {
        *self.0.lock().unwrap() = t;
    }

    pub fn advance(&self, d: f64) {
        *self.0.lock().unwrap() += d;
    }

    pub fn get(&self) -> f64 {
        *self.0.lock().unwrap()
    }
}

/// The approver's 2fa calls, answered from fixed data.
#[derive(Default)]
pub struct FakeTools {
    pub calls: Mutex<Vec<(String, Value)>>,
}

impl Tools for FakeTools {
    fn twofa<'a>(&'a self, verb: &'a str, args: Value) -> BoxFut<'a, Result<Value, String>> {
        Box::pin(async move {
            self.calls.lock().unwrap().push((verb.to_string(), args));
            Ok(match verb {
                "devices" => json!([{"name": "phone"}]),
                "pending" => json!({"phone": [{"urgid": "u1", "app": "Example"}]}),
                _ => json!({"ok": true}),
            })
        })
    }
}

/// alcor's store, approver and home verbs in one temp dir.
pub struct Env {
    pub dir: tempfile::TempDir,
    pub store: Arc<Store>,
    pub ap: Arc<Approver>,
    pub home: Arc<Home>,
}

impl Env {
    pub fn new() -> Env {
        Env::with_clock(Arc::new(awm_plugin_operations::now))
    }

    pub fn with_clock(clock: Clock) -> Env {
        let dir = tempfile::tempdir().unwrap();
        let store = Arc::new(Store::open_with_clock(dir.path().join("queue.db"), true, clock.clone()).unwrap());
        let ap = Arc::new(approver(dir.path(), store.clone(), clock));
        let home = Arc::new(Home { store: store.clone(), approver: ap.clone() });
        Env { dir, store, ap, home }
    }

    pub fn path(&self, name: &str) -> PathBuf {
        self.dir.path().join(name)
    }
}

pub fn approver(dir: &Path, store: Arc<Store>, clock: Clock) -> Approver {
    let keys = Keys::generate(&dir.join("keys")).unwrap();
    let cfg = Config {
        origin: ORIGIN.into(),
        allowed_orders: HashMap::from([("agent".to_string(), vec!["*".to_string()]), ("service".to_string(), vec!["*".to_string()])]),
        rate_max: 30,
        portals: HashMap::new(),
    };
    Approver::open(&dir.join("approver.db"), keys, cfg, store, Arc::new(FakeTools::default()), true, clock).unwrap()
}

/// alcor's home verbs behind a link that can drop, and that can lose a reply after applying.
pub struct Flaky {
    pub home: Arc<Home>,
    pub node: String,
    pub up: AtomicBool,
    pub lose_reply: AtomicBool,
    pub calls: Mutex<Vec<(String, Option<String>)>>,
}

impl Flaky {
    pub fn new(home: Arc<Home>, node: &str) -> Arc<Flaky> {
        Arc::new(Flaky { home, node: node.into(), up: AtomicBool::new(true), lose_reply: AtomicBool::new(false), calls: Mutex::new(vec![]) })
    }

    pub fn set_up(&self, up: bool) {
        self.up.store(up, Ordering::SeqCst);
    }

    pub fn verbs(&self) -> Vec<String> {
        self.calls.lock().unwrap().iter().map(|c| c.0.clone()).collect()
    }

    pub fn ops(&self) -> Vec<Option<String>> {
        self.calls.lock().unwrap().iter().map(|c| c.1.clone()).collect()
    }
}

impl StoreClient for Flaky {
    fn call<'a>(&'a self, verb: &'a str, args: Value) -> BoxFut<'a, Result<Value, ClientError>> {
        Box::pin(async move {
            if !self.up.load(Ordering::SeqCst) {
                return Err(ClientError::Unreachable("down".into()));
            }
            let op = args.get("op_id").and_then(Value::as_str).map(str::to_string);
            self.calls.lock().unwrap().push((verb.to_string(), op));
            let res = self.home.call(verb, args, Some(&self.node)).await.map_err(ClientError::Refused)?;
            if self.lose_reply.swap(false, Ordering::SeqCst) {
                return Err(ClientError::Unreachable("reply lost".into()));
            }
            Ok(res)
        })
    }
}

/// The front's ways out: a pid table, and every line told and DM sent.
pub struct FakeOutside {
    pub pids: Mutex<HashMap<u32, Value>>,
    pub told: Mutex<Vec<(Option<String>, String, String)>>,
    pub dms: Mutex<Vec<String>>,
    pub asks: Mutex<Vec<String>>,
    pub ask_fails: Mutex<bool>,
    pub reach: Mutex<HashMap<String, Reach>>,
    pub reaches: Mutex<Vec<String>>,
    pub roster: Mutex<HashMap<String, HashMap<String, Option<String>>>>,
}

impl FakeOutside {
    /// pid 11 is session A (job-a, fix-tunnel); pid 22 is session B (job-b, other).
    pub fn new() -> Arc<FakeOutside> {
        let pids = HashMap::from([
            (11, json!({"ok": true, "session_id": A, "job": "job-a", "name": "fix-tunnel"})),
            (22, json!({"ok": true, "session_id": B, "job": "job-b", "name": "other"})),
        ]);
        Arc::new(FakeOutside { pids: Mutex::new(pids), told: Mutex::new(vec![]), dms: Mutex::new(vec![]), asks: Mutex::new(vec![]), ask_fails: Mutex::new(false), reach: Mutex::default(), reaches: Mutex::default(), roster: Mutex::default() })
    }

    pub fn dms(&self) -> Vec<String> {
        self.dms.lock().unwrap().clone()
    }

    pub fn asks(&self) -> Vec<String> {
        self.asks.lock().unwrap().clone()
    }

    pub fn told(&self) -> Vec<(Option<String>, String, String)> {
        self.told.lock().unwrap().clone()
    }
}

impl Outside for FakeOutside {
    fn session_of(&self, pid: u32) -> BoxFut<'_, Option<Value>> {
        let rec = self.pids.lock().unwrap().get(&pid).cloned();
        Box::pin(async move { rec })
    }

    fn tell<'a>(&'a self, peer: Option<&'a str>, session: &'a str, text: &'a str) -> BoxFut<'a, bool> {
        Box::pin(async move {
            self.told.lock().unwrap().push((peer.map(str::to_string), session.to_string(), text.to_string()));
            true
        })
    }

    fn dm<'a>(&'a self, text: &'a str) -> BoxFut<'a, bool> {
        Box::pin(async move {
            self.dms.lock().unwrap().push(text.to_string());
            true
        })
    }

    fn dm_tony<'a>(&'a self, text: &'a str) -> BoxFut<'a, bool> {
        Box::pin(async move {
            self.asks.lock().unwrap().push(text.to_string());
            !*self.ask_fails.lock().unwrap()
        })
    }

    fn reach<'a>(&'a self, node: &'a str, session: &'a str) -> BoxFut<'a, Reach> {
        Box::pin(async move {
            self.reaches.lock().unwrap().push(format!("{node}/{session}"));
            self.reach.lock().unwrap().get(session).cloned().unwrap_or(Reach::Link(format!("https://claude.ai/code/{node}-{session}")))
        })
    }

    fn links<'a>(&'a self, node: &'a str) -> BoxFut<'a, Option<HashMap<String, Option<String>>>> {
        Box::pin(async move { self.roster.lock().unwrap().get(node).cloned() })
    }
}

/// Arbutus's ways out: each Slack post, approve-script run and session line.
pub struct FakeArbutus {
    pub rc: i32,
    pub posts: Mutex<Vec<(String, String, String, String)>>,
    pub quiet: Mutex<Vec<bool>>,
    pub runs: Mutex<Vec<(String, PathBuf, String)>>,
    pub lines: Mutex<Vec<(String, String)>>,
}

impl FakeArbutus {
    pub fn new(rc: i32) -> Arc<FakeArbutus> {
        Arc::new(FakeArbutus { rc, posts: Mutex::new(vec![]), quiet: Mutex::new(vec![]), runs: Mutex::new(vec![]), lines: Mutex::new(vec![]) })
    }

    pub fn posts(&self) -> Vec<(String, String, String, String)> {
        self.posts.lock().unwrap().clone()
    }

    pub fn runs(&self) -> Vec<(String, PathBuf, String)> {
        self.runs.lock().unwrap().clone()
    }

    pub fn lines(&self) -> Vec<(String, String)> {
        self.lines.lock().unwrap().clone()
    }
}

impl ArbutusIo for FakeArbutus {
    fn post<'a>(&'a self, account: &'a str, channel: &'a str, text: &'a str, thread: Option<&'a str>, quiet: bool) -> BoxFut<'a, Result<Value, String>> {
        Box::pin(async move {
            self.posts.lock().unwrap().push((account.into(), channel.into(), text.into(), thread.unwrap_or_default().into()));
            self.quiet.lock().unwrap().push(quiet);
            Ok(json!({"ts": "100.1", "channel_id": channel}))
        })
    }

    fn run<'a>(&'a self, name: &'a str, bundles: &'a Path, approved_by: &'a str) -> BoxFut<'a, (i32, String)> {
        Box::pin(async move {
            self.runs.lock().unwrap().push((name.into(), bundles.to_path_buf(), approved_by.into()));
            (self.rc, "Approved grow for 60 minutes".into())
        })
    }

    fn tell<'a>(&'a self, session: &'a str, line: &'a str) -> BoxFut<'a, Result<(), String>> {
        Box::pin(async move {
            self.lines.lock().unwrap().push((session.into(), line.into()));
            Ok(())
        })
    }
}

/// The number of rows in one table of the store's database, read on a second connection.
pub fn count(store: &Store, table: &str) -> i64 {
    let db = rusqlite::Connection::open(store.path()).unwrap();
    db.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0)).unwrap()
}

pub fn s(v: &Value) -> String {
    v.as_str().unwrap_or_else(|| panic!("not a string: {v}")).to_string()
}
