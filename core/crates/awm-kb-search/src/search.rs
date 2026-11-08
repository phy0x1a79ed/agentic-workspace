//! The federation: which sources a query reaches, asking each apart, and the views of them.

use crate::federation::{self, Peers};
use crate::hits::{self, py_str};
use crate::live::{self, truthy};
use crate::registry::{self, Declared};
use crate::request::{self, label, Kinds, Request, SourceReq};
use crate::store::{self, Use, Verdict};
use crate::{BoxFut, CallError, Fail};
use awm_embed::Embedder;
use awm_index::fields::Class;
use awm_index::{Mark, Veil};
use awm_kb_source::{Ask, Source};
use awm_plugin_sdk::privacy;
use awm_store::Store;
use indexmap::IndexMap;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// How long a source has to answer one query.
pub const SOURCE_TIMEOUT: Duration = Duration::from_secs(15);
/// How long the registry read stands before a query reads it again.
pub const REGISTRY_TTL: Duration = Duration::from_secs(10);
/// How long the journal's open tag clashes are trusted before they are read again.
pub const CLASHES_TTL: Duration = Duration::from_secs(60);
/// Sources retired from the archive, never listed.
pub const RETIRED: [&str; 2] = ["precedence", "notes"];
/// The usage log's name for this node.
pub const THIS_NODE: &str = "local";

/// The calls search makes to its own gateway. Tests stand in for them.
pub trait Gateway: Send + Sync {
    /// The rows of `awm services`.
    fn services(&self) -> BoxFut<'_, Result<Vec<Value>, CallError>>;
    /// `awm plugins`: the loader's snapshot, whose `archives` declare every kind a plugin root holds.
    fn plugins(&self) -> BoxFut<'_, Result<Value, CallError>>;
    fn call<'a>(&'a self, tool: &'a str, verb: &'a str, args: Value, timeout: Duration) -> BoxFut<'a, Result<Value, CallError>>;
}

/// The gateway through the SDK's hub client.
pub struct HubGateway(pub awm_plugin_sdk::Hub);

impl Gateway for HubGateway {
    fn services(&self) -> BoxFut<'_, Result<Vec<Value>, CallError>> {
        Box::pin(async move {
            let rows = self.call("awm", "services", json!({}), Duration::from_secs(10)).await?;
            Ok(rows.as_array().cloned().unwrap_or_default())
        })
    }

    fn plugins(&self) -> BoxFut<'_, Result<Value, CallError>> {
        self.call("awm", "plugins", json!({}), Duration::from_secs(10))
    }

    fn call<'a>(&'a self, tool: &'a str, verb: &'a str, args: Value, timeout: Duration) -> BoxFut<'a, Result<Value, CallError>> {
        Box::pin(async move {
            let opts = awm_plugin_sdk::CallOpts { timeout, ..Default::default() };
            self.0.call_with(tool, verb, args, &opts).await.map_err(CallError::from)
        })
    }
}

/// What the registry read last found.
#[derive(Default)]
struct Registry {
    live: IndexMap<String, Declared>,
    declared: BTreeMap<String, (awm_index::Fields, Vec<String>)>,
    at: Option<Instant>,
    refreshed: Option<f64>,
    error: Option<String>,
}

/// One source's last answer, for `status`.
#[derive(Debug, Clone, Default)]
struct Answered {
    at: f64,
    error: Option<String>,
}

pub struct Search {
    store: Store,
    sources: IndexMap<String, Arc<dyn Source>>,
    embedder: Option<Arc<dyn Embedder>>,
    gateway: Arc<dyn Gateway>,
    peers: Arc<dyn Peers>,
    node: String,
    /// How long one source has to answer; [`SOURCE_TIMEOUT`] unless a test shortens it.
    pub timeout: Duration,
    registry: tokio::sync::Mutex<Registry>,
    clashes: Mutex<Option<(Instant, HashSet<String>)>>,
    answers: Mutex<BTreeMap<String, Answered>>,
}

fn timeout_text(d: Duration) -> String {
    let s = d.as_secs_f64();
    if s.fract() == 0.0 {
        format!("timeout after {}s", s as u64)
    } else {
        format!("timeout after {s}s")
    }
}

fn failed(error: impl Into<String>) -> Value {
    json!({"exact": [], "error": error.into()})
}

/// The private marks a caller may not see past: none for a caller on this node.
pub fn veil_for(as_: Option<&str>) -> Veil {
    if !privacy::from_peer(as_) {
        return Veil::None;
    }
    match privacy::marks() {
        Ok(marks) => Veil::Marks { marks: marks.into_iter().map(|m| Mark { project: m.project, scope: m.scope }).collect(), tags: None },
        Err(_) => Veil::Unreadable,
    }
}

/// The node that relayed a call, or `None` for a call made on this node.
pub fn relayer(as_: Option<&str>) -> Option<String> {
    privacy::from_peer(as_).then(|| awm_plugin_sdk::config::caller_peer(as_).unwrap_or("peer").to_string())
}

fn declaration(fields: &awm_index::Fields, hit: &[String]) -> (Value, Value) {
    let mut out = Map::new();
    for f in fields.iter() {
        let class = match f.class {
            Class::Content => "content",
            Class::Categorical => "categorical",
            Class::Numeric => "numeric",
            Class::Datetime => "datetime",
        };
        out.insert(f.name.clone(), if f.nested.is_empty() { json!(class) } else { json!({"class": class, "nested": f.nested}) });
    }
    (Value::Object(out), json!(hit))
}

impl Search {
    /// search over `sources`, with its database in `state_dir` (`<AWM_DIR>/services/search`).
    pub fn new(
        state_dir: &Path,
        sources: Vec<Arc<dyn Source>>,
        embedder: Option<Arc<dyn Embedder>>,
        gateway: Arc<dyn Gateway>,
        peers: Arc<dyn Peers>,
    ) -> anyhow::Result<Search> {
        Ok(Search {
            store: store::open(state_dir)?,
            sources: sources.into_iter().map(|s| (s.name().to_string(), s)).collect(),
            embedder,
            gateway,
            peers,
            node: awm_plugin_sdk::config::node_name(),
            timeout: SOURCE_TIMEOUT,
            registry: tokio::sync::Mutex::new(Registry::default()),
            clashes: Mutex::new(None),
            answers: Mutex::new(BTreeMap::new()),
        })
    }

    /// [`Search::new`] over the gateway at `hub`.
    pub fn with_hub(state_dir: &Path, sources: Vec<Arc<dyn Source>>, embedder: Option<Arc<dyn Embedder>>, hub: awm_plugin_sdk::Hub) -> anyhow::Result<Search> {
        Self::new(state_dir, sources, embedder, Arc::new(HubGateway(hub.clone())), Arc::new(federation::HubPeers(hub)))
    }

    /// The same search under another node name, for tests.
    pub fn named(mut self, node: &str) -> Self {
        self.node = node.into();
        self
    }

    pub fn store(&self) -> &Store {
        &self.store
    }

    pub fn node(&self) -> &str {
        &self.node
    }

    pub fn is_here(&self, node: &str) -> bool {
        node.is_empty() || label(node) == label(&self.node)
    }

    // -- registry ---------------------------------------------------------

    /// Read the registry and the declared kinds. A failed read keeps what was read before.
    pub async fn refresh(&self) -> bool {
        let mut reg = self.registry.lock().await;
        self.refresh_locked(&mut reg).await
    }

    async fn refresh_locked(&self, reg: &mut Registry) -> bool {
        reg.at = Some(Instant::now());
        match self.gateway.plugins().await {
            Ok(snapshot) => reg.declared = registry::declared(&snapshot),
            Err(e) => tracing::warn!("search: plugin snapshot unread: {}", e.describe()),
        }
        match self.gateway.services().await {
            Ok(rows) => {
                reg.live = registry::parse(&rows).0;
                reg.refreshed = Some(store::now());
                reg.error = None;
                true
            }
            Err(e) => {
                reg.error = Some(e.describe());
                false
            }
        }
    }

    async fn registry(&self) -> tokio::sync::MutexGuard<'_, Registry> {
        let mut reg = self.registry.lock().await;
        if reg.at.is_none_or(|t| t.elapsed() > REGISTRY_TTL) {
            self.refresh_locked(&mut reg).await;
        }
        reg
    }

    /// Every kind a query may name: the declared ones, the running live ones, and kb's own.
    async fn kinds(&self) -> (Kinds, IndexMap<String, Declared>) {
        let reg = self.registry().await;
        let mut kinds = reg.declared.clone();
        for (name, d) in &reg.live {
            if let Ok((fields, hit)) = awm_index::fields::parse_fields(&d.table) {
                if !fields.0.is_empty() {
                    kinds.entry(name.clone()).or_insert((fields, hit));
                }
            }
        }
        for (name, s) in &self.sources {
            let (fields, hit) = s.fields();
            kinds.insert(name.clone(), (fields.clone(), hit.to_vec()));
        }
        (Kinds(kinds), reg.live.clone())
    }

    fn defaults(&self) -> Vec<String> {
        let mut out: Vec<String> = self.sources.values().filter(|s| s.default()).map(|s| s.name().to_string()).collect();
        out.sort();
        out
    }

    fn answered(&self, name: &str, error: Option<String>) {
        self.answers.lock().unwrap().insert(name.into(), Answered { at: store::now(), error });
    }

    // -- tag clashes ------------------------------------------------------

    /// The nodes an open tag clash stands between this node and, as the journal records them. A
    /// journal that cannot answer leaves the last set in force, and none before the first read.
    pub async fn clashed(&self) -> HashSet<String> {
        if let Some((at, nodes)) = self.clashes.lock().unwrap().as_ref() {
            if at.elapsed() < CLASHES_TTL {
                return nodes.clone();
            }
        }
        let call = self.gateway.call("journal", "tag_list", json!({}), self.timeout);
        let raw = match tokio::time::timeout(self.timeout + Duration::from_secs(1), call).await {
            Ok(Ok(raw)) => raw,
            Ok(Err(e)) => {
                tracing::warn!("search: tag clashes unread: {}", e.describe());
                return self.clashes.lock().unwrap().as_ref().map(|(_, n)| n.clone()).unwrap_or_default();
            }
            Err(_) => {
                tracing::warn!("search: tag clashes unread: {}", timeout_text(self.timeout));
                return self.clashes.lock().unwrap().as_ref().map(|(_, n)| n.clone()).unwrap_or_default();
            }
        };
        let here = label(&self.node);
        let mut nodes = HashSet::new();
        for c in raw.get("clashes").and_then(Value::as_array).into_iter().flatten() {
            let side = |k: &str| label(&c.get(k).filter(|v| truthy(v)).map(py_str).unwrap_or_default());
            let pair: HashSet<String> = [side("older_node"), side("newer_node")].into_iter().collect();
            if pair.contains(&here) && pair.len() == 2 {
                nodes.extend(pair.into_iter().filter(|n| *n != here));
            }
        }
        *self.clashes.lock().unwrap() = Some((Instant::now(), nodes.clone()));
        nodes
    }

    /// Refuse a call to or from `node` while an open tag clash stands between it and this node.
    pub async fn refuse_clashed(&self, node: Option<&str>) -> Result<(), Fail> {
        match node {
            Some(n) if !n.is_empty() && self.clashed().await.contains(&label(n)) => Err(Fail::new(
                "PermissionError",
                format!("an open tag clash stands between {n} and this node; searches between them wait until an agent renames the tag"),
            )),
            _ => Ok(()),
        }
    }

    // -- query ------------------------------------------------------------

    /// The validated request for `args`, as the caller `as_` may see it.
    pub async fn parse(&self, args: &Value, as_: Option<&str>) -> Result<Request, Fail> {
        let (kinds, _) = self.kinds().await;
        let defaults = self.defaults();
        let ctx = request::Context { kinds: &kinds, here: &self.node, defaults: &defaults, relayed: privacy::from_peer(as_), veil: veil_for(as_) };
        Ok(request::parse(args, ctx)?)
    }

    /// `{source: {exact, related?, error?}}` for each source `args` names, each answered apart.
    pub async fn query(&self, args: &Value, as_: Option<&str>, session: Option<String>) -> Result<Value, Fail> {
        let req = self.parse(args, as_).await?;
        self.answer(&req, relayer(as_).as_deref(), session).await
    }

    /// The answer to a parsed request. `caller` names the node that relayed the call.
    pub async fn answer(&self, req: &Request, caller: Option<&str>, session: Option<String>) -> Result<Value, Fail> {
        self.refuse_clashed(caller).await?;
        let (kinds, live) = self.kinds().await;
        let clashed = if req.remote.is_empty() { HashSet::new() } else { self.clashed().await };
        let vector = self.vector(req).await;
        let here = req.sources.iter().map(|src| self.here(src, req, &kinds, &live, vector.clone(), session.clone()));
        let there = req.remote.iter().map(|(node, srcs)| self.there(node, srcs, req, &kinds, &clashed));
        let (here, there) = futures_util::future::join(futures_util::future::join_all(here), futures_util::future::join_all(there)).await;
        let mut out = Map::new();
        for name in &req.elsewhere {
            out.insert(name.clone(), failed("a relayed call answers for this node alone"));
        }
        for (src, got) in req.sources.iter().zip(here) {
            out.insert(src.name.clone(), got);
        }
        for got in there {
            out.extend(got);
        }
        Ok(Value::Object(out))
    }

    /// The query text's vector, once, when an asked source of kb's own ranks by vectors.
    async fn vector(&self, req: &Request) -> Option<Vec<f32>> {
        let about = req.text.as_ref()?.about.clone()?;
        let embedder = self.embedder.clone()?;
        if !req.sources.iter().any(|s| self.sources.get(&s.kind).is_some_and(|src| src.takes_vectors())) {
            return None;
        }
        match tokio::task::spawn_blocking(move || embedder.encode(&[about.as_str()])).await {
            Ok(Ok(mut v)) if !v.is_empty() => Some(v.swap_remove(0)),
            Ok(Err(e)) => {
                tracing::warn!("search: query not embedded, ranking by keywords: {e}");
                None
            }
            _ => None,
        }
    }

    async fn here(&self, src: &SourceReq, req: &Request, kinds: &Kinds, live: &IndexMap<String, Declared>, vector: Option<Vec<f32>>, session: Option<String>) -> Value {
        let out = if let Some(source) = self.sources.get(&src.kind) {
            self.local(source.clone(), src, req, kinds, vector, session).await
        } else {
            match live.get(&src.kind) {
                None => failed(format!("this node has no {} source", src.kind)),
                Some(d) if !d.error.is_empty() => failed(d.error.clone()),
                Some(d) if d.mode != "live" => failed("an index source is no longer read: its service must answer live"),
                Some(d) => self.live(d, src, req, kinds).await,
            }
        };
        self.answered(&src.kind, out.get("error").map(py_str));
        out
    }

    /// One of kb's own sources, in its own task, within the timeout. For a peer caller, an
    /// answer that does not say it applied the veil is refused.
    async fn local(&self, source: Arc<dyn Source>, src: &SourceReq, req: &Request, kinds: &Kinds, vector: Option<Vec<f32>>, session: Option<String>) -> Value {
        let ask = Ask {
            text: req.text.clone(),
            source: src.filter.clone(),
            max: req.max,
            vector: vector.filter(|_| source.takes_vectors()),
            veil: req.veil.clone(),
            session,
        };
        let task = tokio::spawn(async move { source.query(ask).await });
        let abort = task.abort_handle();
        let raw = match tokio::time::timeout(self.timeout, task).await {
            Err(_) => {
                abort.abort();
                return failed(timeout_text(self.timeout));
            }
            Ok(Err(e)) => return failed(format!("the source failed: {e}")),
            Ok(Ok(Err(e))) => {
                let text = e.0;
                return failed(if text.starts_with("query.") { text } else { format!("query.{}: {text}", src.name) });
            }
            Ok(Ok(Ok(raw))) => raw,
        };
        if req.veil != Veil::None && raw.get("veiled") != Some(&Value::Bool(true)) {
            tracing::error!("search: source {} answered a peer without applying the private marks; its section is refused", src.kind);
            return failed("refused: the source did not apply the private marks for a caller on another node");
        }
        let fields = kinds.hit_fields(&src.kind);
        let mut out = Map::new();
        out.insert("exact".into(), Value::Array(hits::section(raw.get("exact"), req.max, &fields)));
        if raw.get("related").is_some_and(|r| !r.is_null()) {
            out.insert("related".into(), Value::Array(hits::section(raw.get("related"), req.max, &fields)));
        }
        if let Some(e) = raw.get("error").filter(|e| truthy(e)) {
            out.insert("error".into(), json!(py_str(e)));
        }
        Value::Object(out)
    }

    /// A live source another plugin serves, through the translation its verb takes today.
    async fn live(&self, d: &Declared, src: &SourceReq, req: &Request, kinds: &Kinds) -> Value {
        let args = match live::live_args(src, req) {
            Ok(a) => a,
            Err(e) => return failed(e),
        };
        let call = self.gateway.call(&d.tool, &d.query, args, self.timeout);
        let raw = match tokio::time::timeout(self.timeout + Duration::from_secs(1), call).await {
            Err(_) => return failed(timeout_text(self.timeout)),
            Ok(Err(e)) => return failed(e.describe()),
            Ok(Ok(raw)) => raw,
        };
        live::live_answer(&raw, req, &kinds.hit_fields(&src.kind)).unwrap_or_else(failed)
    }

    async fn there(&self, node: &str, srcs: &[SourceReq], req: &Request, kinds: &Kinds, clashed: &HashSet<String>) -> Map<String, Value> {
        let asked = federation::ask_home(self.peers.as_ref(), &self.node, node, req.forwarded(node), req.max, kinds, clashed.contains(&label(node))).await;
        srcs.iter()
            .map(|s| {
                let got = if asked.status != "ok" {
                    failed(asked.error.clone().unwrap_or_else(|| asked.status.clone()))
                } else {
                    asked.found.get(&s.kind).cloned().unwrap_or_else(|| failed("the node did not answer for it"))
                };
                (s.name.clone(), got)
            })
            .collect()
    }

    // -- get --------------------------------------------------------------

    /// `search get`: one document whole, from this node or the node the hit names. A get on
    /// this node is logged as feedback, and a relayed one is logged by the node that asked.
    pub async fn get(&self, args: &Value, as_: Option<&str>, session: Option<String>) -> Result<Value, Fail> {
        let text = |k: &str| args.get(k).filter(|v| truthy(v)).map(py_str).map(|s| awm_pycompat::pystr::strip(&s).to_string()).unwrap_or_default();
        let source = args.get("source").map(py_str).ok_or_else(|| Fail::new("KeyError", "'source'"))?;
        let id = args.get("id").map(py_str).ok_or_else(|| Fail::new("KeyError", "'id'"))?;
        let node = text("node");
        let peer = privacy::from_peer(as_);
        if !peer {
            let (project, scope) = match text("_caller_scope").split_once('/') {
                Some((p, s)) => (p.to_string(), s.to_string()),
                None => (text("_caller_scope"), String::new()),
            };
            let row = Use {
                project,
                scope,
                query: text("query"),
                source: source.clone(),
                doc_id: id.clone(),
                node: if node.is_empty() { THIS_NODE.into() } else { node.clone() },
                ts: None,
            };
            if let Err(e) = self.store.write(move |tx| store::log_use(tx, &row)).await {
                tracing::warn!("search: could not log a get: {e}");
            }
        }
        let caller = relayer(as_);
        self.refuse_clashed(caller.as_deref()).await?;
        if !self.is_here(&node) && !peer {
            self.refuse_clashed(Some(&node)).await?;
            return Ok(federation::fetch(self.peers.as_ref(), &self.node, &node, &source, &id).await?);
        }
        self.local_get(&source, &id, &veil_for(as_), if peer { None } else { session }).await
    }

    fn origin(&self) -> Value {
        json!({"node": self.node})
    }

    async fn local_get(&self, name: &str, id: &str, veil: &Veil, session: Option<String>) -> Result<Value, Fail> {
        if let Some(source) = self.sources.get(name).cloned() {
            let (id_, veil_, name_) = (id.to_string(), veil.clone(), name.to_string());
            let task = tokio::spawn(async move { source.get(&id_, &veil_, session.as_deref()).await });
            let doc = match tokio::time::timeout(self.timeout, task).await {
                Err(_) => return Err(Fail::new("TimeoutError", timeout_text(self.timeout))),
                Ok(Err(e)) => return Err(Fail::new("RuntimeError", e.to_string())),
                Ok(Ok(r)) => r.map_err(|e| Fail::new("RuntimeError", e.0))?,
            };
            let Some(doc) = doc else {
                return Err(Fail::value(format!("no document {name_}:{id}")));
            };
            let mut out: Map<String, Value> = doc
                .into_iter()
                .filter(|(k, v)| k != "id" && !matches!(v, Value::Null) && v != &json!("") && v != &json!([]))
                .collect();
            out.insert("source".into(), json!(name));
            out.insert("id".into(), json!(id));
            out.insert("frozen".into(), json!(false));
            out.insert("origin".into(), self.origin());
            return Ok(json!({"document": out}));
        }
        let (_, live) = self.kinds().await;
        let Some(d) = live.get(name) else {
            return Err(Fail::value(format!("no source {}", awm_pycompat::repr::str_repr(name))));
        };
        if d.mode != "live" || !d.error.is_empty() {
            return Err(Fail::value(format!("no document {name}:{id}")));
        }
        if d.get.is_empty() {
            return Err(Fail::value(format!("{} is a live source that reads no document whole", awm_pycompat::repr::str_repr(name))));
        }
        let call = self.gateway.call(&d.tool, &d.get, json!({"id": id}), self.timeout);
        let raw = match tokio::time::timeout(self.timeout + Duration::from_secs(1), call).await {
            Err(_) => return Err(Fail::new("TimeoutError", String::new())),
            Ok(r) => r?,
        };
        let Some(Value::Object(doc)) = raw.get("document") else {
            return Err(Fail::value(format!("no document {name}:{id}")));
        };
        let mut out = Map::new();
        for k in ["title", "text", "kind", "ts", "url", "journal"] {
            if let Some(Value::String(s)) = doc.get(k) {
                if !s.is_empty() {
                    out.insert(k.into(), json!(s));
                }
            }
        }
        if let Some(Value::Array(authors)) = doc.get("authors") {
            out.insert("authors".into(), authors.iter().take(200).map(|a| json!(py_str(a).chars().take(200).collect::<String>())).collect());
        }
        out.insert("source".into(), json!(name));
        out.insert("id".into(), json!(id));
        out.insert("frozen".into(), json!(false));
        out.insert("origin".into(), self.origin());
        Ok(json!({"document": out}))
    }

    // -- views ------------------------------------------------------------

    async fn counts(&self, peer: bool) -> (Map<String, Value>, BTreeMap<String, Value>) {
        let mut counts = Map::new();
        let mut states = BTreeMap::new();
        for (name, s) in &self.sources {
            let state = match tokio::time::timeout(self.timeout, s.status()).await {
                Ok(v) => v,
                Err(_) => json!({"error": timeout_text(self.timeout)}),
            };
            // A source's own count is unveiled, so a peer is told none.
            let count = if peer { Value::Null } else { state.get("count").filter(|c| c.is_number()).cloned().unwrap_or(Value::Null) };
            counts.insert(name.clone(), count);
            states.insert(name.clone(), state);
        }
        (counts, states)
    }

    fn last(&self, name: &str) -> (Value, Value) {
        match self.answers.lock().unwrap().get(name) {
            None => (Value::Null, Value::Null),
            Some(a) => (store::iso(Some(a.at)), a.error.clone().map_or(Value::Null, Value::String)),
        }
    }

    /// `search status`: each source's count and last answer, and the registry's health.
    pub async fn status(&self, as_: Option<&str>) -> Value {
        let peer = privacy::from_peer(as_);
        let (counts, states) = self.counts(peer).await;
        let reg = self.registry().await;
        let mut answers = Map::new();
        for name in self.sources.keys().chain(reg.live.keys()) {
            let (at, error) = self.last(name);
            answers.insert(name.clone(), json!({"at": at, "error": error}));
        }
        let documents = if peer { Value::Null } else { json!(counts.values().filter_map(Value::as_u64).sum::<u64>()) };
        let mut out = json!({
            "documents": documents,
            "sources": counts,
            "semantic": self.embedder.is_some(),
            "registry": {"refreshed": store::iso(reg.refreshed), "error": reg.error},
            "answers": answers,
        });
        if !peer {
            out["states"] = json!(states);
        }
        out
    }

    /// `search sources`: every source a query can reach on this node, with its fields, and the
    /// health `status` gives.
    pub async fn sources(&self, as_: Option<&str>) -> Value {
        let peer = privacy::from_peer(as_);
        let (counts, _) = self.counts(peer).await;
        let live = self.registry().await.live.clone();
        let mut rows = Vec::new();
        for (name, s) in &self.sources {
            let (at, error) = self.last(name);
            let (fields, hit) = s.fields();
            let (fields, hit) = declaration(fields, hit);
            rows.push(json!({
                "name": name, "service": "kb", "mode": "local", "default": s.default(), "count": counts[name.as_str()],
                "last_answer": at, "last_error": error, "fields": fields, "hit": hit, "kind": name, "node": self.node,
            }));
        }
        for (name, d) in &live {
            if self.sources.contains_key(name) || RETIRED.contains(&name.as_str()) {
                continue;
            }
            let (at, error) = self.last(name);
            let mut row = json!({
                "name": name, "service": d.service, "mode": d.mode, "default": false, "count": Value::Null,
                "last_answer": at, "last_error": if d.error.is_empty() { error } else { json!(d.error) },
            });
            if !d.topic.is_empty() {
                row["topic"] = json!(d.topic);
            }
            if d.optin {
                row["optin"] = json!(true);
            }
            if let Ok((fields, hit)) = awm_index::fields::parse_fields(&d.table) {
                let (fields, hit) = declaration(&fields, &hit);
                row["fields"] = fields;
                row["hit"] = hit;
            }
            row["kind"] = json!(name);
            row["node"] = json!(self.node);
            rows.push(row);
        }
        let mut out = self.status(as_).await;
        if let Value::Object(m) = &mut out {
            m.shift_remove("sources");
            m.shift_remove("answers");
            m.shift_remove("states");
            m.insert("sources".into(), Value::Array(rows));
        }
        out
    }

    // -- judgements -------------------------------------------------------

    /// `search judge`: record, replace or clear one verdict.
    pub async fn judge(&self, args: &Value, as_: Option<&str>) -> Result<Value, Fail> {
        let text = |k: &str| awm_pycompat::pystr::strip(&args.get(k).filter(|v| truthy(v)).map(py_str).unwrap_or_default()).to_string();
        let (query, source, doc_id) = (text("query"), text("source"), text("id"));
        let (verdict, section) = (awm_pycompat::pystr::lower(&text("verdict")), awm_pycompat::pystr::lower(&text("section")));
        if query.is_empty() || source.is_empty() || doc_id.is_empty() {
            return Err(Fail::value("search judge needs a query, a source and an id"));
        }
        if !store::VERDICTS.contains(&verdict.as_str()) && verdict != "clear" {
            return Err(Fail::value(format!("verdict: {} is not relevant, irrelevant or clear", awm_pycompat::repr::str_repr(&verdict))));
        }
        if !section.is_empty() && !store::SECTIONS.contains(&section.as_str()) {
            return Err(Fail::value(format!("section: {} is not exact or related", awm_pycompat::repr::str_repr(&section))));
        }
        let raw = |k: &str| args.get(k).filter(|v| truthy(v)).map(py_str).unwrap_or_default();
        if verdict == "clear" {
            let n = self.store.write(move |tx| store::unjudge(tx, &query, &source, &doc_id)).await?;
            return Ok(json!({"cleared": n}));
        }
        let v = Verdict {
            query,
            source,
            doc_id,
            verdict,
            section,
            collection: raw("set"),
            note: raw("note"),
            judge: as_.unwrap_or("").to_string(),
            ts: None,
        };
        let row = self.store.write(move |tx| store::judge(tx, &v)).await?;
        Ok(json!({"judged": row}))
    }

    /// `search judgements`, optionally of one set or one query.
    pub async fn judgements(&self, args: &Value) -> Result<Value, Fail> {
        let raw = |k: &str| args.get(k).filter(|v| truthy(v)).map(py_str).unwrap_or_default();
        let (set, query) = (raw("set"), raw("query"));
        let rows = self.store.read(move |c| store::judgements(c, &set, &query)).await?;
        Ok(json!({"judgements": rows}))
    }
}
