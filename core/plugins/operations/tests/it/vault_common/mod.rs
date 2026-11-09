//! The approver fixture the approver and vault server tests share: a clock the test moves, a fake
//! 2fa that records its calls, and a fresh key set, store and approver.db in a tempdir.
#![allow(dead_code)]

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use awm_plugin_operations::approver::{Approver, Config, Tools};
use awm_plugin_operations::keys::Keys;
use awm_plugin_operations::passkeys::soft::SoftAuthenticator;
use awm_plugin_operations::relay::async_trait_free::BoxFut;
use awm_plugin_operations::store::Store;
use awm_trust::grant::load_public_key;
use serde_json::{json, Value};

pub const ORIGIN: &str = "https://auth.example.test";
pub const RP_ID: &str = "auth.example.test";
pub const PORTAL: &str = "portal.example.test";
pub const PORTAL_NODE: &str = "mizar";
pub const AGENT_NODE: &str = "alcor";

/// Seconds since the epoch, moved only by the test.
#[derive(Clone)]
pub struct Clock(Arc<Mutex<f64>>);

impl Clock {
    pub fn new() -> Clock {
        Clock(Arc::new(Mutex::new(awm_plugin_operations::now().floor())))
    }

    pub fn now(&self) -> f64 {
        *self.0.lock().unwrap()
    }

    pub fn advance(&self, seconds: f64) {
        *self.0.lock().unwrap() += seconds;
    }

    pub fn as_fn(&self) -> awm_plugin_operations::store::Clock {
        let c = self.clone();
        Arc::new(move || c.now())
    }
}

#[derive(Default)]
pub struct FakeTools {
    calls: Mutex<Vec<(String, Value)>>,
}

impl FakeTools {
    pub fn calls(&self) -> Vec<(String, Value)> {
        self.calls.lock().unwrap().clone()
    }
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

pub struct Env {
    pub dir: tempfile::TempDir,
    pub ap: Arc<Approver>,
    pub store: Arc<Store>,
    pub tools: Arc<FakeTools>,
    pub clock: Clock,
}

pub fn config(rate_max: i64) -> Config {
    Config {
        origin: ORIGIN.into(),
        allowed_orders: HashMap::from([("agent".to_string(), vec![AGENT_NODE.to_string()]), ("service".to_string(), vec!["*".to_string()])]),
        rate_max,
        portals: HashMap::from([(PORTAL.to_string(), PORTAL_NODE.to_string())]),
    }
}

pub fn env() -> Env {
    env_with(5)
}

pub fn env_with(rate_max: i64) -> Env {
    let dir = tempfile::tempdir().unwrap();
    let keys = Keys::generate(&dir.path().join("keys")).unwrap();
    let store = Arc::new(Store::open(dir.path().join("queue.db"), true).unwrap());
    let tools = Arc::new(FakeTools::default());
    let clock = Clock::new();
    let ap = Approver::open(&dir.path().join("approver.db"), keys, config(rate_max), store.clone(), tools.clone(), true, clock.as_fn()).unwrap();
    Env { dir, ap: Arc::new(ap), store, tools, clock }
}

impl Env {
    pub fn now(&self) -> i64 {
        self.clock.now() as i64
    }

    pub fn grant_pub(&self) -> [u8; 32] {
        load_public_key(&self.ap.keys.grant_pub_b64u()).unwrap()
    }

    /// Enrol a device through a fresh link: its id and its passkey.
    pub fn enrol(&self, name: &str) -> (String, SoftAuthenticator) {
        let token = self.ap.enrol_link(15, None).unwrap()["token"].as_str().unwrap().to_string();
        self.enrol_with(&token, name)
    }

    pub fn enrol_with(&self, token: &str, name: &str) -> (String, SoftAuthenticator) {
        let mut pk = SoftAuthenticator::new(ORIGIN, RP_ID);
        let begin = self.ap.enrol_begin(token, name).unwrap();
        let credential = pk.register(&begin["options"]);
        let out = self.ap.enrol_finish(token, begin["reg_id"].as_str().unwrap(), &credential, name, None).unwrap();
        (out["device"].as_str().unwrap().to_string(), pk)
    }

    /// The passkey's assertion for one request's approval.
    pub fn approval_assertion(&self, pk: &mut SoftAuthenticator, request_id: &str) -> Value {
        pk.assert(&self.ap.approve_options(request_id).unwrap())
    }

    /// A request with no payload rules: `vault_hold` on a node.
    pub async fn hold(&self, session: Option<&str>) -> Value {
        self.ap.approval_request("vault_hold", Some("deneb"), "checks tripped", 30, json!({}), session, None).await.unwrap()
    }
}

pub fn s(v: &Value) -> &str {
    v.as_str().unwrap_or_else(|| panic!("not a string: {v}"))
}
