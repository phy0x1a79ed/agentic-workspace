//! The gateway's runtime against real spawned services: calls to absent and starting tools,
//! install hooks, the stale-env report, the needs-only peer notice and a forced restart.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use awm_gateway::config::Config;
use awm_gateway::gateway::ReloadRequest;
use awm_gateway::peer::{BoxFuture, PeerError, PeerTransport};
use awm_gateway::Gateway;
use awm_proto::{CallContext, Caller, Lane, Surface};
use serde_json::{json, Value};

/// One peer, `far`, whose table a test changes.
#[derive(Clone, Default)]
struct OnePeer(Arc<Mutex<Value>>);

impl PeerTransport for OnePeer {
    fn peers(&self) -> Vec<String> {
        vec!["far".into()]
    }

    fn invoke<'a>(&'a self, peer: &'a str, _: &'a str, _: Value, _: &'a CallContext) -> BoxFuture<'a, Result<String, PeerError>> {
        Box::pin(async move { Err(PeerError::Unreachable { peer: peer.into(), reason: "test".into() }) })
    }

    fn tools<'a>(&'a self, _: &'a str) -> BoxFuture<'a, Result<Value, PeerError>> {
        let table = self.0.lock().unwrap().clone();
        Box::pin(async move { Ok(table) })
    }
}

struct Node {
    _dir: tempfile::TempDir,
    state: PathBuf,
    root: PathBuf,
    names: Mutex<Vec<String>>,
}

impl Node {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let (state, root) = (dir.path().join(".awm"), dir.path().join("roots"));
        std::fs::create_dir_all(&state).unwrap();
        std::fs::create_dir_all(&root).unwrap();
        let node = Self { _dir: dir, state, root, names: Mutex::default() };
        node.config("");
        node
    }

    /// Write config.toml. Long windows keep a service that never registers from being restarted.
    fn config(&self, extra: &str) {
        let text = format!(
            "node = \"here\"\nlisten = \"127.0.0.1:0\"\nworkspace = {:?}\ncore_roots = [{:?}]\ntool_wait_s = 0.2\nreconnect_window_s = 600\nadopt_window_s = 600\n{extra}",
            self.root.parent().unwrap(),
            self.root
        );
        std::fs::write(self.state.join("config.toml"), text).unwrap();
    }

    /// A one-service plugin `name` serving tool `name`. Its service never registers, so it ignores
    /// the shutdown frame and would hold a stop for the whole grace period. It idles until
    /// [`Node::release`] touches its flag file, and exits on its own then.
    fn plugin(&self, name: &str, service_keys: &str, needs: &str) -> PathBuf {
        let dir = self.root.join(name);
        std::fs::create_dir_all(&dir).unwrap();
        self.names.lock().unwrap().push(name.into());
        let script = format!(
            "flag=\"$AWM_DIR/release-{name}\"\nrm -f \"$flag\"\nfor _ in $(seq 1200); do [ -e \"$flag\" ] && exit 0; sleep 0.05; done\n"
        );
        std::fs::write(dir.join("run.sh"), script).unwrap();
        std::fs::write(
            dir.join(awm_proto::MANIFEST_FILE),
            format!("[plugin]\nname = '{name}'\n[[service]]\nname = '{name}'\n{service_keys}[[tool]]\nname = '{name}'\n{needs}"),
        )
        .unwrap();
        dir
    }

    /// Let every idle service exit, ahead of a stop or a restart that would otherwise wait out
    /// the grace period.
    fn release_all(&self) {
        let names = self.names.lock().unwrap().clone();
        self.release(&names.iter().map(String::as_str).collect::<Vec<_>>());
    }

    fn release(&self, names: &[&str]) {
        for name in names {
            std::fs::write(self.state.join(format!("release-{name}")), "").unwrap();
        }
    }

    async fn open(&self, peers: Arc<dyn PeerTransport>) -> Arc<Gateway> {
        Gateway::open_with(&self.state, Config::load(&self.state).unwrap(), peers).await.unwrap()
    }
}

fn no_peers() -> Arc<dyn PeerTransport> {
    Arc::new(awm_gateway::peer::HttpPeers::new(BTreeMap::new()))
}

fn plugin_ctx(node: &str, plugin: &str) -> CallContext {
    CallContext {
        caller: Caller { node: node.into(), lane: Lane::Service, trust: Some("trusted".into()), ..Caller::default() },
        plugin: Some(plugin.into()),
        ..CallContext::local(node, Surface::Plugin)
    }
}

async fn until<T>(what: &str, mut probe: impl FnMut() -> Option<T>) -> T {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(v) = probe() {
            return v;
        }
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

fn pid_of(gw: &Gateway, service: &str) -> Option<i32> {
    gw.supervisor.journal.get(service).and_then(|e| e.pid)
}

fn refusal(gw: &Gateway, plugin: &str) -> Option<String> {
    gw.read_load().refused.iter().find(|r| r.plugin == plugin).map(|r| r.reason.clone())
}

fn loaded(gw: &Gateway, plugin: &str) -> bool {
    gw.read_load().plugins.iter().any(|p| p.name == plugin)
}

/// A call to a tool no table holds fails at once. A call to a tool that is in the table but not
/// up waits `tool_wait_s`, then fails with 503. A plugin calls a tool its needs do not name.
#[tokio::test]
async fn an_absent_tool_fails_at_once_and_a_starting_one_waits() {
    assert_eq!(Config::default().tool_wait_s, 10.0, "the default wait is R117 item 7's 10 s");
    let node = Node::new();
    node.plugin("slow", "", "");
    node.plugin("caller", "", "");
    let gw = node.open(no_peers()).await;
    let ctx = plugin_ctx(&gw.node, "caller");

    let started = Instant::now();
    let absent = gw.invoke("nobody", json!({"verb": "get"}), &ctx).await.unwrap_err();
    assert_eq!((absent.status, absent.detail.clone()), (404, json!("nobody not installed")));
    assert!(started.elapsed() < Duration::from_millis(500), "an absent tool fails at once");

    let started = Instant::now();
    let slow = gw.invoke("slow", json!({"verb": "get"}), &ctx).await.unwrap_err();
    assert_eq!(slow.status, 503, "{:?}", slow.detail);
    assert!(slow.detail.as_str().unwrap().contains("\"slow\" is not up"), "{:?}", slow.detail);
    assert!(started.elapsed() >= Duration::from_millis(150), "a starting tool is waited for");
}

/// The hook runs before the first start with its variables set, not again on an unchanged
/// stamp, and again once the folder changes. A plugin with no `install` key runs none.
#[tokio::test]
async fn the_install_hook_runs_when_the_stamp_moves() {
    let node = Node::new();
    let dir = node.plugin("inst", "install = 'install.sh'\n", "");
    std::fs::write(dir.join("install.sh"), "echo \"$AWM_PLUGIN $AWM_NODE_NAME $PWD\" >> \"$AWM_DIR/hook.log\"\n").unwrap();
    let plain = node.plugin("plain", "", "");
    std::fs::write(plain.join("install.sh"), "touch \"$AWM_DIR/plain-ran\"\n").unwrap();
    let gw = node.open(no_peers()).await;
    awm_gateway::supervisor::start_all(&gw, false).await;

    let runs = || std::fs::read_to_string(node.state.join("hook.log")).unwrap_or_default().lines().count();
    until("the first spawn", || pid_of(&gw, "inst")).await;
    assert_eq!(runs(), 1);
    let line = std::fs::read_to_string(node.state.join("hook.log")).unwrap();
    assert_eq!(line.trim(), format!("inst here {}", dir.canonicalize().unwrap().display()));
    assert!(awm_gateway::hooks::stamp_path(&gw.state, "inst").is_file());

    let first = pid_of(&gw, "inst");
    node.release_all();
    gw.reload(&ReloadRequest { restart: vec!["inst".into()], force: false }).await.unwrap();
    until("the restart", || pid_of(&gw, "inst").filter(|p| Some(*p) != first)).await;
    assert_eq!(runs(), 1, "an unchanged stamp runs no hook");

    std::fs::write(dir.join("new-file"), "x").unwrap();
    let second = pid_of(&gw, "inst");
    node.release_all();
    gw.reload(&ReloadRequest { restart: vec!["inst".into()], force: false }).await.unwrap();
    until("the second restart", || pid_of(&gw, "inst").filter(|p| Some(*p) != second)).await;
    assert_eq!(runs(), 2, "a moved stamp runs the hook again");
    assert!(!node.state.join("plain-ran").exists(), "no install key, no install step");
    node.release_all();
    awm_gateway::supervisor::stop_all(&gw).await;
}

/// A failed hook refuses its plugin alone, names its last output, and cascades to the plugins
/// that need its tool. A restart that names it runs the hook again.
#[tokio::test]
async fn a_failed_install_hook_refuses_its_plugin_and_dependents() {
    let node = Node::new();
    let dir = node.plugin("broken", "install = 'install.sh'\n", "");
    std::fs::write(dir.join("install.sh"), "echo first line\necho boom >&2\nexit 3\n").unwrap();
    let dependent = node.plugin("dependent", "", "[[needs.tool]]\nname = 'broken'\n");
    // It starts before the hook failure refuses it, then is stopped; exiting at once spares the grace wait.
    std::fs::write(dependent.join("run.sh"), "exit 0\n").unwrap();
    node.plugin("bystander", "", "");
    let gw = node.open(no_peers()).await;
    awm_gateway::supervisor::start_all(&gw, false).await;

    let reason = until("the refusal", || refusal(&gw, "broken")).await;
    assert!(reason.contains("install hook") && reason.contains("boom") && reason.contains("first line"), "{reason}");
    let cascaded = until("the cascade", || refusal(&gw, "dependent")).await;
    assert!(cascaded.contains("\"broken\""), "{cascaded}");
    assert!(pid_of(&gw, "broken").is_none(), "a refused plugin never spawns");
    until("the bystander", || pid_of(&gw, "bystander")).await;
    assert!(!awm_gateway::hooks::stamp_path(&gw.state, "broken").exists());

    gw.reload(&ReloadRequest::default()).await.unwrap();
    assert!(refusal(&gw, "broken").is_some(), "a plain reload keeps the refusal while the folder is unchanged");

    std::fs::write(dir.join("install.sh"), "exit 0\n").unwrap();
    gw.reload(&ReloadRequest::default()).await.unwrap();
    until("the fixed plugin", || pid_of(&gw, "broken")).await;
    assert!(loaded(&gw, "broken"));
    node.release_all();
    awm_gateway::supervisor::stop_all(&gw).await;
}

/// `stale_env` names a running plugin whose `[env]` is older than the config's, spawned or adopted.
#[tokio::test]
async fn stale_env_judges_spawned_and_adopted_plugins() {
    let node = Node::new();
    node.config("[env]\nFLAVOUR = \"one\"\n");
    node.plugin("idle", "", "");
    let gw = node.open(no_peers()).await;
    awm_gateway::supervisor::start_all(&gw, false).await;
    let pid = until("the spawn", || pid_of(&gw, "idle")).await;

    let report = gw.reload(&ReloadRequest::default()).await.unwrap();
    assert_eq!(report["stale_env"], json!([]), "{report}");
    node.config("[env]\nFLAVOUR = \"two\"\n");
    let report = gw.reload(&ReloadRequest::default()).await.unwrap();
    assert_eq!(report["stale_env"], json!(["idle"]), "{report}");

    // A gateway restart: the service runs on, and the next gateway adopts it.
    awm_gateway::supervisor::leave_running(&gw);
    node.config("[env]\nFLAVOUR = \"one\"\n");
    let next = node.open(no_peers()).await;
    awm_gateway::supervisor::start_all(&next, false).await;
    assert_eq!(pid_of(&next, "idle"), Some(pid), "the live process is adopted");
    until("the adopted env", || next.supervisor.env_of_running().contains_key("idle").then_some(())).await;
    let report = next.reload(&ReloadRequest::default()).await.unwrap();
    assert_eq!(report["stale_env"], json!([]), "{report}");
    node.config("[env]\nFLAVOUR = \"three\"\n");
    let report = next.reload(&ReloadRequest::default()).await.unwrap();
    assert_eq!(report["stale_env"], json!(["idle"]), "an adopted process carries its spawn env: {report}");
    node.release_all();
    awm_gateway::supervisor::stop_all(&next).await;
}

/// A peer's notice starts the plugins whose needs now resolve and stops those whose needs broke.
/// It reads no config and no new folder, and restarts nothing else.
#[tokio::test]
async fn a_peer_notice_re_resolves_needs_only() {
    let node = Node::new();
    node.plugin("dep", "", "[[needs.tool]]\nname = 'remote'\n");
    node.plugin("steady", "", "");
    let peer = OnePeer::default();
    *peer.0.lock().unwrap() = json!([]);
    // The cached table matches the peer's, so opening runs no reload of its own.
    std::fs::write(node.state.join("peer-tools.json"), json!({"far": []}).to_string()).unwrap();
    let gw = node.open(Arc::new(peer.clone())).await;
    awm_gateway::supervisor::start_all(&gw, false).await;
    let steady = until("steady", || pid_of(&gw, "steady")).await;
    assert!(refusal(&gw, "dep").is_some_and(|r| r.contains("\"remote\"")));

    node.plugin("fresh", "", "");
    node.config("[env]\nUNREAD = \"yes\"\n");
    *peer.0.lock().unwrap() = json!([{"name": "remote", "major": 1, "plugin": "remote"}]);
    assert!(gw.refresh_peer("far").await);
    let report = gw.reload_needs().await.unwrap();
    assert_eq!(report["started"], json!(["dep"]), "{report}");
    until("dep", || pid_of(&gw, "dep")).await;
    assert!(!loaded(&gw, "fresh"), "a new folder waits for a reload");
    assert!(gw.cfg().env.is_empty(), "the config is not read again");
    assert_eq!(pid_of(&gw, "steady"), Some(steady), "steady keeps its process");

    node.release(&["dep"]);
    *peer.0.lock().unwrap() = json!([]);
    assert!(gw.refresh_peer("far").await);
    let report = gw.reload_needs().await.unwrap();
    assert_eq!(report["stopped"], json!(["dep"]), "{report}");
    assert!(refusal(&gw, "dep").is_some());
    assert_eq!(pid_of(&gw, "steady"), Some(steady));
    node.release_all();
    awm_gateway::supervisor::stop_all(&gw).await;
}

/// A tripped breaker keeps a plain restart down. `force` passes it.
#[tokio::test]
async fn a_forced_restart_passes_a_tripped_breaker() {
    let node = Node::new();
    node.plugin("crashy", "", "");
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id").unwrap();
    std::fs::write(
        node.state.join("services.json"),
        json!({"boot_id": boot.trim(), "services": {"crashy": {"tripped": true, "exits": []}}}).to_string(),
    )
    .unwrap();
    let gw = node.open(no_peers()).await;
    awm_gateway::supervisor::start_all(&gw, false).await;
    assert!(gw.supervisor.failed().contains(&"crashy".to_string()));

    let restart = |force| ReloadRequest { restart: vec!["crashy".into()], force };
    gw.reload(&restart(false)).await.unwrap();
    assert!(pid_of(&gw, "crashy").is_none(), "a plain restart leaves a tripped plugin down");

    gw.reload(&restart(true)).await.unwrap();
    until("the forced start", || pid_of(&gw, "crashy")).await;
    assert!(!gw.supervisor.journal.tripped("crashy"));
    node.release_all();
    awm_gateway::supervisor::stop_all(&gw).await;
}

/// A mount's response passes through chunk by chunk: the first event reaches the client while
/// the service still holds the second.
#[tokio::test]
async fn a_mount_streams_its_response() {
    use axum::body::Body;
    use tokio::sync::Notify;

    let release = Arc::new(Notify::new());
    let held = release.clone();
    let upstream = axum::Router::new().route(
        "/events",
        axum::routing::get(move || {
            let held = held.clone();
            async move {
                let events = futures_util::stream::unfold(0, move |n| {
                    let held = held.clone();
                    async move {
                        match n {
                            0 => Some((Ok::<_, std::io::Error>("data: one\n\n"), 1)),
                            1 => {
                                held.notified().await;
                                Some((Ok("data: two\n\n"), 2))
                            }
                            _ => None,
                        }
                    }
                });
                ([("content-type", "text/event-stream")], Body::from_stream(events))
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mount = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, upstream).await });

    let node = Node::new();
    let gw = node.open(no_peers()).await;
    let id = gw.hub.register("sse", "sse", None, Some(mount)).unwrap();
    let _claim = gw.hub.claim(&id);
    gw.hub.ready("sse", &json!({"functions": []}), &["sse".to_string()]);
    tokio::spawn(awm_gateway::server::serve(gw.clone()));
    let url = until("the listener", || gw.url.get().cloned()).await;

    let mut resp = reqwest::get(format!("{url}/s/sse/events")).await.unwrap();
    assert_eq!(resp.headers()["content-type"], "text/event-stream");
    let first = tokio::time::timeout(Duration::from_secs(5), resp.chunk()).await.expect("the first event is held back");
    assert_eq!(first.unwrap().unwrap(), "data: one\n\n");
    release.notify_one();
    let second = tokio::time::timeout(Duration::from_secs(5), resp.chunk()).await.unwrap();
    assert_eq!(second.unwrap().unwrap(), "data: two\n\n");
}
