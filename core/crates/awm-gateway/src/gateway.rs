//! Gateway state and the catalog: which tools exist, how they describe themselves, and dispatch.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock, RwLock, Weak};
use std::time::{Duration, Instant};

use awm_proto::{CallContext, Caller, Lane, Surface, Tier, UsageEvent};
use awm_trust::edges::EdgeList;
use serde_json::{json, Value};

use crate::config::Config;
use crate::hub::{CallError, Hub, Verb};
use crate::isolated::{Holder, TokenCache};
use crate::loader::{self, LoadResult, Owner, RemoteTool};
use crate::peer::{HttpPeers, PeerError, PeerTransport};
use crate::supervisor::Supervisor;
use crate::usage::UsageStore;

pub const DESCRIBE: &str = "describe";
pub const NATIVE_TOOL: &str = "awm";
const NATIVE_VERBS: [(&str, &str); 17] = [
    ("status", "The gateway's status: node, uptime, plugins and services."),
    ("plugins", "Every plugin the loader read: loaded, refused with the reason, and skipped."),
    ("services", "Every service of the loaded plugins and every one the hub knows: status, pid, enabled, tools and mount."),
    ("start", "Start the service `name` (a trusted caller on this node)."),
    ("stop", "Stop the service `name` until it is started again or the gateway restarts."),
    ("restart", "Restart the service `name`: its plugin's refresh hook (routines.md R33) and an operator both use it."),
    ("enable", "Enable the service `name` in <state>/enabled.json and start it."),
    ("disable", "Disable the service `name` in <state>/enabled.json and stop it."),
    ("config_contracts", "Every service config contract a service's ready api opts into, with its live values, for the settings page."),
    ("config_set", "Save `values` to the config contract `key`, through its service's config_set."),
    ("usage", "Usage per plugin and surface over the last `days` days (default 60)."),
    ("table", "The live name table: each tool's owner plugin and node, and each contract's providers."),
    ("reload", "Read the plugin roots and the install config again, with no gateway restart. Starts added plugins and stops removed or disabled ones. It restarts nothing unless `restart` (a list of plugin names) names it, with `force` to bypass the crash breaker. The result lists the running plugins whose `[env]` is stale."),
    ("install", "Install or update plugins from `sources` (a list) or `source`: each a plugin folder or git:<url>@<commit>. All of them load as one change, or none does. A git change asks for Tony's passkey approval first; run it again with `approval`=<request_id> once approved."),
    ("uninstall", "Remove an installed plugin by `name` and stop its services."),
    ("drain", "The deliberate stop (`awm stop`): stand every service down, empty the services journal and end the gateway. A plain restart or SIGTERM leaves the services running for the next gateway to adopt."),
    ("hold_leave", "Leave hold mode, which a gateway enters when it restarts too often: spawn the services that are down and clear the start log."),
];
const DEFAULT_CALL_TIMEOUT_S: f64 = 120.0;
/// Where the workspace keeps each core plugin's pin (plugin-contract § Runtime).
const PLUGIN_PINS: &str = "scripts/plugins";
/// Verbs that drive the calling session's own process. Sent to a peer, one would find no
/// calling session there, so the call is refused before it leaves the node. A singleton's verb
/// that acts on the session goes through the door, which carries the session's identity.
const SESSION_BOUND: [(&str, &[&str]); 1] = [("reflection", &["message_self", "compact", "mode", "whoami"])];
/// Native verbs that change this node, so only a trusted caller here may run them.
const NATIVE_WRITES: [&str; 11] =
    ["reload", "install", "uninstall", "start", "stop", "restart", "enable", "disable", "config_set", "drain", "hold_leave"];
/// How long a singleton's verb list, read through its host's door, stands before it is read
/// again, and how soon a failed read is tried again.
const DOOR_VERBS_TTL: Duration = Duration::from_secs(600);
const DOOR_VERBS_RETRY: Duration = Duration::from_secs(60);

/// One verb as the MCP list shows it: name, summary, and each argument with whether it is required.
type VerbLine = (String, String, Vec<(String, bool)>);

/// A singleton's verbs as its host's door last described them.
#[derive(Default)]
struct DoorVerbs {
    verbs: Option<Vec<VerbLine>>,
    read: Option<(Instant, bool)>,
    reading: bool,
}

pub struct Gateway {
    pub state: PathBuf,
    cfg: RwLock<Arc<Config>>,
    pub node: String,
    pub load: RwLock<LoadResult>,
    pub hub: Hub,
    pub usage: UsageStore,
    pub peers: Arc<dyn PeerTransport>,
    pub url: OnceLock<String>,
    pub stopping: AtomicBool,
    pub started: Instant,
    pub supervisor: Supervisor,
    pub tokens: TokenCache,
    /// The tools each peer serves, refreshed when a peer says its table changed.
    pub peer_tools: RwLock<BTreeMap<String, Vec<RemoteTool>>>,
    reloading: tokio::sync::Mutex<()>,
    /// reflection's `session_of` answers by pid, with when they were fetched.
    sessions: std::sync::Mutex<HashMap<u32, (Instant, Value)>>,
    /// The verbs of each singleton another host serves, for the MCP list.
    door_verbs: std::sync::Mutex<HashMap<String, DoorVerbs>>,
    installing: tokio::sync::Mutex<()>,
    /// Each plugin's install hook runs and failures (plugin-contract § Runtime).
    pub hooks: crate::hooks::Hooks,
    /// Each guarded plugin's egress proxy port (R116 item 1).
    pub egress: crate::egress::Egress,
    /// Emit subscribers by service and topic.
    pub topics: crate::streams::Topics,
    /// Direct sessions waiting for their service's answer or bridge.
    pub stream_sessions: crate::streams::Sessions,
    /// The private marks a call from another node may not reach.
    pub marks: crate::privacy::Marks,
    /// The node's edge list, whose singletons each name their callers (fleet.md R98 item 3).
    pub edges: RwLock<EdgeList>,
    me: Weak<Gateway>,
}

/// What the caller of a reload wants restarted. A reload never restarts a plugin on its own.
#[derive(Debug, Default, Clone)]
pub struct ReloadRequest {
    /// Plugins to restart, even if no file changed.
    pub restart: Vec<String>,
    /// Bypass the crash breaker for the named plugins.
    pub force: bool,
}

impl ReloadRequest {
    /// `restart` is a list or a comma-separated string.
    pub fn from_args(args: &Value) -> Self {
        let names = |v: &Value| -> Vec<String> {
            match v {
                Value::Array(a) => a.iter().filter_map(Value::as_str).map(str::to_string).collect(),
                Value::String(s) => s.split(',').map(str::trim).filter(|n| !n.is_empty()).map(str::to_string).collect(),
                _ => vec![],
            }
        };
        Self { restart: names(args.get("restart").unwrap_or(&Value::Null)), force: args.get("force").and_then(Value::as_bool).unwrap_or(false) }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Scope {
    Everything,
    Needs,
}

/// An error with the HTTP status `/invoke` answers it with.
#[derive(Debug)]
pub struct ApiError {
    pub status: u16,
    pub detail: Value,
}

impl ApiError {
    pub fn new(status: u16, detail: impl Into<String>) -> Self {
        Self { status, detail: Value::String(detail.into()) }
    }

    /// A tool that no plugin serves on this node or a peer (plugin-contract § Calls). A gate
    /// that hides a tool answers the same, so a caller cannot tell hidden from absent.
    pub fn not_installed(tool: &str) -> Self {
        Self::new(404, format!("{tool} not installed"))
    }
}

impl From<CallError> for ApiError {
    fn from(e: CallError) -> Self {
        match &e {
            CallError::NotUp(_) => ApiError::new(503, e.to_string()),
            CallError::Timeout { .. } => ApiError::new(504, e.to_string()),
            CallError::Closed(_) => ApiError::new(502, e.to_string()),
            CallError::Remote { error, error_class } => ApiError {
                status: 500,
                detail: json!({"error": error, "error_class": error_class}),
            },
        }
    }
}

impl From<PeerError> for ApiError {
    fn from(e: PeerError) -> Self {
        match e {
            PeerError::Unknown(_) => ApiError::new(400, e.to_string()),
            PeerError::Unreachable { .. } => ApiError::new(502, e.to_string()),
            PeerError::Refused { status, body, .. } => ApiError {
                status,
                detail: serde_json::from_str::<Value>(&body)
                    .ok()
                    .and_then(|v| v.get("detail").cloned())
                    .unwrap_or(Value::String(body)),
            },
        }
    }
}

/// One public tool as the catalog sees it right now.
#[derive(Debug, Clone)]
pub struct ToolEntry {
    pub name: String,
    pub plugin: String,
    pub tier: Tier,
    /// Ready verbs, with the service that answers each. Empty while the services start.
    pub verbs: Vec<(String, Verb)>,
}

impl Gateway {
    /// Read the install config and load the plugins.
    pub async fn open(state: &Path) -> anyhow::Result<Arc<Self>> {
        let cfg = Config::load(state)?;
        let urls = cfg.peer.iter().map(|p| (p.name.clone(), p.url.clone())).collect();
        Self::open_with(state, cfg, Arc::new(HttpPeers::new(urls))).await
    }

    /// Load the plugins against the cached peer tables and return at once. Each peer's table
    /// is then asked for in the background ([`Gateway::refresh_peers`]), so a peer that does
    /// not answer never holds up the listener.
    pub async fn open_with(state: &Path, cfg: Config, peers: Arc<dyn PeerTransport>) -> anyhow::Result<Arc<Self>> {
        std::fs::create_dir_all(state)?;
        let state = state.canonicalize()?;
        std::fs::create_dir_all(crate::install::installed_root(&state))?;
        let node = cfg.node_name();
        let edges = EdgeList::load(&cfg.edges_path(&state))?;
        let workspace = cfg.workspace_dir(&state);
        let peer_tools = cached_peer_tools(&state, &peers.peers());
        let load = load_plugins(&state, &cfg, &peer_tools, &edges, &node, &BTreeMap::new());
        for r in &load.refused {
            tracing::error!("plugin {} refused: {}", r.plugin, r.reason);
        }
        for w in &load.warnings {
            tracing::warn!("{w}");
        }
        let marks = crate::privacy::Marks::new(&state, &workspace);
        let supervisor = Supervisor::new(&state);
        let gw = Arc::new_cyclic(|me| Self {
            marks,
            edges: RwLock::new(edges),
            usage: UsageStore::new(state.join("usage")),
            state,
            cfg: RwLock::new(Arc::new(cfg)),
            node,
            load: RwLock::new(load),
            hub: Hub::default(),
            peers,
            url: OnceLock::new(),
            stopping: AtomicBool::new(false),
            started: Instant::now(),
            supervisor,
            tokens: TokenCache::default(),
            peer_tools: RwLock::new(peer_tools),
            reloading: tokio::sync::Mutex::new(()),
            sessions: std::sync::Mutex::new(HashMap::new()),
            door_verbs: std::sync::Mutex::new(HashMap::new()),
            installing: tokio::sync::Mutex::new(()),
            hooks: Default::default(),
            egress: Default::default(),
            topics: Default::default(),
            stream_sessions: Default::default(),
            me: me.clone(),
        });
        gw.refresh_peers();
        Ok(gw)
    }

    /// The install config as the last reload read it. `listen` and `isolated_listen` are
    /// read once at start, so a change to them needs a gateway restart.
    pub fn cfg(&self) -> Arc<Config> {
        self.cfg.read().unwrap_or_else(|p| p.into_inner()).clone()
    }

    pub fn weak(&self) -> Weak<Gateway> {
        self.me.clone()
    }

    pub fn stopping(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
    }

    pub fn hub_url(&self) -> String {
        self.url.get().cloned().unwrap_or_else(|| format!("http://{}", self.cfg().listen))
    }

    pub fn read_load(&self) -> std::sync::RwLockReadGuard<'_, LoadResult> {
        self.load.read().unwrap_or_else(|p| p.into_inner())
    }

    /// The plugin a service belongs to. A service the loader does not know is its own plugin.
    /// The file holding the token `service` proves itself with: its external config's, else
    /// the one the supervisor minted for its spawn. None when it has neither.
    pub fn token_file_of(&self, service: &str) -> Option<PathBuf> {
        match self.cfg().external(service) {
            Some(external) => external.token_file.clone(),
            None => Some(crate::supervisor::service_token_path(&self.state, service)).filter(|p| p.exists()),
        }
    }

    /// Whether `token` is the one `service` proves itself with.
    pub fn token_matches(&self, service: &str, token: &str) -> bool {
        let token = token.trim();
        !token.is_empty()
            && self
                .token_file_of(service)
                .and_then(|p| std::fs::read_to_string(p).ok())
                .is_some_and(|expected| expected.trim() == token)
    }

    /// The registered service whose token is `token`, if any.
    pub fn service_of_token(&self, token: &str) -> Option<String> {
        self.hub.services().into_iter().map(|s| s.name).find(|name| self.token_matches(name, token))
    }

    /// Whether `token` names a registered service, or an `[[external]]` one that may register.
    pub fn knows_token(&self, token: &str) -> bool {
        let externals = self.cfg().external.iter().map(|e| e.service.clone()).collect::<Vec<_>>();
        self.hub.services().into_iter().map(|s| s.name).chain(externals).any(|name| self.token_matches(&name, token))
    }

    pub fn plugin_of_service(&self, service: &str) -> String {
        self.read_load()
            .plugins
            .iter()
            .find(|p| p.services.iter().any(|s| s.name == service))
            .map(|p| p.name.clone())
            .unwrap_or_else(|| service.to_string())
    }

    /// The tools a service may publish verbs under.
    pub fn bindings(&self, service: &str) -> Vec<String> {
        let load = self.read_load();
        let mut out: Vec<String> = load
            .plugins
            .iter()
            .flat_map(|p| p.tools.iter())
            .filter(|t| t.services.iter().any(|s| s == service))
            .map(|t| t.name.clone())
            .collect();
        if out.is_empty() {
            out.push(service.to_string());
        }
        out
    }

    pub fn page_dir(&self, plugin: &str, page: &str) -> Option<(PathBuf, String)> {
        self.read_load()
            .plugins
            .iter()
            .find(|p| p.name == plugin)?
            .pages
            .iter()
            .find(|pg| pg.name == page)
            .map(|pg| (pg.dir.clone(), pg.entry.clone()))
    }

    pub fn load_snapshot(&self) -> Value {
        serde_json::to_value(&*self.read_load()).unwrap_or(Value::Null)
    }

    /// Whether the singleton list keeps `tool` from `caller`: a call from another node, or
    /// relayed for one, that the tool's callers leave out (fleet.md R98 item 3).
    pub fn singleton_refuses(&self, tool: &str, caller: &Caller) -> bool {
        let edges = self.edges.read().unwrap_or_else(|p| p.into_inner());
        caller.nodes_behind(&self.node).any(|n| !edges.may_call(n, tool))
    }

    /// The tools this node's plugins serve, as `/hub/table` gives them to `caller`. Another
    /// node reaches a singleton only through its door, so no singleton goes to one.
    pub fn local_table(&self, caller: &Caller) -> Vec<RemoteTool> {
        let edges = self.edges.read().unwrap_or_else(|p| p.into_inner());
        let other_node = caller.nodes_behind(&self.node).next().is_some();
        self.read_load()
            .plugins
            .iter()
            .flat_map(|p| p.tools.iter())
            .filter(|t| !(other_node && edges.singleton(&t.name).is_some()))
            .map(|t| RemoteTool {
                name: t.name.clone(),
                major: t.major,
                plugin: t.plugin.clone(),
                implements: t.implements.iter().map(|(c, m)| format!("{c}@{m}")).collect(),
            })
            .collect()
    }

    fn name_table(&self) -> Vec<loader::Provider> {
        let peers = self.peer_tools.read().unwrap_or_else(|p| p.into_inner());
        let edges = self.edges.read().unwrap_or_else(|p| p.into_inner());
        loader::table(&self.read_load().plugins, &edge_tools(&peers, &edges))
    }

    /// Where `tool` runs when it is a singleton another host serves: its host's door. A node
    /// the singleton does not name as a caller finds no such tool. A `peer` other than the host
    /// is refused, since no edge carries a singleton call (fleet.md R89, R98).
    fn door_route(&self, tool: &str, peer: Option<&str>) -> Result<Option<crate::door::DoorRoute>, ApiError> {
        let edges = self.edges.read().unwrap_or_else(|p| p.into_inner());
        let Some(s) = edges.singleton(tool).filter(|s| s.host != self.node) else { return Ok(None) };
        if !edges.may_call(&self.node, tool) {
            return Err(ApiError::not_installed(tool));
        }
        if peer.is_some_and(|p| p != "local" && p != self.node && p != s.host) {
            return Err(ApiError::new(400, format!(
                "{tool} is a singleton on {host}, reached only through {host}'s singleton door; call it without peer, or with peer={host:?}",
                host = s.host)));
        }
        let Some((address, port)) = edges.door_address(&s.host) else {
            return Err(ApiError::new(502, format!("{tool}: edges.yml gives {}'s singleton door no address on its network", s.host)));
        };
        let user = edges.door_user(&s.host);
        Ok(Some(crate::door::DoorRoute { tool: tool.into(), host: s.host.clone(), address, port, user }))
    }

    /// Who owns a tool name right now (R117 item 3).
    pub fn owner(&self, name: &str) -> Option<Owner> {
        loader::owner(&self.name_table(), name).cloned()
    }

    /// Every provider of a contract at a major right now (R117 item 4).
    pub fn providers(&self, contract: &str, major: u32) -> Vec<Owner> {
        let enabled = self.tools();
        let mut owners = loader::providers(&self.name_table(), contract, major);
        owners.retain(|o| o.peer.is_some() || enabled.contains_key(&o.name));
        owners
    }

    /// The live table as `awm table` shows it to `caller`.
    pub fn table_view(&self, caller: &Caller) -> Value {
        let mut index = self.name_table();
        index.retain(|p| !self.singleton_refuses(&p.owner.name, caller));
        let mut contracts: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        for p in &index {
            for (c, m) in &p.implements {
                contracts.entry(format!("{c}@{m}")).or_default().push(json!(p.owner));
            }
        }
        let mut tools: Vec<Value> = index.iter().map(|p| json!(p.owner)).collect();
        if caller.nodes_behind(&self.node).next().is_none() {
            let edges = self.edges.read().unwrap_or_else(|p| p.into_inner());
            tools.extend(edges.door_singletons(&self.node).iter().map(|s| json!({"name": s.tool, "peer": s.host, "door": true})));
        }
        json!({"tools": tools, "contracts": contracts})
    }

    /// Ask every peer for its table, each in its own task, and reload when one changed. A
    /// peer that does not answer delays no other peer.
    pub fn refresh_peers(self: &Arc<Self>) {
        for peer in self.peers.peers() {
            let gw = self.clone();
            tokio::spawn(async move {
                let before = gw.cached_table_of(&peer);
                if !gw.refresh_peer(&peer).await || gw.cached_table_of(&peer) == before {
                    return;
                }
                if let Err(e) = gw.reload_needs().await {
                    tracing::warn!("peer {peer}: needs after its table: {}", e.detail);
                }
            });
        }
    }

    fn cached_table_of(&self, peer: &str) -> Option<Value> {
        self.peer_tools.read().unwrap_or_else(|p| p.into_inner()).get(peer).map(|t| json!(t))
    }

    /// Ask one peer for its table again, after it said the table changed.
    pub async fn refresh_peer(&self, peer: &str) -> bool {
        if !self.peers.peers().iter().any(|p| p == peer) {
            return false;
        }
        match self.peers.tools(peer).await.map(serde_json::from_value::<Vec<RemoteTool>>) {
            Ok(Ok(tools)) => {
                let mut known = self.peer_tools.write().unwrap_or_else(|p| p.into_inner());
                known.insert(peer.to_string(), tools);
                // Written under the lock, so concurrent refreshes cannot land an older map last.
                save_peer_cache(&self.state, &known);
                true
            }
            Ok(Err(e)) => {
                tracing::warn!("peer {peer}: bad tool list: {e}");
                false
            }
            Err(e) => {
                tracing::warn!("peer {peer}: tool list: {e}");
                false
            }
        }
    }

    /// Who owns `pid`: reflection's `session_of` answer, or `None` when reflection stays down or
    /// silent for `lookup_wait_s`. While it restarts (not registered, or its call fails) the
    /// lookup holds and retries until it answers. Only a definite answer is kept: an `unknown`
    /// mode or a failure is asked again on the next call.
    pub async fn session_of(&self, pid: u32) -> Option<Value> {
        let ttl = Duration::from_secs_f64(self.cfg().session_cache_s);
        if let Some((at, v)) = self.sessions.lock().unwrap_or_else(|p| p.into_inner()).get(&pid) {
            if at.elapsed() < ttl {
                return Some(v.clone());
            }
        }
        let caller = awm_proto::Caller { node: self.node.clone(), lane: Lane::Service, ..Default::default() };
        let deadline = Instant::now() + Duration::from_secs_f64(self.cfg().lookup_wait_s);
        let answer = loop {
            let notified = self.hub.changed.notified();
            let tried = match self.lookup_target() {
                Some((service, verb)) => {
                    self.hub.call(&service, &verb.function, json!({"pid": pid}), None, None, &caller, Duration::from_secs(5)).await.ok()
                }
                None => None,
            };
            if let Some(answer) = tried {
                break answer;
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return None;
            }
            let _ = tokio::time::timeout(left.min(Duration::from_millis(250)), notified).await;
        };
        let answer = match answer {
            Value::String(s) => serde_json::from_str(&s).unwrap_or(Value::String(s)),
            v => v,
        };
        if !ttl.is_zero() && crate::caller::is_definite(&answer) {
            self.sessions.lock().unwrap_or_else(|p| p.into_inner()).insert(pid, (Instant::now(), answer.clone()));
        }
        Some(answer)
    }

    /// The service and verb that answer `session_of`, once reflection is registered and ready.
    fn lookup_target(&self) -> Option<(String, Verb)> {
        let tools = self.tools();
        let entry = tools.get(crate::caller::LOOKUP_TOOL)?;
        entry.verbs.iter().find(|(_, v)| v.verb == crate::caller::LOOKUP_VERB).map(|(s, v)| (s.clone(), v.clone()))
    }

    /// Walk from `pid` up its ancestors to the nearest session a harness claims (`caller.rs`).
    pub async fn resolve_caller(&self, pid: u32) -> crate::caller::Lookup {
        use crate::caller::Lookup;
        {
            let load = self.read_load();
            if !load.plugins.iter().any(|p| p.tools.iter().any(|t| t.name == crate::caller::LOOKUP_TOOL)) {
                // A node that runs no reflection runs no harness, so no pid is a session's. One
                // that should run it may be restarting it: wait for it below.
                if !crate::caller::lookup_expected(&load, &self.cfg()) {
                    return Lookup::NoOwner { mode: None };
                }
            }
        }
        let mut first_mode = None;
        let mut at = Some(pid);
        for hop in 0..crate::caller::MAX_HOPS {
            let Some(p) = at.filter(|p| *p > 1) else { break };
            let Some(answer) = self.session_of(p).await.filter(Value::is_object) else { return Lookup::Unreachable };
            if answer.get("known").and_then(Value::as_bool) == Some(true) {
                let mut answer = answer;
                answer[crate::caller::FOUND_PID] = json!(p);
                return Lookup::Session(answer);
            }
            if hop == 0 {
                first_mode = answer.get("mode").and_then(Value::as_str).map(str::to_string);
            }
            at = crate::caller::ppid_of(p);
        }
        Lookup::NoOwner { mode: first_mode }
    }

    /// Fetch each source and load them as one change. A plugin the loader refuses rolls the
    /// whole change back. A change from a git source loads only on Tony's passkey approval of
    /// its commits (R116 item 2): without `approval` the install asks for one and loads nothing;
    /// with the request id, it loads once the grant names exactly the change's commits.
    pub async fn install(&self, sources: &[String], approval: Option<&str>) -> Result<Value, ApiError> {
        use crate::install::{installed_root, source_of, Pending, Pin, Pins};
        if sources.is_empty() {
            return Err(ApiError::new(400, "install needs `source` or `sources`"));
        }
        let _one_at_a_time = self.installing.lock().await;
        let root = installed_root(&self.state);
        let stage = root.join(format!(".staging-{}", uuid::Uuid::new_v4().simple()));
        let cleanup = |dir: &Path| {
            let _ = std::fs::remove_dir_all(dir);
        };
        let mut fetched = Vec::new();
        let mut needs_approval = false;
        for (i, raw) in sources.iter().enumerate() {
            let source = source_of(raw).map_err(|e| ApiError::new(400, e))?;
            let dest = stage.join(i.to_string());
            let (source, got) = tokio::task::spawn_blocking(move || {
                let got = source.fetch(&dest).map(|f| (dest, f));
                (source, got)
            })
            .await
            .map_err(|e| ApiError::new(500, e.to_string()))?;
            let (dest, got) = got.map_err(|e| {
                cleanup(&stage);
                ApiError::new(400, e)
            })?;
            if let Some(p) = self.read_load().plugins.iter().find(|p| p.name == got.name && !p.dir.starts_with(&root)) {
                cleanup(&stage);
                return Err(ApiError::new(409, format!("plugin {:?} is already loaded from {}", got.name, p.dir.display())));
            }
            if fetched.iter().any(|(_, f, _): &(PathBuf, crate::install::Fetched, String)| f.name == got.name) {
                cleanup(&stage);
                return Err(ApiError::new(400, format!("plugin {:?} appears twice in one change", got.name)));
            }
            needs_approval |= source.needs_approval();
            fetched.push((dest, got, source.spec()));
        }
        if needs_approval {
            let pins = Pins::load(&self.state);
            let change: Vec<Pending> = fetched
                .iter()
                .map(|(_, f, spec)| Pending {
                    plugin: f.name.clone(),
                    source: spec.clone(),
                    commit: f.commit.clone().unwrap_or_default(),
                    from: pins.plugin.iter().find(|p| p.name == f.name).and_then(|p| p.commit.clone()).filter(|c| f.commit.as_ref() != Some(c)),
                })
                .collect();
            let approved = self.approve(&change, approval).await;
            if !matches!(approved, Ok(None)) {
                cleanup(&stage);
                return approved.map(|asked| asked.unwrap_or_default());
            }
        }
        let mut moved = Vec::new();
        for (dest, f, _) in &fetched {
            let live = root.join(&f.name);
            let old = root.join(format!(".old-{}", f.name));
            cleanup(&old);
            let had = live.exists() && std::fs::rename(&live, &old).is_ok();
            std::fs::rename(dest, &live).map_err(|e| ApiError::new(500, format!("{}: {e}", live.display())))?;
            moved.push((f.name.clone(), live, old, had));
        }
        cleanup(&stage);
        // The install names what it replaced: those plugins restart, and no other.
        let replaced = ReloadRequest { restart: moved.iter().filter(|m| m.3).map(|m| m.0.clone()).collect(), force: false };
        let report = self.reload(&replaced).await?;
        let refused: Vec<String> = report["refused"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|r| moved.iter().any(|(n, ..)| r["plugin"] == json!(n)))
            .map(|r| format!("{}: {}", r["plugin"].as_str().unwrap_or("?"), r["reason"].as_str().unwrap_or("?")))
            .collect();
        if !refused.is_empty() {
            for (_, live, old, had) in &moved {
                cleanup(live);
                if *had {
                    let _ = std::fs::rename(old, live);
                }
            }
            self.reload(&replaced).await?;
            return Err(ApiError::new(409, format!("install refused, nothing changed: {}", refused.join("; "))));
        }
        let mut pins = Pins::load(&self.state);
        let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        for ((name, _, old, _), (_, f, spec)) in moved.iter().zip(&fetched) {
            cleanup(old);
            pins.set(Pin { name: name.clone(), source: spec.clone(), commit: f.commit.clone(), image: None, installed_at: now.clone() });
        }
        pins.save(&self.state).map_err(|e| ApiError::new(500, e.to_string()))?;
        Ok(json!({"installed": moved.iter().map(|(n, ..)| n).collect::<Vec<_>>(), "reload": report}))
    }

    /// `Ok(None)` when `approval` approves `change`. Without one, file a `plugin_commits`
    /// request and answer what to do next.
    async fn approve(&self, change: &[crate::install::Pending], approval: Option<&str>) -> Result<Option<Value>, ApiError> {
        use crate::install::{check_grant, commits_of, grant_key, request_payload, APPROVER_TOOL, COMMITS_KIND};
        let cfg = self.cfg();
        let key = grant_key(&cfg.secrets_path(&self.state), &cfg.env).map_err(|e| ApiError::new(403, e))?;
        let ctx = CallContext { plugin: Some(NATIVE_TOOL.into()), ..CallContext::local(self.node.clone(), Surface::Plugin) };
        let call = |verb: &'static str, args: Value| {
            let ctx = &ctx;
            async move {
                // Boxed: install is itself a verb of the native tool, so this call recurses.
                let text = Box::pin(self.invoke(APPROVER_TOOL, json!({"verb": verb, "args": args}), ctx)).await.map_err(|e| {
                    ApiError::new(e.status, format!("{APPROVER_TOOL} {verb}: {}", e.detail.as_str().map_or_else(|| e.detail.to_string(), str::to_string)))
                })?;
                Ok::<Value, ApiError>(serde_json::from_str(&text).unwrap_or(Value::String(text)))
            }
        };
        let Some(request_id) = approval else {
            let names: Vec<&str> = change.iter().map(|p| p.plugin.as_str()).collect();
            let asked = call("approval_request", json!({
                "kind": COMMITS_KIND, "node": self.node, "reason": format!("install {} on {}", names.join(", "), self.node),
                "payload": request_payload(change)})).await?;
            let id = asked.get("request_id").cloned().unwrap_or(Value::Null);
            return Ok(Some(json!({
                "installed": [], "approval": asked, "commits": commits_of(change),
                "next": format!("Approve request {} on the passkey panel, then run the same install again with approval={}",
                                id.as_str().unwrap_or("?"), id.as_str().unwrap_or("<request_id>")),
            })));
        };
        let answer = call("approval_get", json!({"request_id": request_id})).await?;
        check_grant(change, &answer, &key, &self.node, chrono::Utc::now().timestamp()).map_err(|e| ApiError::new(403, format!("request {request_id}: {e}")))?;
        Ok(None)
    }

    /// Remove an installed plugin: its folder, its pin and its services.
    pub async fn uninstall(&self, name: &str) -> Result<Value, ApiError> {
        use crate::install::{installed_root, Pins};
        let _one_at_a_time = self.installing.lock().await;
        let mut pins = Pins::load(&self.state);
        if !pins.plugin.iter().any(|p| p.name == name) {
            return Err(ApiError::new(404, format!("no installed plugin {name:?}")));
        }
        let dir = installed_root(&self.state).join(name);
        if dir.exists() {
            std::fs::remove_dir_all(&dir).map_err(|e| ApiError::new(500, format!("{}: {e}", dir.display())))?;
        }
        pins.plugin.retain(|p| p.name != name);
        pins.save(&self.state).map_err(|e| ApiError::new(500, e.to_string()))?;
        Ok(json!({"uninstalled": name, "reload": self.reload(&ReloadRequest::default()).await?}))
    }

    /// Load the plugins again and apply the difference, with no gateway restart (R144).
    /// The caller decides what restarts: a plain reload starts the plugins that were added or
    /// enabled and stops the ones that were removed or disabled. A changed folder restarts
    /// nothing. `request.restart` names the plugins to restart as well.
    pub async fn reload(&self, request: &ReloadRequest) -> Result<Value, ApiError> {
        self.apply_load(request, Scope::Everything).await
    }

    /// A peer's table changed: start the plugins whose `needs` now resolve and stop those whose
    /// `needs` no longer do. It reads no config, restarts nothing and tells no peer.
    pub async fn reload_needs(&self) -> Result<Value, ApiError> {
        self.apply_load(&ReloadRequest::default(), Scope::Needs).await
    }

    async fn apply_load(&self, request: &ReloadRequest, scope: Scope) -> Result<Value, ApiError> {
        let Some(gw) = self.me.upgrade() else { return Err(ApiError::new(503, "the gateway is stopping")) };
        if self.stopping() {
            return Err(ApiError::new(503, "the gateway is stopping"));
        }
        let _one_at_a_time = self.reloading.lock().await;
        let old_cfg = self.cfg();
        let cfg = match scope {
            Scope::Everything => {
                let cfg = Arc::new(Config::load(&self.state).map_err(|e| ApiError::new(400, e.to_string()))?);
                *self.cfg.write().unwrap_or_else(|p| p.into_inner()) = cfg.clone();
                cfg
            }
            Scope::Needs => self.cfg(),
        };
        if scope == Scope::Everything {
            let edges = EdgeList::load(&cfg.edges_path(&self.state)).map_err(|e| ApiError::new(400, format!("{e:#}")))?;
            *self.edges.write().unwrap_or_else(|p| p.into_inner()) = edges;
        }
        // A restart names a plugin to try again, its failed install hook included.
        self.hooks.forget(&request.restart);
        let peers = self.peer_tools.read().unwrap_or_else(|p| p.into_inner()).clone();
        let failed = self.hooks.failed();
        let mut new = load_plugins(&self.state, &cfg, &peers, &self.edges.read().unwrap_or_else(|p| p.into_inner()), &self.node, &failed);
        let (old, was_refused, old_stamps): (BTreeMap<String, loader::Plugin>, BTreeSet<String>, BTreeMap<String, String>) = {
            let before = self.read_load();
            (
                before.plugins.iter().map(|p| (p.name.clone(), p.clone())).collect(),
                before.refused.iter().map(|r| r.plugin.clone()).collect(),
                before.stamps.clone(),
            )
        };

        if scope == Scope::Needs {
            // Only a refusal can come from `needs`. Every other difference on disk waits for a reload.
            let now_refused: BTreeSet<&str> = new.refused.iter().map(|r| r.plugin.as_str()).collect();
            new.plugins.retain(|p| old.contains_key(&p.name) || was_refused.contains(&p.name));
            for (name, o) in &old {
                if !new.plugins.iter().any(|p| &p.name == name) && !now_refused.contains(name.as_str()) {
                    new.plugins.push(o.clone());
                    if let Some(v) = old_stamps.get(name) {
                        new.stamps.insert(name.clone(), v.clone());
                    }
                }
            }
        }
        let fresh: BTreeMap<String, &loader::Plugin> = new.plugins.iter().map(|p| (p.name.clone(), p)).collect();

        let added: Vec<String> = fresh.keys().filter(|n| !old.contains_key(*n)).cloned().collect();
        let removed: Vec<String> = old.keys().filter(|n| !fresh.contains_key(*n)).cloned().collect();
        let (restarted, unknown): (Vec<String>, Vec<String>) =
            request.restart.iter().cloned().partition(|n| old.contains_key(n) && fresh.contains_key(n));

        // A service is supervised when it is enabled and not `[[external]]`: its own unit runs the others.
        let services = |plugins: &BTreeMap<String, &loader::Plugin>, cfg: &Config| -> BTreeMap<String, bool> {
            plugins.values().flat_map(|p| p.services.iter().map(|s| (s.name.clone(), s.enabled && cfg.external(&s.name).is_none()))).collect()
        };
        let old_refs: BTreeMap<String, &loader::Plugin> = old.iter().map(|(k, v)| (k.clone(), v)).collect();
        let (was, now) = (services(&old_refs, &old_cfg), services(&fresh, &cfg));
        let restarting: BTreeSet<&str> =
            restarted.iter().flat_map(|n| old[n].services.iter().map(|s| s.name.as_str())).collect();
        // An external service restarts after its plugin's install hooks, in `install_external`.
        let stopping: Vec<String> = was
            .iter()
            .filter(|(n, was_on)| (restarting.contains(n.as_str()) && cfg.external(n).is_none()) || (**was_on && now.get(*n) != Some(&true)))
            .map(|(n, _)| n.clone())
            .collect();
        let starting: Vec<loader::ServiceSpec> = fresh
            .values()
            .flat_map(|p| p.services.iter())
            .filter(|s| now.get(&s.name) == Some(&true) && (restarting.contains(s.name.as_str()) || was.get(&s.name) != Some(&true)))
            .cloned()
            .collect();

        let externals: Vec<String> = fresh
            .values()
            .filter(|p| p.services.iter().any(|s| s.enabled && cfg.external(&s.name).is_some()))
            .map(|p| p.name.clone())
            .collect();
        if !request.restart.is_empty() || !stopping.is_empty() || !starting.is_empty() {
            tracing::info!(
                "reload: restart {:?} (force {}), stop {stopping:?}, start {:?}",
                request.restart, request.force, starting.iter().map(|s| &s.name).collect::<Vec<_>>()
            );
        }
        crate::supervisor::stop(&gw, &stopping).await;
        for s in &stopping {
            if cfg.external(s).is_none() {
                self.hub.forget(s);
            }
        }
        for r in &new.refused {
            tracing::error!("plugin {} refused: {}", r.plugin, r.reason);
        }
        let refused: Vec<Value> = new.refused.iter().map(|r| json!({"plugin": r.plugin, "reason": r.reason})).collect();
        let changes = {
            let mut load = self.load.write().unwrap_or_else(|p| p.into_inner());
            let changes = crate::lifecycle::diff(&load, &new);
            *load = new;
            changes
        };
        self.hub.changed.notify_waiters();
        for p in &externals {
            crate::supervisor::install_external(&gw, p, restarted.contains(p));
        }
        for spec in starting.clone() {
            if request.force && restarting.contains(spec.name.as_str()) {
                crate::supervisor::force_start(&gw, spec);
            } else {
                crate::supervisor::start(&gw, spec);
            }
        }
        for change in &changes {
            tracing::info!("plugin {} {:?} ({})", change.plugin, change.event, change.version);
            let payload = serde_json::to_value(change).unwrap_or(Value::Null);
            self.topics.publish(self, crate::lifecycle::SERVICE, crate::lifecycle::TOPIC, &payload);
        }
        let stale_env = self.stale_env(&cfg.env);
        let tell_peers = scope == Scope::Everything && !(added.is_empty() && removed.is_empty() && restarted.is_empty() && stopping.is_empty());
        if tell_peers {
            tracing::info!("reload: added {added:?}, removed {removed:?}, restarted {restarted:?}");
            let peers = self.peers.clone();
            let node = self.node.clone();
            tokio::spawn(async move {
                for peer in peers.peers() {
                    if let Err(e) = peers.table_changed(&peer, &node).await {
                        tracing::warn!("peer {peer}: table change not delivered: {e}");
                    }
                }
            });
        }
        Ok(json!({
            "added": added, "removed": removed, "restarted": restarted, "unknown": unknown, "refused": refused,
            "stopped": stopping, "started": starting.iter().map(|s| s.name.clone()).collect::<Vec<_>>(),
            "stale_env": stale_env,
        }))
    }

    /// The running plugins that carry an `[env]` older than the config's: each restarts to take it.
    fn stale_env(&self, env: &BTreeMap<String, String>) -> Vec<String> {
        let ran = self.supervisor.env_of_running();
        let load = self.read_load();
        load.plugins
            .iter()
            .filter(|p| p.services.iter().any(|s| ran.get(&s.name).is_some_and(|e| e != env)))
            .map(|p| p.name.clone())
            .collect()
    }

    /// Every public tool: declared by a loaded plugin, or published by a service the loader
    /// does not know, plus the native `awm` tool.
    pub fn tools(&self) -> BTreeMap<String, ToolEntry> {
        let mut out: BTreeMap<String, ToolEntry> = BTreeMap::new();
        out.insert(
            NATIVE_TOOL.into(),
            ToolEntry { name: NATIVE_TOOL.into(), plugin: NATIVE_TOOL.into(), tier: Tier::On, verbs: vec![] },
        );
        for p in &self.read_load().plugins {
            let enabled: Vec<&str> = p.services.iter().filter(|s| s.enabled).map(|s| s.name.as_str()).collect();
            for t in &p.tools {
                if !t.services.iter().any(|s| enabled.contains(&s.as_str())) {
                    continue;
                }
                out.entry(t.name.clone()).or_insert_with(|| ToolEntry {
                    name: t.name.clone(),
                    plugin: t.plugin.clone(),
                    tier: t.tier,
                    verbs: vec![],
                });
            }
        }
        for (service, verb) in self.hub.verbs() {
            let entry = out.entry(verb.tool.clone()).or_insert_with(|| ToolEntry {
                name: verb.tool.clone(),
                plugin: self.hub.plugin_of(&service).unwrap_or_else(|| service.clone()),
                tier: Tier::On,
                verbs: vec![],
            });
            entry.verbs.push((service, verb));
        }
        for e in out.values_mut() {
            e.verbs.sort_by(|a, b| a.1.verb.cmp(&b.1.verb));
        }
        out
    }

    fn peer_names(&self) -> Vec<String> {
        let mut v = vec!["local".to_string()];
        v.extend(self.peers.peers());
        v
    }

    /// Each verb with its summary and arguments. On `mcp`, a verb whose `surfaces`
    /// leaves out `mcp`, such as a hook's, is left out. The CLI and HTTP keep every verb.
    fn verb_names(&self, entry: &ToolEntry, mcp: bool) -> Vec<VerbLine> {
        let listed = entry.verbs.iter().filter(|(_, vb)| !mcp || on_mcp(&vb.spec));
        let own = listed.map(|(_, vb)| (vb.verb.clone(), summary(&vb.spec), args(&vb.spec)));
        if entry.name == NATIVE_TOOL {
            let mut v: Vec<_> = NATIVE_VERBS.iter().map(|(n, d)| (n.to_string(), d.to_string(), vec![])).collect();
            v.extend(own);
            return v;
        }
        own.collect()
    }

    /// `GET /tools?view=domains`: one `{verb, args, peer}` tool per public tool on the MCP surface.
    pub fn domain_tools(&self) -> Vec<Value> {
        self.domain_tools_for(None)
    }

    /// The domain tools, cut to a holder's whitelist when one is given. A holder sees no peers.
    /// A singleton another host serves lists with the verbs its door last described.
    pub fn domain_tools_for(&self, holder: Option<&Holder>) -> Vec<Value> {
        let peers = if holder.is_some() { vec!["local".to_string()] } else { self.peer_names() };
        let tools = self.tools();
        let mut out: Vec<Value> = tools
            .values()
            .filter(|e| e.tier == Tier::On && holder.is_none_or(|h| h.may_use(&e.name)))
            .map(|e| {
                let mut verbs = self.verb_names(e, true);
                if let Some(h) = holder {
                    verbs.retain(|(v, _, _)| h.may(&e.name, v));
                }
                let runs = if peers.len() > 1 {
                    format!("Runs on this node by default; also on {} via peer=<name>.", peers[1..].join(", "))
                } else {
                    "Runs on this node only.".to_string()
                };
                let peer = json!({"type": "string", "enum": peers,
                                  "description": format!("Optional. Default: local (this node, {}). Valid: {}.", self.node, peers.join(", "))});
                domain_tool(&e.name, Some(verbs), &runs, Some(peer))
            })
            .collect();
        let doors: Vec<(String, String)> = {
            let edges = self.edges.read().unwrap_or_else(|p| p.into_inner());
            edges.door_singletons(&self.node).iter().map(|s| (s.tool.clone(), s.host.clone())).collect()
        };
        for (tool, host) in doors {
            if tools.contains_key(&tool) || holder.is_some_and(|h| !h.may_use(&tool)) {
                continue;
            }
            let mut verbs = self.door_verbs_of(&tool);
            if let (Some(h), Some(v)) = (holder, verbs.as_mut()) {
                v.retain(|(name, _, _)| h.may(&tool, name));
            }
            let runs = format!("A singleton on {host}: every call goes through {host}'s singleton door.");
            out.push(domain_tool(&tool, verbs, &runs, None));
        }
        out.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        out
    }

    /// The verbs `tool`'s door last described, or None before the first answer. A stale or
    /// missing list is read again in the background, and the MCP proxy's poll sees the change.
    fn door_verbs_of(&self, tool: &str) -> Option<Vec<VerbLine>> {
        let mut cache = self.door_verbs.lock().unwrap_or_else(|p| p.into_inner());
        let entry = cache.entry(tool.to_string()).or_default();
        let stale = entry.read.is_none_or(|(at, ok)| at.elapsed() >= if ok { DOOR_VERBS_TTL } else { DOOR_VERBS_RETRY });
        if stale && !entry.reading {
            if let (Ok(rt), Some(gw)) = (tokio::runtime::Handle::try_current(), self.me.upgrade()) {
                entry.reading = true;
                let tool = tool.to_string();
                rt.spawn(async move { gw.read_door_verbs(tool).await });
            }
        }
        entry.verbs.clone()
    }

    async fn read_door_verbs(&self, tool: String) {
        let read = async {
            let route = self.door_route(&tool, None).ok().flatten()?;
            let caller = Caller { node: self.node.clone(), lane: Lane::Service, trust: Some(crate::caller::TRUSTED.into()), ..Caller::default() };
            let request = crate::door::request(tool.clone(), json!({"verb": DESCRIBE, "args": {}}), &caller, &self.node);
            let text = crate::door::call(&route, &request).await.map_err(|e| tracing::warn!("{tool}: verbs not read through the door: {e}")).ok()?;
            serde_json::from_str::<Value>(&text).ok().map(|v| described_verbs(&v))
        }
        .await;
        let mut cache = self.door_verbs.lock().unwrap_or_else(|p| p.into_inner());
        let entry = cache.entry(tool).or_default();
        entry.reading = false;
        entry.read = Some((Instant::now(), read.is_some()));
        if read.is_some() {
            entry.verbs = read;
        }
    }

    /// `GET /tools`: one tool per verb, named `<tool>_<verb>`, for the CLI. Includes tier-off tools.
    pub fn flat_tools(&self) -> Vec<Value> {
        let mut out = Vec::new();
        for e in self.tools().values() {
            for (verb, desc, _) in self.verb_names(e, false) {
                let spec = e.verbs.iter().find(|(_, v)| v.verb == verb).map(|(_, v)| v.spec.clone()).unwrap_or(Value::Null);
                out.push(json!({
                    "name": format!("{}_{verb}", e.name),
                    "description": desc,
                    "inputSchema": input_schema(&spec),
                    "tier": if e.tier == Tier::On { "on" } else { "off" },
                }));
            }
        }
        out
    }

    fn describe(&self, entry: &ToolEntry, filter: Option<&Value>, mcp: bool) -> Value {
        let wanted: Option<Vec<&str>> = filter.and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).collect());
        let mut verbs = Vec::new();
        for (verb, desc, _) in self.verb_names(entry, mcp) {
            if wanted.as_ref().is_some_and(|w| !w.contains(&verb.as_str())) {
                continue;
            }
            let spec = entry.verbs.iter().find(|(_, v)| v.verb == verb).map(|(_, v)| v.spec.clone()).unwrap_or(Value::Null);
            let params: Vec<Value> = spec
                .get("params")
                .and_then(Value::as_array)
                .map(|ps| {
                    ps.iter()
                        .map(|p| {
                            let mut d = json!({"name": p.get("name"), "required": p.get("required").and_then(Value::as_bool).unwrap_or(false)});
                            for key in ["type", "description", "default"] {
                                if let Some(v) = p.get(key).filter(|v| !(key == "type" && v.as_str() == Some("string"))) {
                                    d[key] = v.clone();
                                }
                            }
                            d
                        })
                        .collect()
                })
                .unwrap_or_default();
            let mut v = json!({"verb": verb, "description": desc, "params": params});
            if let Some(surfaces) = spec.get("surfaces") {
                v["surfaces"] = surfaces.clone();
            }
            verbs.push(v);
        }
        json!({"tool": entry.name, "plugin": entry.plugin, "verbs": verbs})
    }

    fn overview(&self, mcp: bool, caller: &Caller) -> Value {
        let tools: Vec<Value> = self
            .tools()
            .values()
            .filter(|e| !self.singleton_refuses(&e.name, caller))
            .map(|e| {
                json!({"tool": e.name, "plugin": e.plugin, "tier": if e.tier == Tier::On {"on"} else {"off"},
                       "verbs": self.verb_names(e, mcp).into_iter().map(|(v, s, a)| {
                           let pick = |want| a.iter().filter(|(_, r)| *r == want).map(|(n, _)| n).collect::<Vec<_>>();
                           json!({"verb": v, "summary": one_line(&s), "required": pick(true), "optional": pick(false)})
                       }).collect::<Vec<_>>()})
            })
            .collect();
        json!({"node": self.node, "tools": tools,
               "how": "Call <tool>(verb=\"describe\") for a tool's params; add peer=<node> to run a verb on another node."})
    }

    async fn native(&self, verb: &str, args: &Value, ctx: &CallContext) -> Result<Value, ApiError> {
        match verb {
            "table" => Ok(self.table_view(&ctx.caller)),
            _ if NATIVE_WRITES.contains(&verb) => {
                let trusted = ctx.caller.trust.as_deref() == Some(crate::caller::TRUSTED);
                if ctx.caller.lane != Lane::Local || !trusted {
                    return Err(ApiError::new(403, format!("{verb} takes a trusted caller on this node")));
                }
                let name = || args.get("name").and_then(Value::as_str).filter(|n| !n.is_empty()).map(str::to_string);
                match verb {
                    "reload" => self.reload(&ReloadRequest::from_args(args)).await,
                    "start" | "stop" | "restart" | "enable" | "disable" => {
                        let Some(name) = name() else { return Err(ApiError::new(400, format!("{verb} needs `name`"))) };
                        self.service_control(verb, &name).await
                    }
                    "drain" => self.drain(),
                    "hold_leave" => self.hold_leave(),
                    "config_set" => {
                        let key = args.get("key").and_then(Value::as_str).unwrap_or_default();
                        self.config_set(key, args.get("values").cloned().unwrap_or_else(|| json!({}))).await
                    }
                    "install" => {
                        let mut sources: Vec<String> = args
                            .get("sources")
                            .and_then(Value::as_array)
                            .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
                            .unwrap_or_default();
                        sources.extend(args.get("source").and_then(Value::as_str).map(str::to_string));
                        self.install(&sources, args.get("approval").and_then(Value::as_str)).await
                    }
                    _ => {
                        let name = args.get("name").and_then(Value::as_str).unwrap_or_default();
                        self.uninstall(name).await
                    }
                }
            }
            "status" => Ok(self.status()),
            "plugins" => Ok(self.load_snapshot()),
            "services" => Ok(self.services_view()),
            "config_contracts" => Ok(self.config_contracts().await),
            "usage" => {
                let days = args.get("days").and_then(Value::as_i64).unwrap_or(crate::usage::WINDOW_DAYS);
                Ok(json!(self.usage.tally(days)))
            }
            _ => Err(ApiError::new(404, format!("Unknown verb {verb:?} for domain {NATIVE_TOOL:?}"))),
        }
    }

    /// `<workspace>/projects`. The workspace defaults to the parent of the state dir, `<workspace>/.awm`.
    pub fn projects_dir(&self) -> PathBuf {
        self.cfg().workspace_dir(&self.state).join("projects")
    }

    pub fn status(&self) -> Value {
        let load = self.read_load();
        json!({
            "status": "ok",
            "node": self.node,
            "version": env!("CARGO_PKG_VERSION"),
            "core_pid": std::process::id(),
            "core_uptime_s": self.started.elapsed().as_secs(),
            "plugins": load.plugins.len(),
            "refused": load.refused.len(),
            "services": self.hub.services().len(),
            "connected": self.hub.connected(),
            "hold": self.supervisor.held(),
            "tripped": self.supervisor.failed(),
        })
    }

    /// Wait for `tool`'s `verb` to be served, up to the configured wait (R117 item 7).
    async fn wait_for_verb(&self, tool: &str, verb: &str) -> Result<(String, Verb), ApiError> {
        let deadline = Instant::now() + Duration::from_secs_f64(self.cfg().tool_wait_s);
        loop {
            let notified = self.hub.changed.notified();
            let tools = self.tools();
            let Some(entry) = tools.get(tool) else {
                return Err(ApiError::not_installed(tool));
            };
            if let Some(found) = entry.verbs.iter().find(|(_, v)| v.verb == verb) {
                return Ok(found.clone());
            }
            if self.tool_settled(tool) {
                // The service may have turned ready since the snapshot above.
                if let Some(found) = self.tools().get(tool).and_then(|e| e.verbs.iter().find(|(_, v)| v.verb == verb).cloned()) {
                    return Ok(found);
                }
                return Err(ApiError::new(404, format!("Unknown verb {verb:?} for domain {tool:?}")));
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err(ApiError::new(503, format!("tool {tool:?} is not up after {:.0}s", self.cfg().tool_wait_s)));
            }
            let _ = tokio::time::timeout(left.min(Duration::from_millis(500)), notified).await;
        }
    }

    /// True when every enabled service behind `tool` has sent its `ready` (or was refused).
    fn tool_settled(&self, tool: &str) -> bool {
        let load = self.read_load();
        let services: Vec<String> = load
            .plugins
            .iter()
            .flat_map(|p| p.tools.iter().filter(|t| t.name == tool).flat_map(|t| t.services.clone()))
            .filter(|s| load.plugins.iter().flat_map(|p| &p.services).any(|svc| &svc.name == s && svc.enabled))
            .collect();
        drop(load);
        services.iter().all(|s| {
            matches!(self.hub.status(s), Some(crate::hub::Status::Ready) | Some(crate::hub::Status::Refused))
        })
    }

    /// `POST /invoke`. `name` is a tool with a `{verb, args, peer}` envelope, or a flat `<tool>_<verb>`.
    pub async fn invoke(&self, name: &str, args: Value, ctx: &CallContext) -> Result<String, ApiError> {
        let tools = self.tools();
        let (tool, verb, inner, peer) = if tools.contains_key(name) || !name.contains('_') {
            let verb = args.get("verb").and_then(Value::as_str).map(str::to_string);
            let Some(verb) = verb else { return Err(ApiError::new(400, format!("{name}: missing 'verb'"))) };
            let inner = args.get("args").cloned().unwrap_or_else(|| json!({}));
            let peer = match args.get("peer") {
                None | Some(Value::Null) => None,
                Some(Value::String(p)) => Some(p.clone()),
                Some(_) => return Err(ApiError::new(400, "'peer' must be a node name string")),
            };
            (name.to_string(), verb, inner, peer)
        } else {
            let (t, v) = name.split_once('_').expect("checked above");
            (t.to_string(), v.to_string(), args, None)
        };
        if !inner.is_object() {
            return Err(ApiError::new(400, "'args' must be an object"));
        }
        // Checked before the call is relayed, so a node carries no call its origin may not make.
        if self.singleton_refuses(&tool, &ctx.caller) {
            tracing::info!("singleton gate: refused {tool}.{verb} to {:?}", ctx.caller.nodes_behind(&self.node).collect::<Vec<_>>());
            return Err(ApiError::not_installed(&tool));
        }
        let door = if tools.contains_key(&tool) { None } else { self.door_route(&tool, peer.as_deref())? };
        // A tool no local plugin serves runs where the name table says it lives (R117 item 3).
        let peer = match &door {
            Some(route) => Some(route.host.clone()),
            None => peer.or_else(|| if tools.contains_key(&tool) { None } else { self.owner(&tool).and_then(|o| o.peer) }),
        };
        // A restricted session may not run a verb on another node, named or by the table.
        let remote = peer.as_deref().filter(|p| *p != "local" && *p != self.node);
        if let Some(why) = crate::modes::refusal(ctx.mode.as_deref(), &tool, &verb, remote) {
            tracing::info!("mode gate: refused {tool}.{verb}: {why}");
            return Err(ApiError::new(403, why));
        }
        if let Some(why) = self.level_refusal(&tool, &verb, &inner, ctx) {
            tracing::info!("level gate: refused {tool}.{verb}: {why}");
            return Err(ApiError::new(403, why));
        }
        let started = Instant::now();
        let routed = peer.filter(|p| p != "local" && *p != self.node);
        // A routed call names no local plugin, so its tool stands in.
        let plugin = if routed.is_some() { Some(tool.clone()) } else { tools.get(&tool).map(|e| e.plugin.clone()) };
        let result = match routed {
            Some(peer) => {
                if SESSION_BOUND.iter().any(|(t, vs)| *t == tool && vs.contains(&verb.as_str())) {
                    return Err(ApiError::new(400, format!(
                        "{tool}.{verb} acts on your own session on this node, so it cannot run on peer {peer:?}; call it without peer")));
                }
                // The far node takes a session claim in the args as this node's word, so a
                // session's own call carries none: its identity rides in the caller this node stamps.
                let from_session = ctx.caller.lane == Lane::Local && ctx.caller.session.is_some();
                let envelope = json!({"verb": verb, "args": if from_session { strip_stamps(&inner) } else { inner }});
                match door {
                    Some(route) => {
                        let request = crate::door::request(tool.clone(), envelope, &ctx.caller, &self.node);
                        crate::door::call(&route, &request).await.map_err(ApiError::from)
                    }
                    None => self.peers.invoke(&peer, &tool, envelope, ctx).await.map_err(ApiError::from),
                }
            }
            None => self.invoke_local(&tool, &verb, inner, ctx, &tools).await,
        };
        self.record_call(plugin, &tool, &verb, ctx, result.is_ok(), started);
        result
    }

    /// Why the caller's stamped level may not make this call (channels.md R125).
    fn level_refusal(&self, tool: &str, verb: &str, args: &Value, ctx: &CallContext) -> Option<String> {
        let level = awm_trust::stamped_level(ctx.caller.trust.as_deref(), ctx.caller.lane != Lane::Edge);
        let (contract, major) = crate::isolated::HARNESS_CONTRACT;
        let harness = self.providers(contract, major).iter().any(|o| o.name == tool);
        let names_other = awm_trust::Call::names_other(args);
        awm_trust::call_refusal(level, &awm_trust::Call { tool, verb, harness, names_other })
    }

    /// Append `ev` to the usage log and publish it, as written, on `awm/usage`.
    pub fn record_usage(&self, ev: UsageEvent) {
        let written = self.usage.record(ev);
        let payload = serde_json::to_value(&written).unwrap_or(Value::Null);
        self.topics.publish(self, crate::telemetry::SERVICE, crate::telemetry::USAGE_TOPIC, &payload);
    }

    /// Publish a service's register or disconnect on `awm/services`.
    pub fn publish_service_event(&self, service: &str, plugin: &str, pid: Option<i64>, event: crate::telemetry::ServiceEvent) {
        let payload = crate::telemetry::service_payload(service, plugin, pid, event);
        self.topics.publish(self, crate::telemetry::SERVICE, crate::telemetry::SERVICES_TOPIC, &payload);
    }

    fn record_call(&self, plugin: Option<String>, tool: &str, verb: &str, ctx: &CallContext, ok: bool, started: Instant) {
        let Some(plugin) = plugin.filter(|_| verb != DESCRIBE) else { return };
        let mut ev = UsageEvent::new(plugin, ctx.surface);
        ev.tool = Some(tool.to_string());
        ev.verb = Some(verb.to_string());
        ev.name = ctx.plugin.clone();
        ev.ok = Some(ok);
        ev.ms = Some(started.elapsed().as_millis() as u64);
        self.record_usage(ev);
    }

    /// The isolated session that holds `token`, from the harness plugin, or None for a token
    /// it does not know.
    pub async fn holder_of(&self, token: &str) -> Result<Option<Holder>, ApiError> {
        if let Some(h) = self.tokens.get(token) {
            return Ok(Some(h));
        }
        let ctx = CallContext { plugin: Some(NATIVE_TOOL.into()), ..CallContext::local(self.node.clone(), Surface::Plugin) };
        let (contract, major) = crate::isolated::HARNESS_CONTRACT;
        let harness: Vec<String> = self.providers(contract, major).into_iter().map(|o| o.name).collect();
        let local: Vec<&String> = harness.iter().filter(|h| self.owner(h).is_some_and(|o| o.peer.is_none())).collect();
        if local.is_empty() {
            return Err(ApiError::new(503, format!("no {contract}@{major} plugin runs on this node")));
        }
        let mut failed = None;
        for tool in local {
            let envelope = json!({"verb": crate::isolated::PROVIDER_VERB, "args": {"token": token}});
            match self.invoke(tool, envelope, &ctx).await {
                Ok(text) => {
                    let answer: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
                    if let Some(h) = Holder::from_answer(&answer) {
                        let h = Holder { harness: harness.clone(), ..h };
                        self.tokens.put(token, h.clone());
                        return Ok(Some(h));
                    }
                }
                Err(e) => failed = Some(e),
            }
        }
        // A harness that could not answer may hold the token: that is no answer, not "unknown".
        failed.map_or(Ok(None), Err)
    }

    /// A call from an isolated session: its whitelist, this node only or a singleton's door,
    /// its own scope.
    pub async fn invoke_isolated(&self, holder: &Holder, tool: &str, envelope: &Value) -> Result<String, ApiError> {
        let refuse = |why: String| ApiError::new(403, why);
        let Some(verb) = envelope.get("verb").and_then(Value::as_str) else {
            return Err(ApiError::new(400, format!("{tool}: missing 'verb'")));
        };
        match envelope.get("peer") {
            None | Some(Value::Null) => {}
            Some(Value::String(p)) if p == "local" || *p == self.node => {}
            Some(p) => return Err(refuse(format!("an isolated session runs verbs on this node only, not on peer {p}"))),
        }
        if !holder.may_use(tool) {
            return Err(refuse(format!("tool {tool:?} is not on this isolated session's whitelist")));
        }
        let mut inner = envelope.get("args").cloned().filter(|a| !a.is_null()).unwrap_or_else(|| json!({}));
        let tools = self.tools();
        let ctx = CallContext {
            caller: holder.caller(&self.node),
            as_: None,
            surface: Surface::Mcp,
            plugin: None,
            mode: None,
            pid: None,
        };
        if verb == DESCRIBE {
            let allowed: Vec<String> = holder.tools.get(tool).into_iter().flatten().filter(|v| holder.may(tool, v)).cloned().collect();
            let wanted: Vec<String> = match inner.get("verbs").and_then(Value::as_array) {
                Some(w) => w.iter().filter_map(Value::as_str).filter(|v| allowed.iter().any(|a| a == v)).map(str::to_string).collect(),
                None => allowed,
            };
            return self.invoke_local(tool, DESCRIBE, json!({"verbs": wanted}), &ctx, &tools).await;
        }
        if !holder.may(tool, verb) {
            return Err(refuse(format!("{tool}.{verb} is not on this isolated session's whitelist")));
        }
        if !tools.contains_key(tool) {
            if let Some(route) = self.door_route(tool, None)? {
                // The host never sees this node's specs, so only the scope check of `pin` applies here;
                // the host stamps the session, its level and scope from the request.
                crate::isolated::pin(holder, &json!({}), &mut inner).map_err(refuse)?;
                let envelope = json!({"verb": verb, "args": strip_stamps(&inner)});
                let request = crate::door::request(tool.to_string(), envelope, &ctx.caller, &self.node);
                return Ok(crate::door::call(&route, &request).await?);
            }
        }
        let started = Instant::now();
        let result = async {
            let (service, found) = self.wait_for_verb(tool, verb).await?;
            crate::isolated::pin(holder, &found.spec, &mut inner).map_err(refuse)?;
            let declared = strip_stamps(&inner);
            check_arguments(tool, verb, &found.spec, &declared)?;
            let timeout = found.spec.get("timeout").and_then(Value::as_f64).unwrap_or(DEFAULT_CALL_TIMEOUT_S);
            let value =
                self.hub.call(&service, &found.function, inner, None, ctx.plugin.as_deref(), &ctx.caller, Duration::from_secs_f64(timeout)).await?;
            Ok::<_, ApiError>(render(&value))
        }
        .await;
        self.record_call(tools.get(tool).map(|e| e.plugin.clone()), tool, verb, &ctx, result.is_ok(), started);
        result
    }

    async fn invoke_local(
        &self,
        tool: &str,
        verb: &str,
        inner: Value,
        ctx: &CallContext,
        tools: &BTreeMap<String, ToolEntry>,
    ) -> Result<String, ApiError> {
        let Some(entry) = tools.get(tool) else { return Err(ApiError::not_installed(tool)) };
        if verb == DESCRIBE {
            let entry = if entry.verbs.is_empty() && tool != NATIVE_TOOL {
                // A caller asking for the schema of a starting tool waits for it like a call would.
                let _ = self.wait_for_verb(tool, "\u{0}").await;
                self.tools().get(tool).cloned().unwrap_or_else(|| entry.clone())
            } else {
                entry.clone()
            };
            let answer = if tool == NATIVE_TOOL && inner.get("verbs").is_none() {
                self.overview(ctx.surface == Surface::Mcp, &ctx.caller)
            } else {
                self.describe(&entry, inner.get("verbs"), ctx.surface == Surface::Mcp)
            };
            return Ok(render(&answer));
        }
        if tool == NATIVE_TOOL && NATIVE_VERBS.iter().any(|(v, _)| *v == verb) {
            return self.native(verb, &inner, ctx).await.map(|v| render(&v));
        }
        if self.marks.refuses(ctx, &inner) {
            tracing::info!("private gate: refused {tool}.{verb} to {}", ctx.caller.node);
            return Err(ApiError::not_installed(tool));
        }
        let (service, found) = self.wait_for_verb(tool, verb).await?;
        check_arguments(tool, verb, &found.spec, &inner)?;
        let mut inner = inner;
        crate::caller::stamp(&mut inner, &ctx.caller, ctx.pid, &self.node);
        let timeout = found.spec.get("timeout").and_then(Value::as_f64).unwrap_or(DEFAULT_CALL_TIMEOUT_S);
        let value = self
            .hub
            .call(&service, &found.function, inner, ctx.as_.as_deref(), ctx.plugin.as_deref(), &ctx.caller, Duration::from_secs_f64(timeout))
            .await?;
        Ok(render(&value))
    }
}

/// A result as `/invoke` returns it: a string verbatim, anything else as indented JSON.
pub fn render(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => serde_json::to_string_pretty(other).unwrap_or_default(),
    }
}

/// One `{verb, args, peer}` tool of the MCP list. `verbs` is None while a door's list is unread:
/// the verb is then left open, so a call still reaches the door.
fn domain_tool(name: &str, verbs: Option<Vec<VerbLine>>, runs: &str, peer: Option<Value>) -> Value {
    let mut lines: Vec<String> = verbs
        .iter()
        .flatten()
        .map(|(v, s, args)| {
            let args: Vec<String> = args.iter().map(|(n, r)| if *r { n.clone() } else { format!("{n}?") }).collect();
            format!("- {v}({}): {}", args.join(", "), one_line(s))
        })
        .collect();
    if lines.is_empty() {
        lines.push("- (its verbs are not read yet; call verb='describe' for them)".into());
    }
    let mut verb = json!({"type": "string",
                          "description": "The operation to run. verb='describe' (optionally args={verbs:[<name>, ...]}) returns parameter schemas."});
    if let Some(verbs) = &verbs {
        let mut names: Vec<&str> = verbs.iter().map(|(v, _, _)| v.as_str()).collect();
        names.push(DESCRIBE);
        verb["enum"] = json!(names);
    }
    let mut properties = json!({
        "verb": verb,
        "args": {"type": "object",
                 "description": "Arguments for the chosen verb. An unknown argument is an error that returns the verb's schema."},
    });
    if let Some(peer) = peer {
        properties["peer"] = peer;
    }
    json!({
        "name": name,
        "description": format!(
            "The '{name}' tool. Call with {{verb, args}}; verb='describe' (optionally args={{verbs:[<name>, ...]}}) returns full parameter schemas. Verbs, with arguments (? marks optional):\n{}\n{runs}",
            lines.join("\n")),
        "inputSchema": {"type": "object", "properties": properties, "required": ["verb"]},
    })
}

/// The MCP verbs of a `describe` answer: each verb its `surfaces` lets onto MCP.
fn described_verbs(answer: &Value) -> Vec<VerbLine> {
    answer
        .get("verbs")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|v| on_mcp(v))
        .filter_map(|v| {
            let name = v.get("verb").and_then(Value::as_str)?.to_string();
            Some((name, summary(v), args(v)))
        })
        .collect()
}

/// Whether a verb lists on MCP: a manifest `surfaces` list that leaves out `mcp` hides it.
fn on_mcp(spec: &Value) -> bool {
    spec.get("surfaces").and_then(Value::as_array).is_none_or(|s| s.iter().any(|v| v == "mcp"))
}

fn summary(spec: &Value) -> String {
    spec.get("description").and_then(Value::as_str).unwrap_or("").to_string()
}

/// The first sentence of the first line, cut at a word boundary past 160 characters.
fn one_line(s: &str) -> String {
    const CAP: usize = 160;
    let line = s.lines().next().unwrap_or("").trim();
    let end = line.match_indices(". ").map(|(i, _)| i).find(|&i| ends_sentence(&line[..i])).map_or(line.len(), |i| i + 1);
    let sentence = &line[..end];
    if sentence.chars().count() <= CAP {
        return sentence.to_string();
    }
    let head: String = sentence.chars().take(CAP).collect();
    let cut = head.rfind(' ').unwrap_or(head.len());
    format!("{}...", head[..cut].trim_end_matches([',', ';', ':']))
}

/// Whether a period after `before` ends a sentence: not after a one-letter word, as in "e.g.".
fn ends_sentence(before: &str) -> bool {
    before.rsplit([' ', '.', '(']).next().is_none_or(|w| w.chars().count() > 1)
}

/// Each param in declared order, with whether it is required.
fn args(spec: &Value) -> Vec<(String, bool)> {
    spec.get("params")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|p| {
            let name = p.get("name").and_then(Value::as_str)?.to_string();
            Some((name, p.get("required").and_then(Value::as_bool).unwrap_or(false)))
        })
        .collect()
}

/// A manifest's type name as a JSON Schema type.
fn json_type(name: &str) -> &'static str {
    match name {
        "integer" | "int" => "integer",
        "number" | "float" => "number",
        "boolean" | "bool" => "boolean",
        "object" | "dict" => "object",
        "array" | "list" => "array",
        _ => "string",
    }
}

/// A verb's params as JSON Schema. A param's `type` is one name or a list of names, so it may take more than one.
fn input_schema(spec: &Value) -> Value {
    let mut props = serde_json::Map::new();
    let mut req = Vec::new();
    for p in spec.get("params").and_then(Value::as_array).into_iter().flatten() {
        let Some(name) = p.get("name").and_then(Value::as_str) else { continue };
        let ty = match p.get("type") {
            Some(Value::Array(types)) => {
                let mut named: Vec<&str> = Vec::new();
                for t in types.iter().map(|t| json_type(t.as_str().unwrap_or("string"))) {
                    if !named.contains(&t) {
                        named.push(t);
                    }
                }
                if named.len() == 1 { json!(named[0]) } else { json!(named) }
            }
            other => json!(json_type(other.and_then(Value::as_str).unwrap_or("string"))),
        };
        props.insert(name.into(), json!({"type": ty, "description": p.get("description").cloned().unwrap_or(json!(""))}));
        if p.get("required").and_then(Value::as_bool).unwrap_or(false) {
            req.push(name.to_string());
        }
    }
    json!({"type": "object", "properties": props, "required": req})
}

/// The args without the `_caller_*` keys the gateway stamps, which no verb declares.
fn strip_stamps(args: &Value) -> Value {
    let mut out = args.clone();
    if let Some(m) = out.as_object_mut() {
        m.retain(|k, _| !k.starts_with("_caller"));
    }
    out
}

/// Refuse an argument the verb does not declare, with the verb's schema (as today's gateway).
fn check_arguments(tool: &str, verb: &str, spec: &Value, args: &Value) -> Result<(), ApiError> {
    let Some(params) = spec.get("params").and_then(Value::as_array) else { return Ok(()) };
    let known: Vec<&str> = params.iter().filter_map(|p| p.get("name").and_then(Value::as_str)).collect();
    // The stamp replaces or drops these, so a caller that sends one is not refused for it.
    let restamped = [crate::caller::PID_KEY, crate::caller::CLAIM_KEY, crate::caller::PLUGIN_KEY];
    let unknown: Vec<&String> = args
        .as_object()
        .into_iter()
        .flat_map(|m| m.keys())
        .filter(|k| !known.contains(&k.as_str()) && !restamped.contains(&k.as_str()))
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    Err(ApiError {
        status: 400,
        detail: json!({
            "error": format!("{tool}.{verb}: unknown argument(s): {}", unknown.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ")),
            "schema": input_schema(spec),
        }),
    })
}

const PEER_CACHE: &str = "peer-tools.json";

/// The last tables the current peers gave, from `peer-tools.json`.
fn cached_peer_tools(state: &Path, peers: &[String]) -> BTreeMap<String, Vec<RemoteTool>> {
    let mut known: BTreeMap<String, Vec<RemoteTool>> =
        std::fs::read_to_string(state.join(PEER_CACHE)).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    known.retain(|p, _| peers.contains(p));
    known
}

fn save_peer_cache(state: &Path, known: &BTreeMap<String, Vec<RemoteTool>>) {
    let Ok(text) = serde_json::to_string_pretty(known) else { return };
    let tmp = state.join(format!("{PEER_CACHE}.tmp"));
    if std::fs::write(&tmp, text).is_ok() {
        let _ = std::fs::rename(&tmp, state.join(PEER_CACHE));
    }
}

/// The peers' tables less every singleton: no edge carries a singleton (fleet.md R89).
fn edge_tools(peers: &BTreeMap<String, Vec<RemoteTool>>, edges: &EdgeList) -> BTreeMap<String, Vec<RemoteTool>> {
    peers
        .iter()
        .map(|(peer, tools)| (peer.clone(), tools.iter().filter(|t| edges.singleton(&t.name).is_none()).cloned().collect()))
        .collect()
}

fn load_plugins(
    state: &Path,
    cfg: &Config,
    peer_tools: &BTreeMap<String, Vec<RemoteTool>>,
    edges: &EdgeList,
    node: &str,
    failed_installs: &BTreeMap<String, (String, String)>,
) -> LoadResult {
    let enabled: HashMap<String, bool> = std::fs::read_to_string(state.join("enabled.json"))
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default();
    let reserved = cfg.reserved.clone();
    let wants = |p: &str| cfg.wants(p);
    let wants_service = |p: &str, s: &str| cfg.wants_service(p, s);
    let core_roots = cfg.core_root_dirs(state);
    let mut plugin_roots = cfg.plugin_roots.clone();
    plugin_roots.push(crate::install::installed_root(state));
    let peer_tools = &edge_tools(peer_tools, edges);
    let doors: Vec<String> = edges.door_singletons(node).iter().map(|s| s.tool.clone()).collect();
    let elsewhere: BTreeMap<String, String> =
        edges.singletons.iter().filter(|s| s.host != node).map(|s| (s.tool.clone(), s.host.clone())).collect();
    let mut load = loader::load(&loader::LoadInput {
        wants: &wants,
        wants_service: &wants_service,
        core_roots: &core_roots,
        plugin_roots: &plugin_roots,
        profiles: &cfg.profiles,
        reserved: &reserved,
        enabled: &enabled,
        peer_tools,
        doors: &doors,
        elsewhere: &elsewhere,
        failed_installs,
    });
    crate::lifecycle::stamp_all(&mut load, &core_roots, &cfg.workspace_dir(state).join(PLUGIN_PINS));
    load
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn domain_tool_verb_lines() {
        let long = format!("{} tail. More.", "word ".repeat(40));
        let rows: [(&str, &[(&str, bool)], &str); 5] = [
            ("Rank hits. Open one with get.", &[("query", true), ("max", false)], "- v(query, max?): Rank hits."),
            ("No arguments.", &[], "- v(): No arguments."),
            ("Find items, e.g. notes. Then more.\nSecond line.", &[], "- v(): Find items, e.g. notes."),
            ("Ends without a period", &[("a", false)], "- v(a?): Ends without a period"),
            (&long, &[], &format!("- v(): {}...", "word ".repeat(32).trim_end())),
        ];
        for (summary, args, want) in rows {
            let args = args.iter().map(|(n, r)| (n.to_string(), *r)).collect();
            let tool = domain_tool("t", Some(vec![("v".into(), summary.into(), args)]), "", None);
            let desc = tool["description"].as_str().unwrap();
            assert!(desc.lines().any(|l| l == want), "{want:?} not in {desc}");
        }
    }
}
