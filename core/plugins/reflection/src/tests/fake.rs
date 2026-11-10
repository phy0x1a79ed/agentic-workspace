//! Fake harness providers, a fake gateway around them, a fake node and a fake trust policy: the
//! Python test fixtures, one to one.

use std::collections::HashMap;
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, Weak};

use serde_json::{json, Value};

use crate::app::App;
use crate::caller::Frame;
use crate::gateway::{Call, CallError, Gateway, Node};
use crate::store::Store;
use crate::trust::{Policy, TrustLib};

pub const ME: u32 = 4242;
pub const NEW_PID: u32 = 5151;

pub fn my_id() -> String {
    format!("cc{}", "0e38".repeat(8))
}

pub fn new_id() -> String {
    format!("cc{}", "1".repeat(32))
}

pub fn other() -> String {
    format!("cc{}", "2".repeat(32))
}

pub fn third() -> String {
    format!("cc{}", "3".repeat(32))
}

/// The session `j2` that is not the caller.
pub fn peer_row() -> Value {
    json!({"session_id": other(), "name": "other", "pid": 77, "job": "j2"})
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap()
}

/// One harness provider answering the contract verbs from a table of sessions. It records every
/// call, so a test reads exactly what reflection asked of whom.
pub struct Fake {
    pub tool: &'static str,
    pub sessions: Mutex<Vec<(String, Value)>>,
    pub calls: Mutex<Vec<(String, Value)>>,
    pub down: AtomicBool,
    pub answers: Mutex<HashMap<String, Value>>,
}

impl Fake {
    pub fn new(tool: &'static str, sessions: Vec<(&str, Value)>) -> Self {
        Self {
            tool,
            sessions: Mutex::new(sessions.into_iter().map(|(k, v)| (k.to_string(), v)).collect()),
            calls: Mutex::default(),
            down: AtomicBool::new(false),
            answers: Mutex::default(),
        }
    }

    pub fn set(&self, reference: &str, row: Value) {
        let mut s = lock(&self.sessions);
        match s.iter_mut().find(|(k, _)| k == reference) {
            Some((_, v)) => *v = row,
            None => s.push((reference.into(), row)),
        }
    }

    pub fn row(&self, reference: &str) -> Option<Value> {
        lock(&self.sessions).iter().find(|(k, _)| k == reference).map(|(_, v)| v.clone())
    }

    pub fn clear(&self) {
        lock(&self.sessions).clear();
    }

    pub fn answer(&self, verb: &str, out: Value) {
        lock(&self.answers).insert(verb.into(), out);
    }

    pub fn verbs(&self) -> Vec<String> {
        lock(&self.calls).iter().map(|(v, _)| v.clone()).collect()
    }

    pub fn last(&self) -> (String, Value) {
        lock(&self.calls).last().cloned().expect("a call")
    }

    pub fn calls_of(&self, verb: &str) -> Vec<Value> {
        lock(&self.calls).iter().filter(|(v, _)| v == verb).map(|(_, a)| a.clone()).collect()
    }

    fn handle(&self, verb: &str, args: &Value) -> Result<Value, CallError> {
        lock(&self.calls).push((verb.into(), args.clone()));
        if self.down.load(Ordering::SeqCst) {
            return Err(CallError::Down("connection refused".into()));
        }
        if let Some(out) = lock(&self.answers).get(verb) {
            return Ok(out.clone());
        }
        let rows: Vec<Value> = lock(&self.sessions).iter().map(|(_, v)| v.clone()).collect();
        Ok(match verb {
            "resolve" => match rows.iter().find(|r| r.get("pid") == args.get("pid")) {
                Some(row) => crate::app::merged(&json!({"ok": true, "mine": true}), row),
                None => json!({"ok": true, "mine": false}),
            },
            "find" => {
                let name = args["name"].as_str().unwrap_or_default().to_lowercase();
                let hits: Vec<&Value> = rows.iter().filter(|r| r["name"].as_str().is_some_and(|n| n.to_lowercase() == name)).collect();
                json!({"ok": true, "sessions": hits})
            }
            "list" => json!({"ok": true, "sessions": rows}),
            "pending" => json!({"ok": true, "pending": []}),
            "send" | "compact" => {
                let mut out = json!({"ok": true, "text": args.get("text").cloned().unwrap_or(json!("/compact")),
                                     "submitted": true, "followup": args.get("followup")});
                if args.get("followup").is_some_and(|f| crate::app::truthy(Some(f))) {
                    out["followup_deferred"] = json!(true);
                    out["ticket"] = json!("t-1");
                }
                out
            }
            "start" => {
                let row = json!({"session_id": new_id(), "name": "new", "pid": NEW_PID, "job": "abcd1234"});
                self.set("abcd1234", row.clone());
                json!({"ok": true, "job": row["job"], "session_id": row["session_id"]})
            }
            "status" => {
                let reference = args.get("session_id").and_then(Value::as_str);
                let row = match reference {
                    Some(r) => rows.iter().find(|x| x.get("session_id").and_then(Value::as_str) == Some(r)).cloned(),
                    None => rows.iter().find(|x| x.get("pid") == args.get("pid")).cloned(),
                };
                match row {
                    None => {
                        let shown = reference.map(str::to_string).unwrap_or_else(|| args.get("pid").map_or("None".into(), |p| p.to_string()));
                        json!({"ok": false, "reason": format!("no session {shown}")})
                    }
                    Some(row) => {
                        let mut out = crate::app::merged(&json!({"ok": true, "kind": "bg"}), &row);
                        out["cwd"] = json!("/w/projects/awm/old");
                        out
                    }
                }
            }
            _ => json!({"ok": true, "verb": verb}),
        })
    }
}

/// The scope claim verbs over one table, holding each scope for one session (scopes.md R42).
#[derive(Default)]
pub struct FakeClaims {
    pub held: Vec<((String, String), Value)>,
    pub calls: Vec<(String, Value)>,
    /// `claims` lists nothing, as when a claim lands between the check and the claim.
    pub unlisted: bool,
}

impl FakeClaims {
    pub fn hold(&mut self, project: &str, scope: &str, holder: Value) {
        let key = (project.to_string(), scope.to_string());
        self.held.retain(|(k, _)| *k != key);
        self.held.push((key, holder));
    }

    pub fn holder(&self, project: &str, scope: &str) -> Option<Value> {
        self.held.iter().find(|(k, _)| k.0 == project && k.1 == scope).map(|(_, h)| h.clone())
    }

    fn handle(&mut self, verb: &str, a: &Value) -> Value {
        self.calls.push((verb.into(), a.clone()));
        let s = |k: &str| a.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
        let (project, scope) = (s("project"), s("scope"));
        match verb {
            "claim" => {
                if let Some(holder) = self.holder(&project, &scope) {
                    if holder["session_id"] != a["session_id"] {
                        let why = format!("session {} holds {project}/{scope}", holder["session_id"].as_str().unwrap_or_default());
                        return json!({"ok": false, "claimed": false, "holder": holder, "reason": why});
                    }
                }
                let holder = json!({"session_id": a["session_id"], "pid": a.get("pid"), "job": a.get("job")});
                self.hold(&project, &scope, holder.clone());
                json!({"ok": true, "claimed": true, "holder": holder})
            }
            "release" => {
                if self.holder(&project, &scope).is_some_and(|h| h["session_id"] == a["session_id"]) {
                    self.held.retain(|(k, _)| !(k.0 == project && k.1 == scope));
                }
                json!({"ok": true})
            }
            "claims" if self.unlisted => json!({"claims": []}),
            "claims" => {
                let want = |k: &str, v: &str| a.get(k).and_then(Value::as_str).is_none_or(|w| w == v);
                let rows: Vec<Value> = self
                    .held
                    .iter()
                    .filter(|((p, sc), _)| want("project", p) && want("scope", sc))
                    .map(|((p, sc), h)| json!({"project": p, "scope": sc, "holder": h}))
                    .collect();
                json!({"claims": rows})
            }
            _ => Value::Null,
        }
    }
}

pub type Hook = Arc<dyn Fn(Value) -> Pin<Box<dyn Future<Output = Value> + Send>> + Send + Sync>;

/// The gateway: the two providers, scope, journal, operations and reflection on the peers.
pub struct FakeGateway {
    pub cc: Fake,
    pub ds: Fake,
    /// Further providers by tool, for a contract table that names them.
    pub more: Mutex<Vec<Arc<Fake>>>,
    /// The contract table, `None` for the two providers.
    pub rows: Mutex<Option<Result<Vec<Value>, String>>>,
    pub codes: Mutex<HashMap<String, String>>,
    pub capabilities: Mutex<HashMap<String, Vec<String>>>,
    pub table_reads: AtomicUsize,
    /// Every call but `platform`, providers included.
    pub sent: Mutex<Vec<Call>>,
    /// Every call that is not to a provider.
    pub others: Mutex<Vec<Call>>,
    /// One answer for every scope call, ahead of the claims table.
    pub scope_fixed: Mutex<Option<Value>>,
    /// The scope claims; None answers every scope call as not installed.
    pub claims: Mutex<Option<FakeClaims>>,
    /// Run inside a scope `claim`, before it answers, with the claim's args.
    pub on_claim: Mutex<Option<Hook>>,
    pub brief: Mutex<Value>,
    /// operations answers every call with a transport fault.
    pub operations_down: AtomicBool,
}

impl FakeGateway {
    pub fn new() -> Self {
        let me = json!({"session_id": my_id(), "name": "me", "pid": ME, "job": "j1", "cwd": "/w/projects/awm/redux-harness"});
        let codes = [("harness-cc", "cc"), ("harness-ds", "ds")].into_iter().map(|(t, c)| (t.to_string(), c.to_string())).collect();
        Self {
            cc: Fake::new("harness-cc", vec![("j1", me)]),
            ds: Fake::new("harness-ds", vec![]),
            more: Mutex::default(),
            rows: Mutex::default(),
            codes: Mutex::new(codes),
            capabilities: Mutex::default(),
            table_reads: AtomicUsize::new(0),
            sent: Mutex::default(),
            others: Mutex::default(),
            scope_fixed: Mutex::default(),
            claims: Mutex::default(),
            on_claim: Mutex::default(),
            brief: Mutex::new(json!({"brief": null})),
            operations_down: AtomicBool::new(false),
        }
    }

    /// The non-provider calls to `tool`, as `(verb, args)`.
    pub fn calls_to(&self, tool: &str) -> Vec<(String, Value)> {
        lock(&self.others).iter().filter(|c| c.tool == tool).map(|c| (c.verb.clone(), c.args.clone())).collect()
    }

    /// The calls to reflection on a peer, as `(peer, verb, args)`.
    pub fn peer_calls(&self) -> Vec<(String, String, Value)> {
        lock(&self.others)
            .iter()
            .filter(|c| c.tool == "reflection")
            .map(|c| (c.peer.clone().unwrap_or_default(), c.verb.clone(), c.args.clone()))
            .collect()
    }

    pub fn use_claims(&self) {
        *lock(&self.claims) = Some(FakeClaims::default());
    }

    pub fn with_claims<T>(&self, f: impl FnOnce(&mut FakeClaims) -> T) -> T {
        f(lock(&self.claims).as_mut().expect("use_claims first"))
    }

    fn provider(&self, tool: &str) -> Option<Arc<Fake>> {
        lock(&self.more).iter().find(|f| f.tool == tool).cloned()
    }
}

impl Gateway for FakeGateway {
    async fn contract_rows(&self) -> Result<Vec<Value>, String> {
        self.table_reads.fetch_add(1, Ordering::SeqCst);
        lock(&self.rows).clone().unwrap_or_else(|| Ok(vec![json!({"name": "harness-cc", "major": 1}), json!({"name": "harness-ds", "major": 1})]))
    }

    async fn call(&self, call: Call) -> Result<Value, CallError> {
        let code = lock(&self.codes).get(&call.tool).cloned();
        if let Some(code) = code {
            if call.verb == "platform" {
                let caps = lock(&self.capabilities).get(&call.tool).cloned().unwrap_or_default();
                return Ok(json!({"ok": true, "code": code, "capabilities": caps}));
            }
            lock(&self.sent).push(call.clone());
            return match call.tool.as_str() {
                "harness-cc" => self.cc.handle(&call.verb, &call.args),
                "harness-ds" => self.ds.handle(&call.verb, &call.args),
                other => match self.provider(other) {
                    Some(f) => f.handle(&call.verb, &call.args),
                    None => Ok(json!({})),
                },
            };
        }
        lock(&self.sent).push(call.clone());
        lock(&self.others).push(call.clone());
        match call.tool.as_str() {
            "scope" => {
                let hook = lock(&self.on_claim).clone();
                if let (Some(hook), "claim") = (hook, call.verb.as_str()) {
                    hook(call.args.clone()).await;
                }
                if let Some(fixed) = lock(&self.scope_fixed).clone() {
                    return Ok(fixed);
                }
                let mut claims = lock(&self.claims);
                match claims.as_mut() {
                    Some(c) => Ok(c.handle(&call.verb, &call.args)),
                    None => Err(CallError::Refused("scope: not installed".into())),
                }
            }
            "journal" => Ok(match call.verb.as_str() {
                "brief_read" => lock(&self.brief).clone(),
                "session_parent" => json!({"ok": true}),
                _ => json!({}),
            }),
            "operations" if self.operations_down.load(Ordering::SeqCst) => Err(CallError::Down("operations is down".into())),
            "operations" => Ok(json!({"task_id": "task-9"})),
            "reflection" => Err(CallError::Down("no route".into())),
            _ => Err(CallError::Refused(format!("{}: not installed", call.tool))),
        }
    }
}

/// The node: no peers unless a test names them, the clock, and `/w/projects`.
pub struct FakeNode {
    pub peers: Mutex<Result<Vec<String>, String>>,
    pub now: Mutex<Option<i64>>,
    pub projects: Mutex<PathBuf>,
    pub emitted: Mutex<Vec<(String, Value)>>,
}

impl Node for FakeNode {
    fn name(&self) -> String {
        "here".into()
    }

    fn peers(&self) -> Result<Vec<String>, String> {
        lock(&self.peers).clone()
    }

    fn projects_dir(&self) -> PathBuf {
        lock(&self.projects).clone()
    }

    fn emit(&self, topic: &str, payload: Value) {
        lock(&self.emitted).push((topic.to_string(), payload));
    }

    fn now_ms(&self) -> i64 {
        lock(&self.now).unwrap_or_else(awm_pycompat::time::now_ms)
    }
}

/// The trust policy, with an answer for `may_steer` a test can fix and the asks it records.
#[derive(Default)]
pub struct FakeTrust {
    pub steer: Mutex<Option<Result<(), String>>>,
    pub asked: Mutex<Vec<(String, String, Option<String>)>>,
}

impl TrustLib for FakeTrust {
    fn may_steer(&self, actor: &str, verb: &str, target: Option<&str>) -> Result<(), String> {
        lock(&self.asked).push((actor.into(), verb.into(), target.map(str::to_string)));
        match lock(&self.steer).clone() {
            Some(answer) => answer,
            None => Policy.may_steer(actor, verb, target),
        }
    }

    fn level_for_start(&self, starter: &str, requested: Option<&str>) -> Result<String, String> {
        Policy.level_for_start(starter, requested)
    }

    fn first_message_hint(&self) -> String {
        Policy.first_message_hint()
    }
}

/// The `fakes` fixture: reflection over the fakes, with its state in a temp dir.
pub struct Fx {
    pub app: Arc<App<FakeGateway>>,
    pub node: Arc<FakeNode>,
    pub trust: Arc<FakeTrust>,
    pub _tmp: tempfile::TempDir,
}

impl Fx {
    pub fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let node = Arc::new(FakeNode { peers: Mutex::new(Ok(vec![])), now: Mutex::default(), projects: Mutex::new(PathBuf::from("/w/projects")), emitted: Mutex::default() });
        let trust = Arc::new(FakeTrust::default());
        let app = Arc::new(App::new(FakeGateway::new(), node.clone(), trust.clone(), Store::new(tmp.path().join("state"))));
        Self { app, node, trust, _tmp: tmp }
    }

    pub fn gw(&self) -> &FakeGateway {
        &self.app.gw
    }

    pub fn cc(&self) -> &Fake {
        &self.app.gw.cc
    }

    pub fn ds(&self) -> &Fake {
        &self.app.gw.ds
    }

    /// Call `verb` as the gateway would, with the stamps in `args`. A start gets a model and an
    /// effort unless the args name them.
    pub async fn run(&self, verb: &str, args: Value) -> Value {
        self.run_as(verb, args, None).await
    }

    pub async fn run_as(&self, verb: &str, args: Value, as_: Option<&str>) -> Value {
        let args = if verb == "start_other" { crate::app::merged(&json!({"model": "opus", "effort": "high"}), &args) } else { args };
        let frame = Frame::from_args(&args, as_, None);
        self.app.handle(verb, &frame, &args).await
    }

    /// The journal posts the alarm made.
    pub fn posts(&self) -> Vec<Value> {
        self.gw().calls_to("journal").into_iter().filter(|(v, _)| v == "post").map(|(_, a)| a).collect()
    }

    /// The parent links reflection asked the journal to record, as `(session_id, parent, how)`.
    pub fn links(&self) -> Vec<(String, String, String)> {
        let s = |a: &Value, k: &str| a[k].as_str().unwrap_or_default().to_string();
        self.gw()
            .calls_to("journal")
            .into_iter()
            .filter(|(v, _)| v == "session_parent")
            .map(|(_, a)| (s(&a, "session_id"), s(&a, "parent"), s(&a, "how")))
            .collect()
    }

    pub fn deny_steers(&self, why: &str) {
        *lock(&self.trust.steer) = Some(Err(why.into()));
    }

    pub fn asked(&self) -> Vec<(String, String, Option<String>)> {
        lock(&self.trust.asked).clone()
    }

    pub fn set_peers(&self, peers: &[&str]) {
        *lock(&self.node.peers) = Ok(peers.iter().map(|p| p.to_string()).collect());
    }

    pub fn weak(&self) -> Weak<App<FakeGateway>> {
        Arc::downgrade(&self.app)
    }

    pub async fn trust_file(&self) -> Value {
        Value::Object(self.app.trust_data().await)
    }
}

/// A log sink a test reads back.
#[derive(Clone, Default)]
pub struct Captured(pub Arc<Mutex<Vec<u8>>>);

impl std::io::Write for Captured {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Captured {
    type Writer = Captured;

    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

impl Captured {
    /// Capture this thread's logs until the guard drops.
    ///
    /// A callsite caches its interest from the dispatchers alive when it first fires. With no
    /// global subscriber, a parallel test that fires it first caches "never", and this thread's
    /// subscriber then misses the event. A global one that discards keeps every callsite live.
    pub fn install(&self) -> tracing::subscriber::DefaultGuard {
        static GLOBAL: std::sync::Once = std::sync::Once::new();
        GLOBAL.call_once(|| {
            let sink = tracing_subscriber::fmt().with_writer(std::io::sink).with_max_level(tracing::Level::INFO).finish();
            let _ = tracing::subscriber::set_global_default(sink);
        });
        let sub = tracing_subscriber::fmt().with_writer(self.clone()).with_max_level(tracing::Level::INFO).with_ansi(false).finish();
        tracing::subscriber::set_default(sub)
    }

    pub fn text(&self) -> String {
        String::from_utf8(self.0.lock().unwrap().clone()).unwrap()
    }
}
