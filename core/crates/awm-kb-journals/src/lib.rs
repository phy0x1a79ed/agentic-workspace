//! The journal inside kb, a port of the Python `journal` plugin (`awm/services/journal`).
//!
//! A scope is a channel. Every post lives once, as one row of the `journals` LanceDB source
//! (`<AWM_DIR>/services/kb/lance/journals/`), with its tag ids and citations in that row.
//! journal.db (`<AWM_DIR>/services/journal/journal.db`, schema v6, opened in place) keeps the
//! relational state: the tag registry, its history and clashes, edit history, inbox cursors,
//! read receipts and session lineage. Its post tables are the pre-redux record, frozen after
//! the cutover that copies them into LanceDB.
//!
//! Write order, so that a crash never loses a record: an edit writes its `post_versions` row
//! before the post row, and the start reconciles a post row behind its newest version. Every
//! LanceDB write merges on the post id, so a replay never duplicates a post.

pub mod briefs;
pub mod caller;
pub mod channel;
pub mod cutover;
pub mod inbox;
pub mod lineage;
pub mod posts;
pub mod precedents;
pub mod render;
pub mod schema;
pub mod source;
pub mod tags;
pub mod tagsync;
pub mod verbs;
pub mod versions;

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex as StdMutex, OnceLock, RwLock};
use std::time::{Duration, Instant};

use awm_embed::Embedder;
use awm_index::{Index, Store as Lance};
use awm_plugin_sdk::{CallOpts, Context};
use awm_store::Store;
use futures_util::future::BoxFuture;
use serde_json::{json, Value};
use tokio::sync::Mutex;

pub use caller::Who;
pub use cutover::{export_back, Cutover};
pub use verbs::register;

/// A verb's failure as the Python raised it: the exception's class and its text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fail {
    pub class: &'static str,
    pub message: String,
}

impl Fail {
    pub fn new(class: &'static str, message: impl Into<String>) -> Self {
        Self { class, message: message.into() }
    }
    pub fn value(message: impl Into<String>) -> Self {
        Self::new("ValueError", message)
    }
    pub fn refused(message: impl Into<String>) -> Self {
        Self::new("Refused", message)
    }
    pub fn permission(message: impl Into<String>) -> Self {
        Self::new("PermissionError", message)
    }
    pub fn tag(message: impl Into<String>) -> Self {
        Self::new("TagError", message)
    }
    pub fn brief(message: impl Into<String>) -> Self {
        Self::new("BriefError", message)
    }
    pub fn runtime(message: impl Into<String>) -> Self {
        Self::new("RuntimeError", message)
    }
}

impl std::fmt::Display for Fail {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.class, self.message)
    }
}

impl std::error::Error for Fail {}

impl Fail {
    /// The verb error with this error's class; the SDK's blanket conversion would lose it.
    pub fn into_verb(self) -> awm_plugin_sdk::VerbError {
        awm_plugin_sdk::VerbError::new(self.class, self.message)
    }
}

impl From<awm_store::StoreError> for Fail {
    fn from(e: awm_store::StoreError) -> Self {
        Fail::new("OperationalError", e.to_string())
    }
}

impl From<awm_store::rusqlite::Error> for Fail {
    fn from(e: awm_store::rusqlite::Error) -> Self {
        Fail::new("OperationalError", e.to_string())
    }
}

impl From<awm_index::IndexError> for Fail {
    fn from(e: awm_index::IndexError) -> Self {
        Fail::runtime(e.to_string())
    }
}

impl From<lancedb::Error> for Fail {
    fn from(e: lancedb::Error) -> Self {
        Fail::runtime(e.to_string())
    }
}

pub type Result<T, E = Fail> = std::result::Result<T, E>;

/// What the journal asks of other services and nodes. Every call fails soft.
pub trait Outside: Send + Sync {
    /// `scope resolve {project, scope}`.
    fn resolve_scope<'a>(&'a self, project: &'a str, scope: &'a str) -> BoxFuture<'a, Result<Value, String>>;
    /// The nodes this node keeps the tag vocabulary in step with.
    fn peers(&self) -> Vec<String>;
    /// `journal tag_list {}` on `peer`.
    fn tag_list<'a>(&'a self, peer: &'a str) -> BoxFuture<'a, Result<Value, String>>;
    /// `journal tag_receive {tags}` on `peer`, fire and forget.
    fn tag_receive(&self, peer: &str, tags: Vec<Value>);
    /// Start the agent that renames the newer tag of a clash; returns its handle.
    fn start_agent<'a>(&'a self, args: Value) -> BoxFuture<'a, Result<Value, String>>;
}

fn edge_peers(awm_dir: &Path, here: &str) -> Vec<String> {
    match awm_trust::edges::EdgeList::load(&awm_trust::edges::edges_path(awm_dir)) {
        Ok(e) => e.reachable_from(here),
        Err(err) => {
            tracing::info!("no peers to sync tags with: {err:#}");
            vec![]
        }
    }
}

/// Before the start hook: no gateway, so every call fails soft.
pub struct Detached {
    pub awm_dir: PathBuf,
    pub here: String,
}

impl Outside for Detached {
    fn resolve_scope<'a>(&'a self, _: &'a str, _: &'a str) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(async { Err("no gateway connection".to_string()) })
    }
    fn peers(&self) -> Vec<String> {
        edge_peers(&self.awm_dir, &self.here)
    }
    fn tag_list<'a>(&'a self, _: &'a str) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(async { Err("no gateway connection".to_string()) })
    }
    fn tag_receive(&self, _: &str, _: Vec<Value>) {}
    fn start_agent<'a>(&'a self, _: Value) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(async { Err("no gateway connection".to_string()) })
    }
}

/// The gateway, through the running service's context.
pub struct HubOutside {
    pub ctx: Context,
    pub awm_dir: PathBuf,
    pub here: String,
}

impl HubOutside {
    async fn call(&self, tool: &str, verb: &str, args: Value, peer: Option<&str>, secs: u64) -> Result<Value, String> {
        let opts = CallOpts { peer: peer.map(str::to_string), timeout: Duration::from_secs(secs), ..CallOpts::default() };
        self.ctx.hub().call_with(tool, verb, args, &opts).await.map_err(|e| e.to_string())
    }
}

impl Outside for HubOutside {
    fn resolve_scope<'a>(&'a self, project: &'a str, scope: &'a str) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(self.call("scope", "resolve", json!({"project": project, "scope": scope}), None, 5))
    }
    fn peers(&self) -> Vec<String> {
        edge_peers(&self.awm_dir, &self.here)
    }
    fn tag_list<'a>(&'a self, peer: &'a str) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(self.call("journal", "tag_list", json!({}), Some(peer), 15))
    }
    fn tag_receive(&self, peer: &str, tags: Vec<Value>) {
        let (ctx, peer) = (self.ctx.clone(), peer.to_string());
        tokio::spawn(async move {
            let opts = CallOpts { peer: Some(peer.clone()), timeout: Duration::from_secs(15), ..CallOpts::default() };
            if let Err(e) = ctx.hub().call_with("journal", "tag_receive", json!({"tags": tags}), &opts).await {
                tracing::info!("tag sync to {peer} failed, the next pull heals it: {e}");
            }
        });
    }
    fn start_agent<'a>(&'a self, args: Value) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(self.call("reflection", "start_other", args, None, 60))
    }
}

/// Where and how to open the journal.
#[derive(Debug, Clone)]
pub struct Opts {
    /// This node's fleet name, as `config.node_name()` gives it.
    pub node: String,
    /// Other places a journal.db may sit on this node. A fresh database is refused while one
    /// of them holds a journal, so a node never grows a second, empty journal.
    pub other_dbs: Vec<PathBuf>,
}

impl Opts {
    /// The node's own name and the two places an older install keeps its journal.
    pub fn for_node(awm_dir: &Path, node: &str) -> Opts {
        Opts {
            node: node.to_string(),
            other_dbs: vec![
                PathBuf::from("/var/lib/awm/journal/services/journal/journal.db"),
                awm_dir.join("services").join("journal.moved").join("journal.db"),
            ],
        }
    }
}

/// The journal: its relational store, its LanceDB source and the hooks into the running service.
pub struct Journals {
    pub awm_dir: PathBuf,
    pub db: Store,
    pub index: Index,
    pub node: String,
    embedder: RwLock<Option<Arc<dyn Embedder>>>,
    write: Mutex<()>,
    seq: AtomicI64,
    ctx: OnceLock<Context>,
    outside: RwLock<Arc<dyn Outside>>,
    known_scopes: StdMutex<HashMap<(String, String), Instant>>,
    /// The cutover's outcome at open.
    pub cutover: Cutover,
}

/// How long a scope the scopes service confirmed stays confirmed.
const SCOPE_TTL: Duration = Duration::from_secs(60);

impl Journals {
    pub fn db_path(awm_dir: &Path) -> PathBuf {
        awm_dir.join("services").join("journal").join("journal.db")
    }

    pub fn lance_path(awm_dir: &Path) -> PathBuf {
        awm_dir.join("services").join("kb").join("lance").join("journals")
    }

    /// Open journal.db in place (migrating a node below v6), open the `journals` source, run
    /// the cutover on the first start and the startup reconcile. The cutover gate refuses the
    /// open until the source holds every post of `scope_posts`.
    pub async fn open(awm_dir: &Path, embedder: Option<Arc<dyn Embedder>>, opts: Opts) -> Result<Arc<Journals>> {
        let path = Self::db_path(awm_dir);
        if !path.exists() {
            if let Some(other) = opts.other_dbs.iter().find(|p| p.exists()) {
                return Err(Fail::runtime(format!(
                    "no journal at {}, but {} holds one: copy it here (VACUUM INTO) before the first start, so this node keeps one journal",
                    path.display(),
                    other.display()
                )));
            }
        }
        let db = Store::open(&path)?;
        db.init(&schema::FRESH, schema::SCHEMA_VERSION, &schema::MIGRATIONS)?;
        db.blocking_read(|c| c.execute_batch(&schema::builtins_sql()).map_err(Fail::from))?;
        let lance = Lance::open(&Self::lance_path(awm_dir)).await?;
        let index = lance.index(posts::spec()).await?;
        let here = awm_plugin_sdk::config::label(&opts.node);
        let j = Journals {
            awm_dir: awm_dir.to_path_buf(),
            db,
            index,
            node: opts.node.clone(),
            embedder: RwLock::new(embedder),
            write: Mutex::new(()),
            seq: AtomicI64::new(0),
            ctx: OnceLock::new(),
            outside: RwLock::new(Arc::new(Detached { awm_dir: awm_dir.to_path_buf(), here })),
            known_scopes: StdMutex::new(HashMap::new()),
            cutover: Cutover::default(),
        };
        j.seq.store(j.max_seq().await?, Ordering::SeqCst);
        let mut j = j;
        j.cutover = j.run_cutover().await?;
        j.reconcile().await?;
        Ok(Arc::new(j))
    }

    /// The start hook: hold the service context for the `posts` emitter and the gateway calls,
    /// start the tag pull, and embed what waits for the model.
    pub fn start(self: &Arc<Self>, ctx: Context) {
        let here = self.here();
        self.set_outside(Arc::new(HubOutside { ctx: ctx.clone(), awm_dir: self.awm_dir.clone(), here }));
        let _ = self.ctx.set(ctx);
        let me = self.clone();
        awm_plugin_sdk::spawn_supervised("journal:tag-pull", Duration::from_secs(30), move || {
            let me = me.clone();
            async move { me.pull_loop().await }
        });
        if self.embedder().is_some() {
            let me = self.clone();
            tokio::spawn(async move {
                if let Err(e) = me.embed_waiting().await {
                    tracing::warn!("journal: embedding the waiting posts failed: {e}");
                }
            });
        }
    }

    /// Replace what the journal calls outside itself; tests stand in for the gateway with it.
    pub fn set_outside(&self, outside: Arc<dyn Outside>) {
        *self.outside.write().unwrap_or_else(|p| p.into_inner()) = outside;
    }

    pub fn outside(&self) -> Arc<dyn Outside> {
        self.outside.read().unwrap_or_else(|p| p.into_inner()).clone()
    }

    pub fn embedder(&self) -> Option<Arc<dyn Embedder>> {
        self.embedder.read().unwrap_or_else(|p| p.into_inner()).clone()
    }

    /// The model loaded: keep it, and embed every post whose chunks wait for it.
    pub async fn set_embedder(&self, embedder: Arc<dyn Embedder>) -> Result<awm_index::Written> {
        *self.embedder.write().unwrap_or_else(|p| p.into_inner()) = Some(embedder);
        self.embed_waiting().await
    }

    pub async fn embed_waiting(&self) -> Result<awm_index::Written> {
        match self.embedder() {
            Some(e) => Ok(self.index.embed_waiting(e.as_ref()).await?),
            None => Ok(awm_index::Written::default()),
        }
    }

    /// This node as the edge list names it.
    pub fn here(&self) -> String {
        awm_plugin_sdk::config::label(&self.node)
    }

    pub fn edges(&self) -> awm_trust::edges::EdgeList {
        awm_trust::edges::EdgeList::load(&awm_trust::edges::edges_path(&self.awm_dir)).unwrap_or_default()
    }

    fn next_seq(&self) -> i64 {
        self.seq.fetch_add(1, Ordering::SeqCst) + 1
    }

    /// Publish a new post on the `posts` topic. Never fails the post.
    fn emit_post(&self, project: &str, scope: &str, post: &Value) {
        if let Some(ctx) = self.ctx.get() {
            ctx.emit("posts", json!({"project": project, "scope": scope, "post": post}));
        }
    }

    /// `owners.require_scope`: refuse a literal owner the scopes service says does not exist.
    /// A scopes service that cannot answer accepts the post.
    pub async fn require_scope(&self, project: &str, scope: &str) -> Result<()> {
        if project.is_empty() {
            return Ok(());
        }
        let key = (project.to_string(), scope.to_string());
        if self.known_scopes.lock().unwrap_or_else(|p| p.into_inner()).get(&key).is_some_and(|t| *t > Instant::now()) {
            return Ok(());
        }
        match self.outside().resolve_scope(project, scope).await {
            Err(e) => {
                tracing::warn!("resolveScope {project}/{scope} unavailable, accepting the post: {e}");
                Ok(())
            }
            Ok(res) if res.get("exists").is_some_and(truthy) => {
                self.known_scopes.lock().unwrap_or_else(|p| p.into_inner()).insert(key, Instant::now() + SCOPE_TTL);
                Ok(())
            }
            Ok(_) => Err(Fail::value(format!("no scope {project}/{scope}"))),
        }
    }
}

/// Python truthiness.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's `str()` of a JSON value.
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => awm_pycompat::repr::repr(other),
    }
}

/// Python's `repr()` of a JSON value.
pub fn py_repr(v: &Value) -> String {
    awm_pycompat::repr::repr(v)
}

/// `a.get(key)` as a string, None when absent or null.
pub fn opt_str(a: &Value, key: &str) -> Option<String> {
    match a.get(key) {
        None | Some(Value::Null) => None,
        Some(v) => Some(py_str(v)),
    }
}

/// `a.get(key) or None` as a string.
pub fn truthy_str(a: &Value, key: &str) -> Option<String> {
    a.get(key).filter(|v| truthy(v)).map(py_str)
}

/// `int(a.get(key, default))`.
pub fn int_arg(a: &Value, key: &str, default: i64) -> Result<i64> {
    match a.get(key) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Number(n)) => Ok(n.as_i64().unwrap_or_else(|| n.as_f64().unwrap_or(0.0).trunc() as i64)),
        Some(Value::Bool(b)) => Ok(*b as i64),
        Some(Value::String(s)) => awm_pycompat::pystr::strip(s)
            .parse::<i64>()
            .map_err(|_| Fail::value(format!("invalid literal for int() with base 10: {}", awm_pycompat::repr::str_repr(s)))),
        Some(other) => Err(Fail::new("TypeError", format!("int() argument must be a string, a bytes-like object or a real number, not '{}'", type_name(other)))),
    }
}

pub fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}
