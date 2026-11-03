//! Gateway state and the catalog: which tools exist, how they describe themselves, and dispatch.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock, RwLock, Weak};
use std::time::{Duration, Instant};

use awm_proto::{CallContext, Lane, Surface, Tier, UsageEvent};
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
const NATIVE_VERBS: [(&str, &str); 6] = [
    ("status", "The gateway's status: node, uptime, plugins and services."),
    ("plugins", "Every plugin the loader read: loaded, refused with the reason, and skipped."),
    ("services", "Every service the hub knows, with its status."),
    ("usage", "Usage per plugin and surface over the last `days` days (default 60)."),
    ("table", "The live name table: each tool's owner plugin and node, and each contract's providers."),
    ("reload", "Read the plugin roots and the install config again. Starts added plugins, restarts changed ones and stops removed ones, with no gateway restart."),
];
const DEFAULT_CALL_TIMEOUT_S: f64 = 120.0;

pub struct Gateway {
    pub state: PathBuf,
    pub cfg: Config,
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
    me: Weak<Gateway>,
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

    pub async fn open_with(state: &Path, cfg: Config, peers: Arc<dyn PeerTransport>) -> anyhow::Result<Arc<Self>> {
        std::fs::create_dir_all(state)?;
        let state = state.canonicalize()?;
        let node = cfg.node_name();
        let peer_tools = fetch_peer_tools(&state, peers.as_ref()).await;
        let load = load_plugins(&state, &cfg, &peer_tools);
        for r in &load.refused {
            tracing::error!("plugin {} refused: {}", r.plugin, r.reason);
        }
        for w in &load.warnings {
            tracing::warn!("{w}");
        }
        Ok(Arc::new_cyclic(|me| Self {
            usage: UsageStore::new(state.join("usage")),
            state,
            cfg,
            node,
            load: RwLock::new(load),
            hub: Hub::default(),
            peers,
            url: OnceLock::new(),
            stopping: AtomicBool::new(false),
            started: Instant::now(),
            supervisor: Supervisor::default(),
            tokens: TokenCache::default(),
            peer_tools: RwLock::new(peer_tools),
            reloading: tokio::sync::Mutex::new(()),
            me: me.clone(),
        }))
    }

    pub fn stopping(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
    }

    pub fn hub_url(&self) -> String {
        self.url.get().cloned().unwrap_or_else(|| format!("http://{}", self.cfg.listen))
    }

    pub fn read_load(&self) -> std::sync::RwLockReadGuard<'_, LoadResult> {
        self.load.read().unwrap_or_else(|p| p.into_inner())
    }

    /// The plugin a service belongs to. A service the loader does not know is its own plugin.
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

    /// The tools this node's plugins serve, as `/hub/table` gives them to a peer.
    pub fn local_table(&self) -> Vec<RemoteTool> {
        self.read_load()
            .plugins
            .iter()
            .flat_map(|p| p.tools.iter())
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
        loader::table(&self.read_load().plugins, &peers)
    }

    /// Who owns a tool name right now (R117 item 3).
    pub fn owner(&self, name: &str) -> Option<Owner> {
        loader::owner(&self.name_table(), name).cloned()
    }

    /// Every provider of a contract at a major right now (R117 item 4).
    pub fn providers(&self, contract: &str, major: u32) -> Vec<Owner> {
        loader::providers(&self.name_table(), contract, major)
    }

    /// The live table as `awm table` shows it.
    pub fn table_view(&self) -> Value {
        let index = self.name_table();
        let mut contracts: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        for p in &index {
            for (c, m) in &p.implements {
                contracts.entry(format!("{c}@{m}")).or_default().push(json!(p.owner));
            }
        }
        json!({"tools": index.iter().map(|p| json!(p.owner)).collect::<Vec<_>>(), "contracts": contracts})
    }

    /// Ask one peer for its table again, after it said the table changed.
    pub async fn refresh_peer(&self, peer: &str) -> bool {
        if !self.peers.peers().iter().any(|p| p == peer) {
            return false;
        }
        match self.peers.tools(peer).await.map(serde_json::from_value::<Vec<RemoteTool>>) {
            Ok(Ok(tools)) => {
                self.peer_tools.write().unwrap_or_else(|p| p.into_inner()).insert(peer.to_string(), tools);
                let cache = self.peer_tools.read().unwrap_or_else(|p| p.into_inner()).clone();
                if let Ok(text) = serde_json::to_string_pretty(&cache) {
                    let _ = std::fs::write(self.state.join("peer-tools.json"), text);
                }
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

    /// Load the plugins again and apply the difference, with no gateway restart (R144).
    /// Only a plugin that was added, removed or changed has its services started or stopped.
    pub async fn reload(&self) -> Result<Value, ApiError> {
        let Some(gw) = self.me.upgrade() else { return Err(ApiError::new(503, "the gateway is stopping")) };
        if self.stopping() {
            return Err(ApiError::new(503, "the gateway is stopping"));
        }
        let _one_at_a_time = self.reloading.lock().await;
        let cfg = Config::load(&self.state).map_err(|e| ApiError::new(400, e.to_string()))?;
        let peers = self.peer_tools.read().unwrap_or_else(|p| p.into_inner()).clone();
        let new = load_plugins(&self.state, &cfg, &peers);
        let old: BTreeMap<String, loader::Plugin> =
            self.read_load().plugins.iter().map(|p| (p.name.clone(), p.clone())).collect();
        let fresh: BTreeMap<String, &loader::Plugin> = new.plugins.iter().map(|p| (p.name.clone(), p)).collect();

        let (mut added, mut updated, mut removed) = (Vec::new(), Vec::new(), Vec::new());
        for (name, p) in &fresh {
            match old.get(name) {
                None => added.push(name.clone()),
                Some(o) if o.stamp != p.stamp || o.services != p.services || o.dir != p.dir => updated.push(name.clone()),
                Some(_) => {}
            }
        }
        removed.extend(old.keys().filter(|n| !fresh.contains_key(*n)).cloned());

        let going: Vec<String> = removed
            .iter()
            .chain(updated.iter())
            .flat_map(|n| old[n].services.iter().map(|s| s.name.clone()))
            .collect();
        crate::supervisor::stop(&gw, &going).await;
        for s in &going {
            if self.cfg.external(s).is_none() {
                self.hub.forget(s);
            }
        }
        let starting: Vec<loader::ServiceSpec> = added
            .iter()
            .chain(updated.iter())
            .flat_map(|n| fresh[n].services.iter().cloned())
            .collect();
        for r in &new.refused {
            tracing::error!("plugin {} refused: {}", r.plugin, r.reason);
        }
        let refused: Vec<Value> = new.refused.iter().map(|r| json!({"plugin": r.plugin, "reason": r.reason})).collect();
        *self.load.write().unwrap_or_else(|p| p.into_inner()) = new;
        self.hub.changed.notify_waiters();
        for spec in starting {
            crate::supervisor::start(&gw, spec);
        }
        if !(added.is_empty() && updated.is_empty() && removed.is_empty()) {
            tracing::info!("reload: added {added:?}, updated {updated:?}, removed {removed:?}");
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
        Ok(json!({"added": added, "updated": updated, "removed": removed, "refused": refused}))
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

    fn verb_names(&self, entry: &ToolEntry) -> Vec<(String, String, Vec<String>)> {
        if entry.name == NATIVE_TOOL {
            let mut v: Vec<_> = NATIVE_VERBS.iter().map(|(n, d)| (n.to_string(), d.to_string(), vec![])).collect();
            v.extend(entry.verbs.iter().map(|(_, vb)| (vb.verb.clone(), summary(&vb.spec), required(&vb.spec))));
            return v;
        }
        entry.verbs.iter().map(|(_, vb)| (vb.verb.clone(), summary(&vb.spec), required(&vb.spec))).collect()
    }

    /// `GET /tools?view=domains`: one `{verb, args, peer}` tool per public tool on the MCP surface.
    pub fn domain_tools(&self) -> Vec<Value> {
        self.domain_tools_for(None)
    }

    /// The domain tools, cut to a holder's whitelist when one is given. A holder sees no peers.
    pub fn domain_tools_for(&self, holder: Option<&Holder>) -> Vec<Value> {
        let peers = if holder.is_some() { vec!["local".to_string()] } else { self.peer_names() };
        self.tools()
            .values()
            .filter(|e| e.tier == Tier::On && holder.is_none_or(|h| h.may_use(&e.name)))
            .map(|e| {
                let mut verbs = self.verb_names(e);
                if let Some(h) = holder {
                    verbs.retain(|(v, _, _)| h.may(&e.name, v));
                }
                let mut lines: Vec<String> = verbs
                    .iter()
                    .map(|(v, s, req)| format!("- {v}({}): {}", req.join(", "), one_line(s)))
                    .collect();
                if lines.is_empty() {
                    lines.push("- (its service is starting; call verb='describe' again shortly)".into());
                }
                let mut verb_enum: Vec<String> = verbs.into_iter().map(|(v, _, _)| v).collect();
                verb_enum.push(DESCRIBE.into());
                json!({
                    "name": e.name,
                    "description": format!(
                        "The '{}' tool. Call with {{verb, args}}; verb='describe' (optionally args={{verbs:[<name>, ...]}}) returns full parameter schemas. Verbs, with required arguments:\n{}\n{}",
                        e.name, lines.join("\n"),
                        if peers.len() > 1 { format!("Runs on this node by default; also on {} via peer=<name>.", peers[1..].join(", ")) }
                        else { "Runs on this node only.".to_string() }),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "verb": {"type": "string", "enum": verb_enum,
                                     "description": "The operation to run. verb='describe' (optionally args={verbs:[<name>, ...]}) returns parameter schemas."},
                            "args": {"type": "object",
                                     "description": "Arguments for the chosen verb. An unknown argument is an error that returns the verb's schema."},
                            "peer": {"type": "string", "enum": peers,
                                     "description": format!("Optional. Default: local (this node, {}). Valid: {}.", self.node, peers.join(", "))},
                        },
                        "required": ["verb"],
                    },
                })
            })
            .collect()
    }

    /// `GET /tools`: one tool per verb, named `<tool>_<verb>`, for the CLI. Includes tier-off tools.
    pub fn flat_tools(&self) -> Vec<Value> {
        let mut out = Vec::new();
        for e in self.tools().values() {
            for (verb, desc, _) in self.verb_names(e) {
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

    fn describe(&self, entry: &ToolEntry, filter: Option<&Value>) -> Value {
        let wanted: Option<Vec<&str>> = filter.and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).collect());
        let mut verbs = Vec::new();
        for (verb, desc, _) in self.verb_names(entry) {
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
            verbs.push(json!({"verb": verb, "description": desc, "params": params}));
        }
        json!({"tool": entry.name, "plugin": entry.plugin, "verbs": verbs})
    }

    fn overview(&self) -> Value {
        let tools: Vec<Value> = self
            .tools()
            .values()
            .map(|e| {
                json!({"tool": e.name, "plugin": e.plugin, "tier": if e.tier == Tier::On {"on"} else {"off"},
                       "verbs": self.verb_names(e).into_iter().map(|(v, s, r)| json!({"verb": v, "summary": one_line(&s), "required": r})).collect::<Vec<_>>()})
            })
            .collect();
        json!({"node": self.node, "tools": tools,
               "how": "Call <tool>(verb=\"describe\") for a tool's params; add peer=<node> to run a verb on another node."})
    }

    async fn native(&self, verb: &str, args: &Value, ctx: &CallContext) -> Result<Value, ApiError> {
        match verb {
            "table" => Ok(self.table_view()),
            "reload" => {
                if ctx.caller.lane != Lane::Local || ctx.caller.trust.as_deref().is_some_and(|t| t != crate::caller::DEFAULT_TRUST) {
                    return Err(ApiError::new(403, "reload takes a trusted caller on this node"));
                }
                self.reload().await
            }
            "status" => Ok(self.status()),
            "plugins" => Ok(self.load_snapshot()),
            "services" => Ok(json!(self.hub.services())),
            "usage" => {
                let days = args.get("days").and_then(Value::as_i64).unwrap_or(crate::usage::WINDOW_DAYS);
                Ok(json!(self.usage.tally(days)))
            }
            _ => Err(ApiError::new(404, format!("Unknown verb {verb:?} for domain {NATIVE_TOOL:?}"))),
        }
    }

    /// `<workspace>/projects`. The workspace defaults to the parent of the state dir, `<workspace>/.awm`.
    pub fn projects_dir(&self) -> PathBuf {
        let ws = self.cfg.workspace.clone().unwrap_or_else(|| self.state.parent().map(Path::to_path_buf).unwrap_or_default());
        ws.join("projects")
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
        })
    }

    /// Wait for `tool`'s `verb` to be served, up to the configured wait (R117 item 7).
    async fn wait_for_verb(&self, tool: &str, verb: &str) -> Result<(String, Verb), ApiError> {
        let deadline = Instant::now() + Duration::from_secs_f64(self.cfg.tool_wait_s);
        loop {
            let notified = self.hub.changed.notified();
            let tools = self.tools();
            let Some(entry) = tools.get(tool) else {
                return Err(ApiError::new(404, format!("Unknown tool {tool:?}")));
            };
            if let Some(found) = entry.verbs.iter().find(|(_, v)| v.verb == verb) {
                return Ok(found.clone());
            }
            if self.tool_settled(tool) {
                return Err(ApiError::new(404, format!("Unknown verb {verb:?} for domain {tool:?}")));
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err(ApiError::new(503, format!("tool {tool:?} is not up after {:.0}s", self.cfg.tool_wait_s)));
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
        if let Some(peer) = peer.filter(|p| p != "local" && *p != self.node) {
            let envelope = json!({"verb": verb, "args": inner});
            return Ok(self.peers.invoke(&peer, &tool, envelope, ctx).await?);
        }

        let started = Instant::now();
        let result = self.invoke_local(&tool, &verb, inner, ctx, &tools).await;
        self.record_call(&tools, &tool, &verb, ctx, result.is_ok(), started);
        result
    }

    fn record_call(&self, tools: &BTreeMap<String, ToolEntry>, tool: &str, verb: &str, ctx: &CallContext, ok: bool, started: Instant) {
        let Some(plugin) = tools.get(tool).map(|e| e.plugin.clone()).filter(|_| verb != DESCRIBE) else { return };
        let mut ev = UsageEvent::new(plugin, ctx.surface);
        ev.tool = Some(tool.to_string());
        ev.verb = Some(verb.to_string());
        ev.name = ctx.plugin.clone();
        ev.ok = Some(ok);
        ev.ms = Some(started.elapsed().as_millis() as u64);
        self.usage.record(ev);
    }

    /// The isolated session that holds `token`, from the harness plugin, or None for a token
    /// it does not know.
    pub async fn holder_of(&self, token: &str) -> Result<Option<Holder>, ApiError> {
        if let Some(h) = self.tokens.get(token) {
            return Ok(Some(h));
        }
        let ctx = CallContext { plugin: Some(NATIVE_TOOL.into()), ..CallContext::local(self.node.clone(), Surface::Plugin) };
        let envelope = json!({"verb": crate::isolated::PROVIDER_VERB, "args": {"token": token}});
        let text = self.invoke(crate::isolated::PROVIDER_TOOL, envelope, &ctx).await?;
        let answer: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
        let holder = Holder::from_answer(&answer);
        if let Some(h) = &holder {
            self.tokens.put(token, h.clone());
        }
        Ok(holder)
    }

    /// A call from an isolated session: its whitelist, this node only, its own scope.
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
        let started = Instant::now();
        let result = async {
            let (service, found) = self.wait_for_verb(tool, verb).await?;
            crate::isolated::pin(holder, &found.spec, &mut inner).map_err(refuse)?;
            let declared = strip_stamps(&inner);
            check_arguments(tool, verb, &found.spec, &declared)?;
            let timeout = found.spec.get("timeout").and_then(Value::as_f64).unwrap_or(DEFAULT_CALL_TIMEOUT_S);
            let value =
                self.hub.call(&service, &found.function, inner, None, &ctx.caller, Duration::from_secs_f64(timeout)).await?;
            Ok::<_, ApiError>(render(&value))
        }
        .await;
        self.record_call(&tools, tool, verb, &ctx, result.is_ok(), started);
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
        let Some(entry) = tools.get(tool) else { return Err(ApiError::new(404, format!("Unknown tool {tool:?}"))) };
        if verb == DESCRIBE {
            let entry = if entry.verbs.is_empty() && tool != NATIVE_TOOL {
                // A caller asking for the schema of a starting tool waits for it like a call would.
                let _ = self.wait_for_verb(tool, "\u{0}").await;
                self.tools().get(tool).cloned().unwrap_or_else(|| entry.clone())
            } else {
                entry.clone()
            };
            let answer = if tool == NATIVE_TOOL && inner.get("verbs").is_none() {
                self.overview()
            } else {
                self.describe(&entry, inner.get("verbs"))
            };
            return Ok(render(&answer));
        }
        if tool == NATIVE_TOOL && NATIVE_VERBS.iter().any(|(v, _)| *v == verb) {
            return self.native(verb, &inner, ctx).await.map(|v| render(&v));
        }
        let (service, found) = self.wait_for_verb(tool, verb).await?;
        check_arguments(tool, verb, &found.spec, &inner)?;
        let mut inner = inner;
        crate::caller::stamp(&mut inner, &ctx.caller);
        let timeout = found.spec.get("timeout").and_then(Value::as_f64).unwrap_or(DEFAULT_CALL_TIMEOUT_S);
        let value = self
            .hub
            .call(&service, &found.function, inner, ctx.as_.as_deref(), &ctx.caller, Duration::from_secs_f64(timeout))
            .await?;
        Ok(render(&value))
    }

    /// `POST /svc/<service>/fn/<fn>`, the door today's Python services call each other through.
    pub async fn call_function(&self, service: &str, function: &str, args: Value, ctx: &CallContext) -> Result<Value, ApiError> {
        let started = Instant::now();
        let mut args = args;
        crate::caller::stamp(&mut args, &ctx.caller);
        let result = self
            .hub
            .call(service, function, args, ctx.as_.as_deref(), &ctx.caller, Duration::from_secs_f64(DEFAULT_CALL_TIMEOUT_S))
            .await
            .map_err(ApiError::from);
        let mut ev = UsageEvent::new(self.plugin_of_service(service), Surface::Plugin);
        ev.tool = Some(service.to_string());
        ev.verb = Some(function.to_string());
        ev.ok = Some(result.is_ok());
        ev.ms = Some(started.elapsed().as_millis() as u64);
        self.usage.record(ev);
        result
    }
}

/// A result as `/invoke` returns it: a string verbatim, anything else as indented JSON.
pub fn render(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => serde_json::to_string_pretty(other).unwrap_or_default(),
    }
}

fn summary(spec: &Value) -> String {
    spec.get("description").and_then(Value::as_str).unwrap_or("").to_string()
}

fn one_line(s: &str) -> String {
    let first = s.lines().next().unwrap_or("").trim();
    if first.chars().count() > 100 {
        format!("{}...", first.chars().take(97).collect::<String>())
    } else {
        first.to_string()
    }
}

fn required(spec: &Value) -> Vec<String> {
    spec.get("params")
        .and_then(Value::as_array)
        .map(|ps| {
            ps.iter()
                .filter(|p| p.get("required").and_then(Value::as_bool).unwrap_or(false))
                .filter_map(|p| p.get("name").and_then(Value::as_str).map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

fn input_schema(spec: &Value) -> Value {
    let mut props = serde_json::Map::new();
    let mut req = Vec::new();
    for p in spec.get("params").and_then(Value::as_array).into_iter().flatten() {
        let Some(name) = p.get("name").and_then(Value::as_str) else { continue };
        let ty = match p.get("type").and_then(Value::as_str).unwrap_or("string") {
            t @ ("string" | "integer" | "number" | "boolean" | "object" | "array") => t,
            "int" => "integer",
            "bool" => "boolean",
            "float" => "number",
            "dict" => "object",
            "list" => "array",
            _ => "string",
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
    let unknown: Vec<&String> = args.as_object().into_iter().flat_map(|m| m.keys()).filter(|k| !known.contains(&k.as_str())).collect();
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

async fn fetch_peer_tools(state: &Path, peers: &dyn PeerTransport) -> BTreeMap<String, Vec<RemoteTool>> {
    let cache = state.join("peer-tools.json");
    let mut known: BTreeMap<String, Vec<RemoteTool>> =
        std::fs::read_to_string(&cache).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    for peer in peers.peers() {
        match peers.tools(&peer).await {
            Ok(v) => match serde_json::from_value::<Vec<RemoteTool>>(v) {
                Ok(tools) => {
                    known.insert(peer, tools);
                }
                Err(e) => tracing::warn!("peer {peer}: bad tool list: {e}"),
            },
            Err(e) => tracing::warn!("peer {peer}: tool list from cache: {e}"),
        }
    }
    known.retain(|p, _| peers.peers().contains(p));
    if let Ok(text) = serde_json::to_string_pretty(&known) {
        let _ = std::fs::write(&cache, text);
    }
    known
}

fn load_plugins(state: &Path, cfg: &Config, peer_tools: &BTreeMap<String, Vec<RemoteTool>>) -> LoadResult {
    let enabled: HashMap<String, bool> = std::fs::read_to_string(state.join("enabled.json"))
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default();
    let reserved = cfg.reserved_names(state);
    let wants = |p: &str| cfg.wants(p);
    loader::load(&loader::LoadInput {
        wants: &wants,
        core_roots: &cfg.core_roots,
        plugin_roots: &cfg.plugin_roots,
        page_roots: &cfg.page_roots,
        profiles: &cfg.profiles,
        reserved: &reserved,
        enabled: &enabled,
        peer_tools,
    })
}
