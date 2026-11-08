//! Being called: register, `ready`, the call loop, sessions and the mount (contract § Service
//! wire protocol). The twin of `gatewayclient/adapter.py`.

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use awm_proto::{Caller, Surface, UsageEvent};
use futures_util::{FutureExt, SinkExt, StreamExt};
use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::sync::{mpsc, watch};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::Message;

use crate::cancel::CancelToken;
use crate::hub::{Bridge, Hub, HubError};

type BoxFut<T> = Pin<Box<dyn Future<Output = T> + Send>>;
type Handler = Arc<dyn Fn(Call) -> BoxFut<Result<Value, VerbError>> + Send + Sync>;
type SessionHandler = Arc<dyn Fn(Session) -> BoxFut<anyhow::Result<()>> + Send + Sync>;

pub const TRUST_KEY: &str = "_caller_trust";
pub const SCOPE_KEY: &str = "_caller_scope";
pub const PID_KEY: &str = "_caller_pid";
pub const SESSION_KEY: &str = "_caller_session_id";
pub const CLAIM_KEY: &str = "_caller_session";

/// A verb's failure, as the reply's `error` and `error_class`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerbError {
    pub message: String,
    pub error_class: String,
}

impl VerbError {
    pub fn new(error_class: impl Into<String>, message: impl Into<String>) -> Self {
        Self { message: message.into(), error_class: error_class.into() }
    }

    /// A usage error: bad or missing arguments.
    pub fn value(message: impl Into<String>) -> Self {
        Self::new("ValueError", message)
    }

    pub fn runtime(message: impl Into<String>) -> Self {
        Self::new("RuntimeError", message)
    }

    /// An `anyhow` error with its context chain, for `.map_err(VerbError::from_anyhow)`.
    pub fn from_anyhow(e: anyhow::Error) -> Self {
        Self::runtime(format!("{e:#}"))
    }

    /// A refusal: the caller may not do this.
    pub fn permission(message: impl Into<String>) -> Self {
        Self::new("PermissionError", message)
    }
}

impl std::fmt::Display for VerbError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl<E: std::error::Error + Send + Sync + 'static> From<E> for VerbError {
    fn from(e: E) -> Self {
        Self::runtime(e.to_string())
    }
}



/// What the gateway told this process when it spawned it.
#[derive(Debug, Clone)]
pub struct Env {
    pub hub_url: String,
    pub service: String,
    /// `AWM_PLUGIN`, else the service name.
    pub plugin: String,
    pub node: String,
    pub awm_dir: PathBuf,
    pub token_file: Option<PathBuf>,
}

impl Env {
    /// The service's own state directory, `<AWM_DIR>/services/<service>`, as the Python services keep it.
    pub fn state_dir(&self) -> PathBuf {
        self.awm_dir.join("services").join(&self.service)
    }

    /// Read `AWM_HUB_URL`, `AWM_SERVICE_NAME`, `AWM_PLUGIN`, `AWM_NODE_NAME`, `AWM_DIR` and `AWM_HUB_TOKEN_FILE`.
    pub fn from_env() -> anyhow::Result<Self> {
        let var = |n: &str| std::env::var(n).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty());
        let hub_url = var("AWM_HUB_URL").ok_or_else(|| anyhow::anyhow!("AWM_HUB_URL is not set; cannot reach the gateway"))?;
        let service = var("AWM_SERVICE_NAME").ok_or_else(|| anyhow::anyhow!("AWM_SERVICE_NAME is not set"))?;
        Ok(Self {
            hub_url: hub_url.trim_end_matches('/').to_string(),
            plugin: var("AWM_PLUGIN").unwrap_or_else(|| service.clone()),
            service,
            node: crate::config::node_name(),
            awm_dir: crate::config::awm_dir(),
            token_file: var("AWM_HUB_TOKEN_FILE").map(PathBuf::from),
        })
    }
}

/// The running service, as handlers and background tasks reach it. Cloning shares it.
#[derive(Clone)]
pub struct Context(Arc<CtxInner>);

struct CtxInner {
    env: Env,
    hub: Hub,
    /// The live control connection's outbox, with the connection's number.
    link: Mutex<Option<(u64, mpsc::UnboundedSender<String>)>>,
}

impl Context {
    fn new(env: Env) -> Self {
        let hub = Hub::new(&env.hub_url, env.token_file.clone());
        Self(Arc::new(CtxInner { env, hub, link: Mutex::new(None) }))
    }

    pub fn env(&self) -> &Env {
        &self.0.env
    }

    pub fn hub(&self) -> &Hub {
        &self.0.hub
    }

    fn outbox(&self) -> Option<mpsc::UnboundedSender<String>> {
        self.0.link.lock().unwrap_or_else(|p| p.into_inner()).as_ref().map(|(_, tx)| tx.clone())
    }

    fn send(&self, frame: Value) -> bool {
        self.outbox().is_some_and(|tx| tx.send(frame.to_string()).is_ok())
    }

    /// Whether the control connection is up now.
    pub fn connected(&self) -> bool {
        self.outbox().is_some()
    }

    /// Publish `payload` on `topic` to every subscriber. Best effort: false, and nothing sent,
    /// while the control connection is down.
    pub fn emit(&self, topic: &str, payload: impl Serialize) -> bool {
        let payload = serde_json::to_value(payload).unwrap_or(Value::Null);
        self.send(json!({"kind": "emit", "topic": topic, "payload": payload}))
    }

    /// Count one background run of `name`: a control frame, else `POST /usage`. Never fails.
    pub fn usage(&self, name: &str, ok: bool, ms: u64) {
        if self.send(json!({"kind": "usage", "surface": "background", "name": name, "ok": ok, "ms": ms})) {
            return;
        }
        let mut ev = UsageEvent::new(self.0.env.plugin.clone(), Surface::Background);
        (ev.name, ev.ok, ev.ms) = (Some(name.into()), Some(ok), Some(ms));
        let hub = self.0.hub.clone();
        if let Ok(rt) = tokio::runtime::Handle::try_current() {
            rt.spawn(async move {
                if let Err(e) = hub.post_usage(&ev).await {
                    tracing::debug!("background usage for {:?} dropped: {e}", ev.name);
                }
            });
        }
    }

    /// Run `work` in the background, fire and forget. A failure is logged under `name`.
    pub fn spawn(&self, name: &str, work: impl Future<Output = anyhow::Result<()>> + Send + 'static) -> tokio::task::JoinHandle<()> {
        let name = name.to_string();
        tokio::spawn(async move {
            if let Err(e) = work.await {
                tracing::warn!("background task {name} failed: {e:#}");
            }
        })
    }

    /// Run `work` as one background run of `job`, counted with its outcome and duration.
    pub async fn background<T, E>(&self, job: &str, work: impl Future<Output = Result<T, E>>) -> Result<T, E> {
        let started = Instant::now();
        let out = work.await;
        self.usage(job, out.is_ok(), started.elapsed().as_millis() as u64);
        out
    }
}

/// One inbound call, as its handler gets it.
pub struct Call {
    pub id: Value,
    pub tool: String,
    pub verb: String,
    pub args: Value,
    /// The caller the gateway stamped (R103).
    pub caller: Caller,
    /// The `X-Awm-As` principal, `peer:<node>` for a call from another node.
    pub as_: Option<String>,
    /// The calling plugin, when the gateway's frame names one.
    pub plugin: Option<String>,
    pub cancel: CancelToken,
    pub ctx: Context,
}

impl Call {
    pub fn hub(&self) -> &Hub {
        self.ctx.hub()
    }

    /// One argument, decoded. Absent or null is None; a value of the wrong type is a usage error.
    pub fn arg<T: DeserializeOwned>(&self, name: &str) -> Result<Option<T>, VerbError> {
        match self.args.get(name) {
            None | Some(Value::Null) => Ok(None),
            Some(v) => serde_json::from_value(v.clone()).map(Some).map_err(|e| VerbError::value(format!("{}: argument {name}: {e}", self.verb))),
        }
    }

    /// One argument the verb cannot run without.
    pub fn req<T: DeserializeOwned>(&self, name: &str) -> Result<T, VerbError> {
        self.arg(name)?.ok_or_else(|| VerbError::value(format!("{}: missing required argument(s): {name}.", self.verb)))
    }

    pub fn str_arg(&self, name: &str) -> Option<&str> {
        self.args.get(name).and_then(Value::as_str)
    }

    /// `_caller_trust`: the calling session's level, as the gateway wrote it.
    pub fn caller_trust(&self) -> Option<&str> {
        self.str_arg(TRUST_KEY)
    }

    /// The level the call runs at. A call with no `_caller_trust` is untrusted.
    pub fn level(&self) -> awm_trust::Level {
        awm_trust::stamped_level(self.caller_trust(), false)
    }

    pub fn trusted(&self) -> bool {
        self.level() == awm_trust::Level::Trusted
    }

    /// `_caller_scope`: the calling session's `<project>/<scope>`.
    pub fn caller_scope(&self) -> Option<&str> {
        self.str_arg(SCOPE_KEY)
    }

    /// `_caller_pid`: the calling session's pid on this node.
    pub fn caller_pid(&self) -> Option<u32> {
        self.args.get(PID_KEY).filter(|v| v.is_u64()).and_then(Value::as_u64).and_then(|p| u32::try_from(p).ok())
    }

    /// `_caller_session_id`: the R9 id of an isolated session.
    pub fn caller_session_id(&self) -> Option<&str> {
        self.str_arg(SESSION_KEY)
    }

    /// `_caller_session`: the `{kind, job, name}` claim of a session on another node.
    pub fn caller_claim(&self) -> Option<&Value> {
        self.args.get(CLAIM_KEY).filter(|v| v.is_object())
    }

    /// Whether the call comes from another node, by its `as` stamp.
    pub fn from_peer(&self) -> bool {
        crate::privacy::from_peer(self.as_.as_deref())
    }

    /// The node of a `peer:<node>` stamp.
    pub fn peer_node(&self) -> Option<&str> {
        crate::config::caller_peer(self.as_.as_deref())
    }

    /// The args without the gateway's `_caller_*` stamps.
    pub fn declared_args(&self) -> Value {
        let mut a = self.args.clone();
        if let Some(m) = a.as_object_mut() {
            for k in [TRUST_KEY, SCOPE_KEY, PID_KEY, SESSION_KEY, CLAIM_KEY] {
                m.remove(k);
            }
        }
        a
    }
}

/// One direct session the gateway opened, as its handler gets it.
pub struct Session {
    pub session_id: String,
    pub kind: String,
    /// The client's first frame.
    pub init: Value,
    pub caller: Caller,
    pub as_: Option<String>,
    pub ctx: Context,
    bridge_url: Option<String>,
}

impl Session {
    /// Open this service's end of the session. The gateway relays it to the client's socket.
    pub async fn open_bridge(&self) -> Result<Bridge, HubError> {
        let what = format!("bridge {}", self.session_id);
        let url = self.bridge_url.as_deref().ok_or_else(|| HubError::Transport { what: what.clone(), reason: "not a direct session".into() })?;
        let req = url.into_client_request().map_err(|e| HubError::Transport { what: what.clone(), reason: e.to_string() })?;
        Ok(Bridge::new(crate::hub::connect_ws(&what, req, Duration::from_secs(10)).await?, what))
    }
}

struct VerbEntry {
    function: String,
    tool: String,
    verb: String,
    spec: Value,
    handler: Handler,
}

/// A plugin service: its verbs, sessions and mount, and the loop that serves them.
pub struct Service {
    ctx: Context,
    verbs: Vec<VerbEntry>,
    sessions: BTreeMap<String, SessionHandler>,
    api: serde_json::Map<String, Value>,
    mount: Option<(u16, axum::Router)>,
    on_start: Option<BoxFut<anyhow::Result<()>>>,
    on_ready: Option<BoxFut<()>>,
    on_shutdown: Option<BoxFut<()>>,
    state_toml: Option<&'static str>,
}

/// The init gate: None while `on_start` runs, then its outcome.
type Gate = watch::Receiver<Option<Result<(), String>>>;

struct Shared {
    ctx: Context,
    verbs: HashMap<String, VerbEntry>,
    sessions: BTreeMap<String, SessionHandler>,
    gate: Gate,
    calls: Mutex<HashMap<String, (tokio::task::AbortHandle, CancelToken)>>,
    init_wait: Duration,
}

enum Stop {
    /// Stand down: the gateway said so, or another instance holds the name.
    GiveUp(String),
    Retry(String),
}

impl Service {
    /// A service named by the environment the gateway spawned it with ([`Env::from_env`]).
    pub fn from_env() -> anyhow::Result<Self> {
        Ok(Self::new(Env::from_env()?))
    }

    pub fn new(env: Env) -> Self {
        Self { ctx: Context::new(env), verbs: vec![], sessions: BTreeMap::new(), api: serde_json::Map::new(), mount: None, on_start: None, on_ready: None, on_shutdown: None, state_toml: None }
    }

    /// The handle handlers and background tasks emit and count through.
    pub fn context(&self) -> Context {
        self.ctx.clone()
    }

    /// Publish `tool`'s verb `name`. `params` is the contract's list of `{name, type, required,
    /// description, if_missing?}`. The handler's result is the reply's JSON `result`.
    pub fn verb<F, Fut, R>(mut self, tool: &str, name: &str, description: &str, params: Value, handler: F) -> Self
    where
        F: Fn(Call) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<R, VerbError>> + Send + 'static,
        R: Serialize,
    {
        let taken = self.verbs.iter().any(|v| v.function == name);
        let function = if taken { format!("{tool}_{name}") } else { name.to_string() };
        let mut spec = json!({"name": function, "tool": format!("{tool}_{name}"), "description": description});
        if !params.is_null() {
            spec["params"] = params;
        }
        let handler: Handler = Arc::new(move |call| {
            let fut = handler(call);
            Box::pin(async move { fut.await.and_then(|r| serde_json::to_value(r).map_err(|e| VerbError::runtime(format!("result: {e}")))) })
        });
        self.verbs.push(VerbEntry { function, tool: tool.into(), verb: name.into(), spec, handler });
        self
    }

    fn last_spec(&mut self) -> &mut Value {
        &mut self.verbs.last_mut().expect("call .verb() before setting its options").spec
    }

    /// The surfaces the last verb appears on, such as `["cli", "http"]` for a hook-only verb.
    pub fn surfaces<I, S>(mut self, surfaces: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let list: Vec<String> = surfaces.into_iter().map(|s| s.as_ref().to_string()).collect();
        self.last_spec()["surfaces"] = json!(list);
        self
    }

    /// The name the gateway sends in the last verb's `call` frames, instead of the verb name.
    pub fn function(mut self, function: &str) -> Self {
        let entry = self.verbs.last_mut().expect("call .verb() before setting its options");
        entry.function = function.to_string();
        entry.spec["name"] = json!(function);
        self
    }

    /// The flat name the last verb publishes, instead of `<tool>_<verb>`. The gateway binds it
    /// to the longest tool of the plugin it starts with, and the rest is the verb.
    pub fn published_as(mut self, flat: &str) -> Self {
        self.last_spec()["tool"] = json!(flat);
        self
    }

    /// Keep the last verb off the MCP surface: a hook-only verb the CLI and HTTP still reach.
    pub fn hidden(self) -> Self {
        self.surfaces(["cli", "http"])
    }

    /// How long the gateway waits for the last verb's reply, instead of its default.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.last_spec()["timeout"] = json!(timeout.as_secs_f64());
        self
    }

    /// Serve direct sessions of `kind` over the bridge socket.
    pub fn session<F, Fut>(mut self, kind: &str, handler: F) -> Self
    where
        F: Fn(Session) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = anyhow::Result<()>> + Send + 'static,
    {
        self.sessions.insert(kind.into(), Arc::new(move |s| Box::pin(handler(s)) as BoxFut<anyhow::Result<()>>));
        self
    }

    /// Add a key to the `ready` api beside `functions` and `sessions`, such as `config`.
    pub fn api(mut self, key: &str, value: Value) -> Self {
        self.api.insert(key.into(), value);
        self
    }

    /// Declare an emit topic in the `ready` api's `emitters`, as `{topic, description?}`.
    pub fn emitter(mut self, topic: &str, description: Option<&str>) -> Self {
        let mut e = json!({"topic": topic});
        if let Some(d) = description {
            e["description"] = json!(d);
        }
        let list = self.api.entry("emitters").or_insert_with(|| json!([]));
        if let Some(a) = list.as_array_mut() {
            a.push(e);
        }
        self
    }

    /// Run `hook` once, after the first `ready` frame is queued: work that must start only once
    /// the service is reachable, such as a loop that calls the gateway.
    pub fn on_ready(mut self, hook: impl Future<Output = ()> + Send + 'static) -> Self {
        self.on_ready = Some(Box::pin(hook));
        self
    }

    /// Serve `router` on loopback and register it as the mount the gateway serves at `/s/<service>/`.
    pub fn mount(mut self, router: axum::Router) -> Self {
        self.mount = Some((0, router));
        self
    }

    /// [`Service::mount`] on a chosen loopback port.
    pub fn mount_on(mut self, port: u16, router: axum::Router) -> Self {
        self.mount = Some((port, router));
        self
    }

    /// Run `hook` once when the service stands down, before [`Service::run`] returns.
    pub fn on_shutdown(mut self, hook: impl Future<Output = ()> + Send + 'static) -> Self {
        self.on_shutdown = Some(Box::pin(hook));
        self
    }

    /// The plugin's `state.toml` (statecheck's declaration), usually `include_str!("../state.toml")`.
    /// [`Service::run`] takes its snapshot before anything else. Without one, nothing is copied.
    pub fn state(mut self, toml: &'static str) -> Self {
        self.state_toml = Some(toml);
        self
    }

    /// Run `init` once, beside registration: the service is ready at once, and calls wait for
    /// `init` up to `AWM_INIT_WAIT_S` (60 s). A failed `init` ends [`Service::run`] with its error.
    pub fn on_start(mut self, init: impl Future<Output = anyhow::Result<()>> + Send + 'static) -> Self {
        self.on_start = Some(Box::pin(init));
        self
    }

    /// The `ready` api this service sends: `functions`, `sessions` and each [`Service::api`] key.
    pub fn manifest(&self) -> Value {
        let mut api = self.api.clone();
        api.insert("functions".into(), Value::Array(self.verbs.iter().map(|v| v.spec.clone()).collect()));
        if !self.sessions.is_empty() {
            let sessions: Vec<Value> = self.sessions.keys().map(|k| json!({"kind": k, "transport": "direct"})).collect();
            api.insert("sessions".into(), Value::Array(sessions));
        }
        Value::Object(api)
    }

    /// Register, send `ready` and serve until the gateway stands the service down. A lost
    /// connection reconnects with backoff, and gives up after `AWM_RECONNECT_DEADLINE_S` (10 s).
    pub async fn run(mut self) -> anyhow::Result<()> {
        let (env, toml) = (self.ctx.env().clone(), self.state_toml);
        tokio::task::spawn_blocking(move || snapshot_state(&env, toml)).await??;
        let manifest = self.manifest();
        let mount = match self.mount.take() {
            Some((port, router)) => Some(serve_mount(port, router).await?),
            None => None,
        };
        let (gate_tx, gate) = watch::channel(None);
        let init = self.on_start.take();
        if init.is_none() {
            let _ = gate_tx.send(Some(Ok(())));
        }
        let shared = Arc::new(Shared {
            ctx: self.ctx.clone(),
            verbs: self.verbs.into_iter().map(|v| (v.function.clone(), v)).collect(),
            sessions: self.sessions,
            gate,
            calls: Mutex::new(HashMap::new()),
            init_wait: Duration::from_secs_f64(env_secs("AWM_INIT_WAIT_S", 60.0)),
        });
        let (on_shutdown, on_ready, target) = (self.on_shutdown.take(), self.on_ready.take(), shared.clone());
        let serve = async move {
            let out = run_target(target, manifest, mount, on_ready).await;
            if let Some(hook) = on_shutdown {
                hook.await;
            }
            out
        };
        match init {
            None => serve.await,
            Some(init) => {
                let name = shared.ctx.env().service.clone();
                let init = async move {
                    let out = init.await;
                    let _ = gate_tx.send(Some(out.as_ref().map(|_| ()).map_err(|e| format!("{e:#}"))));
                    match &out {
                        Ok(()) => tracing::info!("{name}: initialisation complete"),
                        Err(e) => tracing::error!("{name}: initialisation FAILED: {e:#}"),
                    }
                    out
                };
                tokio::try_join!(serve, init).map(|_| ())
            }
        }
    }
}

/// Take the pre-redux snapshot of what the plugin declares, before its first Rust write. A failure
/// refuses the start. Only a declaration is copied: a service dir can hold data that is not
/// pre-redux state, such as kb's LanceDB.
fn snapshot_state(env: &Env, toml: Option<&str>) -> anyhow::Result<()> {
    let Some(text) = toml else { return Ok(()) };
    let decl = awm_statecheck::decl::Decl::parse(text, &env.state_dir(), &env.awm_dir, |k| std::env::var(k).ok())?;
    let out = awm_statecheck::snapshot_once(&env.plugin, &decl).map_err(|e| anyhow::anyhow!("{}: pre-redux snapshot: {e}", env.service))?;
    for p in &out.taken {
        tracing::info!("{}: took the pre-redux snapshot {}", env.service, p.display());
    }
    for n in &out.notes {
        tracing::info!("{}: snapshot: {n}", env.service);
    }
    Ok(())
}

fn env_secs(name: &str, default: f64) -> f64 {
    std::env::var(name).ok().and_then(|v| v.trim().parse().ok()).unwrap_or(default)
}

async fn serve_mount(port: u16, router: axum::Router) -> anyhow::Result<String> {
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await?;
    let url = format!("http://{}", listener.local_addr()?);
    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, router).await {
            tracing::error!("mount stopped: {e}");
        }
    });
    Ok(url)
}

async fn run_target(shared: Arc<Shared>, manifest: Value, mount: Option<String>, mut on_ready: Option<BoxFut<()>>) -> anyhow::Result<()> {
    let name = shared.ctx.env().service.clone();
    let deadline = Duration::from_secs_f64(env_secs("AWM_RECONNECT_DEADLINE_S", 10.0));
    let mut backoff = Duration::from_secs(1);
    // The give-up clock counts from the first failure since the link was last confirmed up.
    // A frame confirms it; a connect the gateway closes at once does not.
    let mut last_up = Instant::now();
    let mut down_since: Option<Instant> = None;
    let mut conn = 0u64;
    loop {
        conn += 1;
        let outcome = async {
            let sid = register(&shared.ctx, mount.as_deref()).await?;
            tracing::info!("{name}: registered service_id={sid}");
            serve(&shared, &sid, &manifest, conn, &mut last_up, &mut on_ready).await
        }
        .await;
        match outcome {
            Ok(()) => {
                last_up = Instant::now();
                backoff = Duration::from_secs(1);
            }
            Err(Stop::GiveUp(why)) => {
                tracing::info!("{name}: giving up: {why}");
                return Ok(());
            }
            Err(Stop::Retry(why)) => {
                let now = Instant::now();
                let since = match down_since {
                    Some(d) if last_up <= d => d,
                    _ => *down_since.insert(now),
                };
                if now.duration_since(since) > deadline {
                    tracing::warn!("{name}: gateway unreachable for >{:.0}s; exiting", deadline.as_secs_f64());
                    return Ok(());
                }
                tracing::warn!("{name}: control WS lost ({why}); retry in {:.1}s", backoff.as_secs_f64());
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(30));
            }
        }
    }
}

async fn register(ctx: &Context, mount: Option<&str>) -> Result<String, Stop> {
    let env = ctx.env();
    let cwd = std::env::current_dir().map(|p| p.display().to_string()).unwrap_or_else(|_| ".".into());
    let mut body = json!({"name": env.service, "pid": std::process::id(), "start": ["bash", "run.sh"], "cwd": cwd});
    if let Some(m) = mount {
        body["mount"] = json!(m);
    }
    let mut req = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| Stop::Retry(e.to_string()))?
        .post(format!("{}/hub/service/register", env.hub_url))
        .json(&body);
    if let Some(token) = ctx.hub().token() {
        req = req.header("X-Awm-Service-Token", token);
    }
    let resp = req.send().await.map_err(|e| Stop::Retry(format!("register: {e}")))?;
    let status = resp.status().as_u16();
    let text = resp.text().await.unwrap_or_default();
    if status == 409 {
        return Err(Stop::GiveUp(format!("register rejected (409): {text}")));
    }
    if !(200..300).contains(&status) {
        return Err(Stop::Retry(format!("register: HTTP {status}: {text}")));
    }
    let data: Value = serde_json::from_str(&text).map_err(|e| Stop::Retry(format!("register reply: {e}")))?;
    if let Some(ws) = data.get("canonical_workspace").and_then(Value::as_str) {
        crate::config::set_canonical_workspace(ws);
    }
    data.get("service_id").and_then(Value::as_str).map(str::to_string).ok_or_else(|| Stop::Retry("register reply has no service_id".into()))
}

fn ws_base(hub_url: &str) -> String {
    hub_url.replacen("https://", "wss://", 1).replacen("http://", "ws://", 1)
}

async fn serve(shared: &Arc<Shared>, sid: &str, manifest: &Value, conn: u64, last_up: &mut Instant, on_ready: &mut Option<BoxFut<()>>) -> Result<(), Stop> {
    use tokio_tungstenite::tungstenite::Error;
    let env = shared.ctx.env();
    let url = format!("{}/hub/service/control/{sid}", ws_base(&env.hub_url));
    let ws = match tokio::time::timeout(Duration::from_secs(10), tokio_tungstenite::connect_async(url.as_str())).await {
        Err(_) => return Err(Stop::Retry("control WS open timed out".into())),
        Ok(Ok((ws, _))) => ws,
        Ok(Err(Error::Http(resp))) if matches!(resp.status().as_u16(), 403 | 409) => {
            return Err(Stop::GiveUp(format!("control WS upgrade rejected ({})", resp.status().as_u16())))
        }
        Ok(Err(e)) => return Err(Stop::Retry(e.to_string())),
    };
    let (mut sink, mut stream) = ws.split();
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let _ = tx.send(json!({"kind": "ready", "api": manifest}).to_string());
    *shared.ctx.0.link.lock().unwrap_or_else(|p| p.into_inner()) = Some((conn, tx.clone()));
    tracing::info!("{}: control WS open, ready sent", env.service);
    if let Some(hook) = on_ready.take() {
        tokio::spawn(hook);
    }
    let writer = tokio::spawn(async move {
        while let Some(text) = rx.recv().await {
            if sink.send(Message::Text(text.into())).await.is_err() {
                break;
            }
        }
    });
    let result = loop {
        let Some(msg) = stream.next().await else { break Ok(()) };
        let msg = match msg {
            Ok(m) => m,
            Err(e) => break Err(Stop::Retry(e.to_string())),
        };
        *last_up = Instant::now();
        let text = match msg {
            Message::Text(t) => t,
            Message::Close(frame) => {
                let code = frame.as_ref().map(|f| u16::from(f.code));
                break match code {
                    Some(c @ (4409 | 4404)) => Err(Stop::GiveUp(format!("control WS closed with code {c}"))),
                    Some(c) if c == u16::from(CloseCode::Normal) || c == u16::from(CloseCode::Away) => Ok(()),
                    _ => Err(Stop::Retry(format!("control WS closed ({code:?})"))),
                };
            }
            _ => continue,
        };
        let Ok(frame) = serde_json::from_str::<Value>(text.as_str()) else { continue };
        match frame.get("kind").and_then(Value::as_str) {
            Some("call") => dispatch_call(shared, frame, tx.clone()),
            Some("cancel") => cancel_call(shared, &frame["id"]),
            Some("notify") => {
                let shared = shared.clone();
                tokio::spawn(async move {
                    let fun = frame.get("fn").and_then(Value::as_str).unwrap_or_default().to_string();
                    if let Err(e) = run_handler(&shared, frame, CancelToken::new()).await {
                        tracing::warn!("{}: notify {fun} failed: {e}", shared.ctx.env().service);
                    }
                });
            }
            Some("session.open") => {
                tokio::spawn(open_session(shared.clone(), sid.to_string(), frame, tx.clone()));
            }
            Some("shutdown") => {
                tracing::info!("{}: shutdown frame received; standing down", env.service);
                break Err(Stop::GiveUp("gateway requested shutdown".into()));
            }
            other => tracing::debug!("{}: ignored inbound kind={other:?}", env.service),
        }
    };
    {
        let mut link = shared.ctx.0.link.lock().unwrap_or_else(|p| p.into_inner());
        if link.as_ref().is_some_and(|(n, _)| *n == conn) {
            *link = None;
        }
    }
    writer.abort();
    result
}

fn call_key(id: &Value) -> String {
    id.as_str().map(str::to_string).unwrap_or_else(|| id.to_string())
}

fn dispatch_call(shared: &Arc<Shared>, frame: Value, tx: mpsc::UnboundedSender<String>) {
    let id = frame.get("id").cloned().unwrap_or(Value::Null);
    let key = call_key(&id);
    let token = CancelToken::new();
    let tracked = !id.is_null();
    // Held while the task spawns, so a call that finishes at once cannot remove its entry first.
    let mut calls = shared.calls.lock().unwrap_or_else(|p| p.into_inner());
    let task = {
        let (shared, token, key) = (shared.clone(), token.clone(), key.clone());
        tokio::spawn(async move {
            let fun = frame.get("fn").and_then(Value::as_str).unwrap_or_default().to_string();
            let reply = match run_handler(&shared, frame, token).await {
                Ok(result) => json!({"kind": "reply", "id": id, "ok": true, "result": result}),
                Err(e) => {
                    tracing::warn!("{}: call {fun} failed: {e}", shared.ctx.env().service);
                    json!({"kind": "reply", "id": id, "ok": false, "error": e.message, "error_class": e.error_class})
                }
            };
            let _ = tx.send(reply.to_string());
            shared.calls.lock().unwrap_or_else(|p| p.into_inner()).remove(&key);
        })
    };
    if tracked {
        calls.insert(key, (task.abort_handle(), token));
    }
}

fn cancel_call(shared: &Shared, id: &Value) {
    let entry = shared.calls.lock().unwrap_or_else(|p| p.into_inner()).remove(&call_key(id));
    if let Some((task, token)) = entry {
        tracing::info!("{}: call {id} cancelled by the caller", shared.ctx.env().service);
        token.cancel();
        task.abort();
    }
}

async fn await_init(shared: &Shared, what: &str) -> Result<(), VerbError> {
    let mut gate = shared.gate.clone();
    let name = &shared.ctx.env().service;
    if gate.borrow().is_none() {
        tracing::info!("{name}: {what} arrived during initialisation; buffering");
        let waited = tokio::time::timeout(shared.init_wait, gate.wait_for(Option::is_some)).await;
        if waited.is_err() {
            return Err(VerbError::runtime(format!("{name} is still initialising after {:.0}s", shared.init_wait.as_secs_f64())));
        }
    }
    let failed = gate.borrow().as_ref().and_then(|r| r.clone().err());
    match failed {
        Some(e) => Err(VerbError::runtime(format!("{name} failed to initialise: {e}"))),
        None => Ok(()),
    }
}

async fn run_handler(shared: &Arc<Shared>, frame: Value, cancel: CancelToken) -> Result<Value, VerbError> {
    let fun = frame.get("fn").and_then(Value::as_str).unwrap_or_default();
    let Some(entry) = shared.verbs.get(fun) else { return Err(VerbError::runtime(format!("unknown function {fun:?}"))) };
    await_init(shared, &format!("call {fun:?}")).await?;
    let args = frame.get("args").cloned().filter(|a| !a.is_null()).unwrap_or_else(|| json!({}));
    let params = entry.spec.get("params").and_then(Value::as_array).cloned().unwrap_or_default();
    let missing: Vec<&Value> = params
        .iter()
        .filter(|p| p.get("required").and_then(Value::as_bool) == Some(true))
        .filter(|p| p.get("name").and_then(Value::as_str).is_some_and(|n| args.get(n).is_none()))
        .collect();
    if !missing.is_empty() {
        let names: Vec<&str> = missing.iter().filter_map(|p| p["name"].as_str()).collect();
        let hints = missing.iter().filter_map(|p| p.get("if_missing").and_then(Value::as_str));
        let msg = std::iter::once(format!("{fun}: missing required argument(s): {}.", names.join(", "))).chain(hints.map(str::to_string)).collect::<Vec<_>>().join(" ");
        return Err(VerbError::value(msg));
    }
    let call = Call {
        id: frame.get("id").cloned().unwrap_or(Value::Null),
        tool: entry.tool.clone(),
        verb: entry.verb.clone(),
        args,
        caller: frame.get("caller").cloned().and_then(|c| serde_json::from_value(c).ok()).unwrap_or_default(),
        as_: frame.get("as").and_then(Value::as_str).map(str::to_string),
        plugin: frame.get("plugin").and_then(Value::as_str).map(str::to_string),
        cancel,
        ctx: shared.ctx.clone(),
    };
    match std::panic::AssertUnwindSafe(async { (entry.handler)(call).await }).catch_unwind().await {
        Ok(out) => out,
        Err(panic) => {
            let why = panic.downcast_ref::<&str>().map(|s| s.to_string()).or_else(|| panic.downcast_ref::<String>().cloned()).unwrap_or_default();
            Err(VerbError::new("Panic", format!("{fun} panicked: {why}")))
        }
    }
}

async fn open_session(shared: Arc<Shared>, sid: String, frame: Value, tx: mpsc::UnboundedSender<String>) {
    let session_id = frame.get("session_id").and_then(Value::as_str).unwrap_or_default().to_string();
    let kind = frame.get("session_kind").and_then(Value::as_str).unwrap_or("call").to_string();
    let opened = |ok: bool, error: Option<String>| {
        let mut f = json!({"kind": "session.opened", "session_id": session_id, "ok": ok});
        if let Some(e) = error {
            f["error"] = json!(e);
        }
        let _ = tx.send(f.to_string());
    };
    let Some(handler) = shared.sessions.get(&kind).cloned() else {
        opened(false, Some(format!("no session handler for kind {kind:?}")));
        return;
    };
    if let Err(e) = await_init(&shared, &format!("session.open {kind:?}")).await {
        opened(false, Some(e.message));
        return;
    }
    opened(true, None);
    let bridge_url = frame
        .get("bridge_id")
        .and_then(Value::as_str)
        .filter(|b| !b.is_empty())
        .map(|b| format!("{}/hub/service/bridge/{sid}/{b}", ws_base(&shared.ctx.env().hub_url)));
    let session = Session {
        session_id: session_id.clone(),
        kind: kind.clone(),
        init: frame.get("init").cloned().filter(|i| !i.is_null()).unwrap_or_else(|| json!({})),
        caller: frame.get("caller").cloned().and_then(|c| serde_json::from_value(c).ok()).unwrap_or_default(),
        as_: frame.get("as").and_then(Value::as_str).map(str::to_string),
        ctx: shared.ctx.clone(),
        bridge_url,
    };
    if let Err(e) = handler(session).await {
        tracing::warn!("{}: session {kind} {session_id} failed: {e:#}", shared.ctx.env().service);
    }
}

/// A service driven in-process, for a plugin's unit tests: calls go straight to the handlers,
/// with any caller and `as` stamp, and emitted frames are kept for the test to read.
pub struct TestService {
    shared: Arc<Shared>,
    frames: mpsc::UnboundedReceiver<String>,
}

impl Service {
    /// Run `on_start`, then hand the service to a test instead of the gateway.
    pub async fn into_test(mut self) -> anyhow::Result<TestService> {
        if let Some(init) = self.on_start.take() {
            init.await?;
        }
        let (tx, frames) = mpsc::unbounded_channel();
        *self.ctx.0.link.lock().unwrap_or_else(|p| p.into_inner()) = Some((0, tx));
        let (_gate_tx, gate) = watch::channel(Some(Ok(())));
        let shared = Arc::new(Shared {
            ctx: self.ctx.clone(),
            verbs: self.verbs.into_iter().map(|v| (v.function.clone(), v)).collect(),
            sessions: self.sessions,
            gate,
            calls: Mutex::new(HashMap::new()),
            init_wait: Duration::from_secs(1),
        });
        Ok(TestService { shared, frames })
    }
}

impl TestService {
    /// Call `verb` (its function name, or its verb name) as a trusted local caller.
    pub async fn call(&self, verb: &str, args: Value) -> Result<Value, VerbError> {
        self.call_as(verb, args, Caller { node: self.shared.ctx.env().node.clone(), ..Caller::default() }, None).await
    }

    /// Call `verb` with the caller frame and `as` stamp the gateway would send.
    pub async fn call_as(&self, verb: &str, args: Value, caller: Caller, as_: Option<&str>) -> Result<Value, VerbError> {
        let function = match self.shared.verbs.get(verb) {
            Some(_) => verb.to_string(),
            None => self.shared.verbs.values().find(|v| v.verb == verb).map(|v| v.function.clone()).unwrap_or_else(|| verb.to_string()),
        };
        let frame = json!({"kind": "call", "id": "test", "fn": function, "args": args, "caller": caller, "as": as_});
        run_handler(&self.shared, frame, CancelToken::new()).await
    }

    /// The emit and usage frames sent since the last read.
    pub fn frames(&mut self) -> Vec<Value> {
        std::iter::from_fn(|| self.frames.try_recv().ok()).filter_map(|t| serde_json::from_str(&t).ok()).collect()
    }

    pub fn context(&self) -> Context {
        self.shared.ctx.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env() -> Env {
        Env {
            hub_url: "http://127.0.0.1:1".into(),
            service: "svc".into(),
            plugin: "svc".into(),
            node: "here".into(),
            awm_dir: PathBuf::from("/tmp"),
            token_file: None,
        }
    }

    #[test]
    fn the_snapshot_copies_only_what_the_plugin_declares() {
        let tmp = tempfile::tempdir().unwrap();
        let env = Env { awm_dir: tmp.path().to_path_buf(), ..env() };
        std::fs::create_dir_all(env.state_dir()).unwrap();
        std::fs::write(env.state_dir().join("data.json"), "{}").unwrap();
        snapshot_state(&env, None).unwrap();
        assert!(!env.state_dir().join(".pre-rust").exists());
        snapshot_state(&env, Some("")).unwrap();
        assert!(env.state_dir().join(".pre-rust/state/data.json").exists());
        snapshot_state(&env, Some("")).unwrap();
    }

    #[test]
    fn manifest_names_each_verb_and_session() {
        let svc = Service::new(env())
            .verb("svc", "echo", "Echo.", json!([{"name": "text", "required": true}]), |c: Call| async move { Ok(c.args) })
            .verb("svc-admin", "echo", "Echo again.", Value::Null, |_c: Call| async move { Ok(1) })
            .hidden()
            .timeout(Duration::from_secs(90))
            .session("pcm", |_s| async move { Ok(()) })
            .api("config", json!({"title": "x"}))
            .verb("svc", "seat", "", json!([]), |_c: Call| async move { Ok(0) })
            .function("seat_fn")
            .published_as("svc_seat_take");
        let m = svc.manifest();
        let seat = &m["functions"][2];
        assert_eq!((seat["name"].as_str(), seat["tool"].as_str()), (Some("seat_fn"), Some("svc_seat_take")));
        assert_eq!(svc.verbs[2].function, "seat_fn");
        assert_eq!(svc.ctx.env().state_dir(), PathBuf::from("/tmp/services/svc"));
        let f = m["functions"].as_array().unwrap();
        assert_eq!((f[0]["name"].as_str(), f[0]["tool"].as_str()), (Some("echo"), Some("svc_echo")));
        assert_eq!((f[1]["name"].as_str(), f[1]["tool"].as_str()), (Some("svc-admin_echo"), Some("svc-admin_echo")));
        assert_eq!(f[1]["surfaces"], json!(["cli", "http"]));
        assert_eq!(f[1]["timeout"], json!(90.0));
        assert!(f[1].get("params").is_none() && f[0]["params"].is_array(), "no params key stays absent");
        assert_eq!(m["sessions"], json!([{"kind": "pcm", "transport": "direct"}]));
        assert_eq!(m["config"]["title"], "x");
        assert!(Service::new(env()).manifest().get("sessions").is_none(), "no sessions key unless declared");
    }

    #[tokio::test]
    async fn handlers_check_required_args_and_catch_panics() {
        let svc = Service::new(env())
            .verb("svc", "need", "", json!([{"name": "x", "required": true, "if_missing": "Ask Tony."}]), |c: Call| async move { c.req::<i64>("x") })
            .verb("svc", "boom", "", json!([]), |_c: Call| async move {
                if true {
                    panic!("kaboom");
                }
                Ok(0)
            });
        let (_tx, gate) = watch::channel(Some(Ok(())));
        let shared = Arc::new(Shared {
            ctx: svc.ctx.clone(),
            verbs: svc.verbs.into_iter().map(|v| (v.function.clone(), v)).collect(),
            sessions: BTreeMap::new(),
            gate,
            calls: Mutex::new(HashMap::new()),
            init_wait: Duration::from_secs(1),
        });
        let frame = |f: &str, args: Value| json!({"kind": "call", "id": "1", "fn": f, "args": args, "caller": {"node": "n", "lane": "local"}});
        let e = run_handler(&shared, frame("need", json!({})), CancelToken::new()).await.unwrap_err();
        assert_eq!(e.error_class, "ValueError");
        assert_eq!(e.message, "need: missing required argument(s): x. Ask Tony.");
        assert_eq!(run_handler(&shared, frame("need", json!({"x": 3})), CancelToken::new()).await.unwrap(), json!(3));
        let e = run_handler(&shared, frame("need", json!({"x": "s"})), CancelToken::new()).await.unwrap_err();
        assert_eq!(e.error_class, "ValueError");
        let e = run_handler(&shared, frame("boom", json!({})), CancelToken::new()).await.unwrap_err();
        assert!(e.error_class == "Panic" && e.message.contains("kaboom"), "{e:?}");
        assert!(run_handler(&shared, frame("nope", json!({})), CancelToken::new()).await.is_err());
    }

    #[test]
    fn call_helpers_read_the_stamps() {
        let call = Call {
            id: json!("1"),
            tool: "t".into(),
            verb: "v".into(),
            args: json!({"a": 1, "_caller_trust": "trusted", "_caller_scope": "awm/x", "_caller_pid": 42, "_caller_session_id": "ccab"}),
            caller: Caller::default(),
            as_: Some("peer:mira".into()),
            plugin: None,
            cancel: CancelToken::new(),
            ctx: Context::new(env()),
        };
        assert!(call.trusted() && call.from_peer());
        assert_eq!((call.caller_scope(), call.caller_pid(), call.caller_session_id(), call.peer_node()), (Some("awm/x"), Some(42), Some("ccab"), Some("mira")));
        assert_eq!(call.declared_args(), json!({"a": 1}));
        assert!(!call.ctx.emit("t", 1), "no control connection, nothing sent");
    }

}
