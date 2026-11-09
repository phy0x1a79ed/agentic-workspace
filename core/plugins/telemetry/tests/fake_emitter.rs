//! The node part and the roster end to end, against a fake gateway that serves the emit topics
//! the contract names. The test pushes each event; nothing in the plugin waits on a clock.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use awm_plugin_sdk::axum::extract::ws::{Message, WebSocketUpgrade};
use awm_plugin_sdk::axum::extract::{Path, State};
use awm_plugin_sdk::axum::routing::get;
use awm_plugin_sdk::axum::Router;
use awm_plugin_sdk::{Caller, Env, Hub, Service, TestService};
use awm_plugin_telemetry::alcor::{self, RosterPart};
use awm_plugin_telemetry::node::{self, Forward, Probe};
use awm_plugin_telemetry::roster::{Item, ServiceRow, Session, ToolRow};
use futures_util::future::BoxFuture;
use serde_json::{json, Value};
use tokio::sync::broadcast;

/// A gateway that serves `/hub/emit/<service>/<topic>` and nothing else.
#[derive(Clone, Default)]
struct FakeEmitter {
    topics: Arc<Mutex<HashMap<String, broadcast::Sender<String>>>>,
    subscribers: Arc<Mutex<HashMap<String, usize>>>,
}

impl FakeEmitter {
    fn channel(&self, key: &str) -> broadcast::Sender<String> {
        self.topics.lock().unwrap().entry(key.into()).or_insert_with(|| broadcast::channel(64).0).clone()
    }

    fn emit(&self, service: &str, topic: &str, payload: Value) {
        self.channel(&format!("{service}/{topic}")).send(payload.to_string()).expect("a subscriber");
    }

    fn subscribed(&self, key: &str) -> bool {
        self.subscribers.lock().unwrap().get(key).copied().unwrap_or(0) > 0
    }

    async fn serve(self) -> String {
        let app = Router::new()
            .route(
                "/hub/emit/{service}/{topic}",
                get(|State(me): State<FakeEmitter>, Path((service, topic)): Path<(String, String)>, ws: WebSocketUpgrade| async move {
                    let key = format!("{service}/{topic}");
                    let mut rx = me.channel(&key).subscribe();
                    *me.subscribers.lock().unwrap().entry(key).or_default() += 1;
                    ws.on_upgrade(move |mut socket| async move {
                        while let Ok(text) = rx.recv().await {
                            if socket.send(Message::Text(text.into())).await.is_err() {
                                break;
                            }
                        }
                    })
                }),
            )
            .with_state(self);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { awm_plugin_sdk::axum::serve(listener, app).await.unwrap() });
        format!("http://{addr}")
    }
}

/// The node's state at connect: one session already running.
struct FakeProbe;

impl Probe for FakeProbe {
    fn hello(&self) -> BoxFuture<'_, anyhow::Result<Item>> {
        Box::pin(async {
            Ok(Item::Hello {
                role: Some("home".into()),
                portal: Some("https://mizar.example:8443".into()),
                sessions: vec![Session { job: "boot".into(), link: Some("https://claude.ai/code/boot".into()), ..Session::default() }],
                services: vec![ServiceRow { service: "journal".into(), plugin: Some("journal".into()), tools: vec!["journal".into()], up: true, enabled: Some(true) }],
                tools: vec![ToolRow { tool: "journal".into(), host: None }, ToolRow { tool: "social".into(), host: Some("alcor".into()) }],
                plugins: vec![],
            })
        })
    }

    fn tools(&self) -> BoxFuture<'_, anyhow::Result<Item>> {
        Box::pin(async { Ok(Item::Tools { services: vec![], tools: vec![ToolRow { tool: "journal".into(), host: None }], plugins: vec![] }) })
    }
}

fn env(dir: &std::path::Path) -> Env {
    Env { hub_url: "http://127.0.0.1:1".into(), service: "telemetry".into(), plugin: "telemetry".into(), node: "alcor".into(), awm_dir: dir.into(), token_file: None }
}

type Roster = Arc<tokio::sync::Mutex<TestService>>;

/// Reports go straight to the roster's handler, stamped as the door would stamp mizar's call.
fn forward_to(roster: Roster) -> Forward {
    Arc::new(move |args: Value| {
        let roster = roster.clone();
        Box::pin(async move {
            let caller = Caller { node: "mizar".into(), ..Caller::default() };
            roster.lock().await.call_as("report", args, caller, Some("peer:mizar")).await.map_err(|e| anyhow::anyhow!("{e}"))
        })
    })
}

async fn until<F: Fn(&Value) -> bool>(roster: &Roster, what: &str, f: F) -> (Value, Duration) {
    let start = Instant::now();
    loop {
        let v = roster.lock().await.call("nodes", json!({})).await.unwrap();
        if f(&v) {
            return (v, start.elapsed());
        }
        assert!(start.elapsed() < Duration::from_secs(5), "{what}: not within 5 s: {v}");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

fn jobs(v: &Value) -> Vec<String> {
    let mut j: Vec<String> = v["nodes"][0]["sessions"].as_array().into_iter().flatten().filter_map(|s| s["job"].as_str()).map(str::to_string).collect();
    j.sort();
    j
}

fn topics(frames: &[Value]) -> Vec<(String, String)> {
    frames.iter().filter(|f| f["kind"] == "emit").map(|f| (f["topic"].as_str().unwrap_or("").to_string(), f["payload"]["event"].as_str().unwrap_or("").to_string())).collect()
}

#[tokio::test]
async fn a_session_start_and_stop_reach_the_roster_within_seconds() {
    let tmp = tempfile::tempdir().unwrap();
    let part = RosterPart::load(tmp.path().join("state"), Some("tony".into()), Arc::new(|| vec![("social".into(), "alcor".into())])).unwrap();
    let roster: Roster = Arc::new(tokio::sync::Mutex::new(alcor::service(Service::new(env(tmp.path())), part).into_test().await.unwrap()));

    let fake = FakeEmitter::default();
    let base = fake.clone().serve().await;
    let hub = Hub::new(&base, None);
    let _part = node::start(hub, "mizar".into(), "tony".into(), Arc::new(FakeProbe), forward_to(roster.clone()), Arc::new(|p: &str, _: Option<&str>| p == "secret"));

    // Each subscription's first connect sends the hello read at connect.
    let (v, _) = until(&roster, "hello", |v| jobs(v) == ["boot"]).await;
    assert_eq!(v["nodes"][0]["role"], "home");
    assert_eq!(v["nodes"][0]["sessions"][0]["link"], "https://claude.ai/code/boot");
    for (service, topic) in node::TOPICS {
        let key = format!("{service}/{topic}");
        let start = Instant::now();
        while !fake.subscribed(&key) {
            assert!(start.elapsed() < Duration::from_secs(5), "{key} never subscribed");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }
    roster.lock().await.frames();

    fake.emit("reflection", "sessions", json!({"event": "started", "job": "w1", "session_id": "s-w1", "name": "worker", "node": "mizar",
        "project": "awm", "scope": "lobby", "kind": "worker", "task_id": "CWPY", "rc_url": "https://claude.ai/code/s-w1", "ts": "2026-11-07T10:00:00Z"}));
    let (v, took) = until(&roster, "start", |v| jobs(v) == ["boot", "w1"]).await;
    assert!(took < Duration::from_secs(2), "start took {took:?}");
    let w1 = v["nodes"][0]["sessions"].as_array().unwrap().iter().find(|s| s["job"] == "w1").unwrap().clone();
    assert_eq!((w1["link"].as_str(), w1["task_id"].as_str()), (Some("https://claude.ai/code/s-w1"), Some("CWPY")));
    assert_eq!(v["nodes"][0]["sessions_7d"], 2);
    let frames = roster.lock().await.frames();
    assert_eq!(topics(&frames), [("sessions".into(), "live".into()), ("roster".into(), "".into())]);
    assert_eq!(frames[0]["payload"]["session_id"], "s-w1");

    fake.emit("reflection", "sessions", json!({"event": "stopped", "job": "w1", "session_id": "s-w1", "node": "mizar", "ts": "2026-11-07T10:01:00Z"}));
    let (v, took) = until(&roster, "stop", |v| jobs(v) == ["boot"]).await;
    assert!(took < Duration::from_secs(2), "stop took {took:?}");
    assert_eq!(v["nodes"][0]["sessions_7d"], 2, "a stopped session still counts for the week");
    let frames = roster.lock().await.frames();
    assert_eq!(topics(&frames), [("sessions".into(), "gone".into()), ("roster".into(), "".into())]);

    // A session in a private project reaches the roster as its job alone.
    fake.emit("reflection", "sessions", json!({"event": "started", "job": "p1", "name": "hush", "node": "mizar", "project": "secret",
        "rc_url": "https://claude.ai/code/p1", "task_id": "T"}));
    let (v, _) = until(&roster, "private start", |v| jobs(v) == ["boot", "p1"]).await;
    let p1 = v["nodes"][0]["sessions"].as_array().unwrap().iter().find(|s| s["job"] == "p1").unwrap().clone();
    assert!(p1.get("name").is_none() && p1.get("project").is_none() && p1.get("link").is_none() && p1.get("task_id").is_none(), "{p1}");
    roster.lock().await.frames();

    // A service going down, and a use of a singleton from mizar.
    fake.emit("awm", "services", json!({"service": "journal", "plugin": "journal", "event": "down", "pid": 7, "ts": "2026-11-07T10:02:00Z"}));
    fake.emit("awm", "usage", json!({"v": 1, "ts": "2026-11-07T10:03:00Z", "plugin": "social", "surface": "mcp", "tool": "social", "verb": "seats", "ok": true}));
    fake.emit("awm", "usage", json!({"v": 1, "ts": "2026-11-07T10:04:00Z", "plugin": "telemetry-node", "surface": "plugin", "tool": "telemetry", "verb": "report", "ok": true}));
    let (v, _) = until(&roster, "service and usage", |v| {
        let n = &v["nodes"][0];
        n["services"][0]["up"] == false && n["uses"].as_array().unwrap().iter().any(|u| u["tool"] == "social" && u["last_used"] == "2026-11-07T10:03:00Z")
    })
    .await;
    assert!(!v["nodes"][0]["uses"].as_array().unwrap().iter().any(|u| u["tool"] == "telemetry"), "the report's own use is not reported");
    let services = roster.lock().await.call("services", json!({})).await.unwrap();
    assert_eq!((services["services"][0]["tool"].as_str(), services["services"][0]["health"].as_str()), (Some("social"), Some("unknown")));
    assert_eq!(services["services"][0]["last_used"], "2026-11-07T10:03:00Z");

    // A plugin change reads the node's tools again.
    fake.emit("awm", "plugins", json!({"plugin": "kb", "event": "removed", "version": "x", "root": "core"}));
    until(&roster, "refresh", |v| v["nodes"][0]["uses"].as_array().unwrap().iter().any(|u| u["tool"] == "social" && u["available"] == false)).await;
}
