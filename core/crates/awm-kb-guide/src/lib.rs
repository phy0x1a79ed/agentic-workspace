//! guide, the source inside kb that answers how to use awm. It is listed with `default: false`,
//! so a query reaches it only by naming it. It holds the workspace docs and awm's `docs/`, each
//! installed plugin's `README.md`, `INSTALL.md` and shipped skills, and each tool's describe,
//! in its own LanceDB at `<AWM_DIR>/services/kb/lance/guide/`.
//!
//! Nothing runs on a clock. The gateway's `awm/plugins` events re-index a plugin on install,
//! update and enable, mask it on disable, and delete it on removal. Each connect to the topic
//! reconciles against the loader's snapshot, so a dropped socket loses nothing. A unit is
//! written again only when the digest of what it was built from changed.

pub mod doc;

use awm_embed::Embedder;
use awm_index::fields::{Class, Field, Fields};
use awm_index::grammar::SourceFilter;
use awm_index::{Index, Spec, Veil};
use awm_kb_source::{Ask, Source, SourceError};
use futures_util::future::BoxFuture;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

pub const NAME: &str = "guide";
/// The unit of the workspace's own docs: an awm deploy is an install of awm itself.
pub const AWM: &str = "awm";
/// The workspace docs, beside awm's `docs/`.
pub const WORKSPACE_DOCS: [&str; 5] = ["AGENTS.md", "PROTOCOLS.md", "FEDERATION.md", "ARCHITECTURE.md", "README.md"];
/// A plugin's own docs, beside its `skills/`.
pub const PLUGIN_DOCS: [&str; 2] = ["README.md", "INSTALL.md"];
const DESCRIBE_TIMEOUT: Duration = Duration::from_secs(10);

/// What guide asks the gateway. Tests stand in for it.
pub trait GuideHub: Send + Sync {
    /// `awm plugins`: the loader's snapshot of loaded, skipped and refused plugins.
    fn plugins(&self) -> BoxFuture<'_, Result<Value, String>>;
    /// `<tool> describe`: every verb of a tool, with its params.
    fn describe<'a>(&'a self, tool: &'a str) -> BoxFuture<'a, Result<Value, String>>;
}

/// The gateway through the SDK's hub client.
pub struct HubGuide(pub awm_plugin_sdk::Hub);

impl GuideHub for HubGuide {
    fn plugins(&self) -> BoxFuture<'_, Result<Value, String>> {
        Box::pin(async move {
            let opts = awm_plugin_sdk::CallOpts { timeout: DESCRIBE_TIMEOUT, ..Default::default() };
            self.0.call_with("awm", "plugins", json!({}), &opts).await.map_err(|e| e.to_string())
        })
    }

    fn describe<'a>(&'a self, tool: &'a str) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(async move {
            let opts = awm_plugin_sdk::CallOpts { timeout: DESCRIBE_TIMEOUT, ..Default::default() };
            self.0.call_with(tool, "describe", json!({}), &opts).await.map_err(|e| e.to_string())
        })
    }
}

/// guide's fields: the content, and the categoricals a hit is placed by.
pub fn fields() -> Fields {
    Fields(vec![
        Field::new("title", Class::Content),
        Field::new("text", Class::Content),
        Field::new("section", Class::Categorical),
        Field::new("plugin", Class::Categorical),
        Field::new("file", Class::Categorical),
        Field::new("heading", Class::Categorical),
        Field::new("tool", Class::Categorical),
        Field::new("verb", Class::Categorical),
    ])
}

pub fn hit() -> Vec<String> {
    ["title", "plugin", "file", "heading"].map(String::from).to_vec()
}

/// One written unit: a plugin's docs or its tools, and what they were built from.
#[derive(Debug, Clone, Default, PartialEq)]
struct Unit {
    digest: String,
    ids: Vec<String>,
}

/// What guide has indexed, kept beside kb's LanceDB in `guide.json`.
#[derive(Debug, Clone, Default)]
struct State {
    units: BTreeMap<String, Unit>,
    masked: BTreeSet<String>,
}

impl State {
    fn load(path: &Path) -> State {
        let Ok(text) = std::fs::read_to_string(path) else { return State::default() };
        let Ok(v) = serde_json::from_str::<Value>(&text) else {
            tracing::warn!("guide: {} unreadable; every unit is written again", path.display());
            return State::default();
        };
        let units = v["units"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(k, u)| {
                let ids = u["ids"].as_array().into_iter().flatten().filter_map(Value::as_str).map(String::from).collect();
                (k.clone(), Unit { digest: u["digest"].as_str().unwrap_or("").into(), ids })
            })
            .collect();
        let masked = v["masked"].as_array().into_iter().flatten().filter_map(Value::as_str).map(String::from).collect();
        State { units, masked }
    }

    fn save(&self, path: &Path) -> std::io::Result<()> {
        let units: Map<String, Value> = self.units.iter().map(|(k, u)| (k.clone(), json!({"digest": u.digest, "ids": u.ids}))).collect();
        let text = serde_json::to_string(&json!({"units": units, "masked": self.masked}))?;
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, text)?;
        std::fs::rename(tmp, path)
    }
}

/// The plugin a unit key `docs:<plugin>` or `tools:<plugin>` belongs to.
fn plugin_of(unit: &str) -> &str {
    unit.split_once(':').map_or(unit, |(_, p)| p)
}

/// One plugin as the loader's snapshot names it.
#[derive(Debug, Clone)]
struct Plugin {
    name: String,
    dir: PathBuf,
    tools: Vec<String>,
}

#[derive(Debug, Clone, Default)]
struct Snapshot {
    loaded: Vec<Plugin>,
    /// Skipped by the install config, or refused: their rows stay, masked.
    held: BTreeSet<String>,
}

impl Snapshot {
    fn read(v: &Value) -> Snapshot {
        let loaded = v["plugins"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|p| {
                Some(Plugin {
                    name: p["name"].as_str()?.to_string(),
                    dir: PathBuf::from(p["dir"].as_str().unwrap_or("")),
                    tools: p["tools"].as_array().into_iter().flatten().filter_map(|t| t["name"].as_str().or(t.as_str())).map(String::from).collect(),
                })
            })
            .collect();
        let mut held: BTreeSet<String> = v["skipped"].as_array().into_iter().flatten().filter_map(Value::as_str).map(String::from).collect();
        held.extend(v["refused"].as_array().into_iter().flatten().filter_map(|r| r["plugin"].as_str()).map(String::from));
        Snapshot { loaded, held }
    }

    fn get(&self, name: &str) -> Option<&Plugin> {
        self.loaded.iter().find(|p| p.name == name)
    }
}

/// What one reconcile or event changed, for the log and the tests.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Changed {
    pub indexed: Vec<String>,
    pub masked: Vec<String>,
    pub unmasked: Vec<String>,
    pub deleted: Vec<String>,
}

pub struct Guide {
    index: Index,
    fields: Fields,
    hit: Vec<String>,
    embedder: Option<Arc<dyn Embedder>>,
    hub: Arc<dyn GuideHub>,
    workspace: PathBuf,
    state_path: PathBuf,
    state: tokio::sync::Mutex<State>,
    /// The masked plugins, readable while a write holds `state`.
    masks: std::sync::RwLock<BTreeSet<String>>,
    last: std::sync::Mutex<(Option<String>, Option<String>)>,
}

fn digest<'a>(parts: impl IntoIterator<Item = &'a str>) -> String {
    let mut h = Sha256::new();
    for p in parts {
        h.update(p.as_bytes());
        h.update([0u8]);
    }
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

fn row(id: String, title: &str, text: &str, section: &str, plugin: &str, file: &str, heading: &str, tool: &str, verb: &str) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("id".into(), json!(id));
    for (k, v) in [("title", title), ("text", text), ("section", section), ("plugin", plugin), ("file", file), ("heading", heading), ("tool", tool), ("verb", verb)] {
        m.insert(k.into(), json!(v));
    }
    m
}

/// Every `*.md` under `dir`, sorted, following no links.
fn markdown_under(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(read) = std::fs::read_dir(dir) else { return };
    let mut entries: Vec<_> = read.filter_map(Result::ok).collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let Ok(kind) = e.file_type() else { continue };
        let path = e.path();
        if kind.is_dir() {
            markdown_under(&path, out);
        } else if kind.is_file() && path.extension().is_some_and(|x| x == "md") {
            out.push(path);
        }
    }
}

impl Guide {
    /// Open guide's LanceDB under `awm_dir` and its state beside it. `workspace` is the deployed
    /// checkout whose docs it holds.
    pub async fn open(awm_dir: &Path, workspace: &Path, embedder: Option<Arc<dyn Embedder>>, hub: Arc<dyn GuideHub>) -> anyhow::Result<Arc<Guide>> {
        let kb = awm_dir.join("services").join("kb");
        let store = awm_index::Store::open(&kb.join("lance").join(NAME)).await?;
        let (fields, hit) = (fields(), hit());
        let index = store.index(Spec { name: NAME.into(), fields: fields.clone(), hit: hit.clone(), extra: vec![], section_by: None }).await?;
        let state_path = kb.join("guide.json");
        Ok(Arc::new(Guide {
            index,
            fields,
            hit,
            embedder,
            hub,
            workspace: workspace.to_path_buf(),
            masks: std::sync::RwLock::new(State::load(&state_path).masked),
            state: tokio::sync::Mutex::new(State::load(&state_path)),
            state_path,
            last: std::sync::Mutex::new((None, None)),
        }))
    }

    pub fn index(&self) -> &Index {
        &self.index
    }

    fn shown(&self, path: &Path) -> String {
        path.strip_prefix(&self.workspace).unwrap_or(path).to_string_lossy().into_owned()
    }

    fn file_rows(&self, unit: &str, section: &str, files: &[PathBuf]) -> (Vec<Map<String, Value>>, String) {
        let mut rows = Vec::new();
        let mut texts: Vec<(String, String)> = Vec::new();
        for path in files {
            let Ok(text) = std::fs::read_to_string(path) else { continue };
            let file = self.shown(path);
            let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            for part in doc::split(&text, &name) {
                let id = format!("doc:{unit}/{file}#{}", part.slug);
                rows.push(row(id, &part.title, &part.text, section, unit, &file, &part.heading, "", ""));
            }
            texts.push((file, text));
        }
        let d = digest(texts.iter().flat_map(|(f, t)| [f.as_str(), t.as_str()]));
        (rows, d)
    }

    fn workspace_files(&self) -> Vec<PathBuf> {
        let mut files: Vec<PathBuf> = WORKSPACE_DOCS.iter().map(|n| self.workspace.join(n)).filter(|p| p.is_file()).collect();
        markdown_under(&self.workspace.join("docs"), &mut files);
        files
    }

    fn plugin_files(dir: &Path) -> Vec<PathBuf> {
        let mut files: Vec<PathBuf> = PLUGIN_DOCS.iter().map(|n| dir.join(n)).filter(|p| p.is_file()).collect();
        markdown_under(&dir.join("skills"), &mut files);
        files
    }

    async fn tool_rows(&self, plugin: &str, tools: &[String]) -> Option<(Vec<Map<String, Value>>, String)> {
        let mut rows = Vec::new();
        for tool in tools {
            let described = match self.hub.describe(tool).await {
                Ok(d) => d,
                Err(e) => {
                    tracing::info!("guide: {tool} gave no describe yet ({e}); its service's start indexes it");
                    return None;
                }
            };
            for verb in described["verbs"].as_array().into_iter().flatten() {
                if let Some((name, title, text)) = doc::verb_entry(tool, verb) {
                    rows.push(row(format!("tool:{tool}.{name}"), &title, &text, "tools", plugin, "", "", tool, &name));
                }
            }
        }
        let blob = serde_json::to_string(&rows).unwrap_or_default();
        let d = digest([blob.as_str()]);
        Some((rows, d))
    }

    /// Write a unit when what it is built from changed. Ids it no longer has go first.
    async fn write_unit(&self, state: &mut State, key: &str, rows: Vec<Map<String, Value>>, d: String) -> anyhow::Result<bool> {
        if state.units.get(key).is_some_and(|u| u.digest == d) {
            return Ok(false);
        }
        let ids: Vec<String> = rows.iter().filter_map(|r| r["id"].as_str().map(String::from)).collect();
        let keep: BTreeSet<&String> = ids.iter().collect();
        let gone: Vec<String> = state.units.get(key).map(|u| u.ids.iter().filter(|i| !keep.contains(i)).cloned().collect()).unwrap_or_default();
        if !gone.is_empty() {
            self.index.delete(&gone).await?;
        }
        if !rows.is_empty() {
            let written = self.index.upsert(&rows, self.embedder.as_deref()).await?;
            if let Some(e) = written.error {
                tracing::warn!("guide: {key} stored, its vectors wait: {e}");
            }
        }
        state.units.insert(key.into(), Unit { digest: d, ids });
        self.save(state);
        Ok(true)
    }

    async fn drop_unit(&self, state: &mut State, key: &str) -> anyhow::Result<bool> {
        let Some(unit) = state.units.remove(key) else { return Ok(false) };
        self.index.delete(&unit.ids).await?;
        self.save(state);
        Ok(true)
    }

    fn save(&self, state: &State) {
        if let Err(e) = state.save(&self.state_path) {
            tracing::warn!("guide: {} not saved: {e}", self.state_path.display());
        }
    }

    fn set_mask(&self, state: &mut State, plugin: &str, masked: bool, changed: &mut Changed) {
        let did = if masked { state.masked.insert(plugin.into()) } else { state.masked.remove(plugin) };
        if did {
            *self.masks.write().unwrap() = state.masked.clone();
            let list = if masked { &mut changed.masked } else { &mut changed.unmasked };
            list.push(plugin.into());
            self.save(state);
        }
    }

    async fn index_plugin(&self, state: &mut State, p: &Plugin, changed: &mut Changed) -> anyhow::Result<()> {
        let (rows, d) = self.file_rows(&p.name, "plugins", &Self::plugin_files(&p.dir));
        if self.write_unit(state, &format!("docs:{}", p.name), rows, d).await? {
            changed.indexed.push(format!("docs:{}", p.name));
        }
        if let Some((rows, d)) = self.tool_rows(&p.name, &p.tools).await {
            if self.write_unit(state, &format!("tools:{}", p.name), rows, d).await? {
                changed.indexed.push(format!("tools:{}", p.name));
            }
        }
        self.set_mask(state, &p.name, false, changed);
        Ok(())
    }

    async fn forget(&self, state: &mut State, plugin: &str, changed: &mut Changed) -> anyhow::Result<()> {
        for key in [format!("docs:{plugin}"), format!("tools:{plugin}")] {
            if self.drop_unit(state, &key).await? {
                changed.deleted.push(key);
            }
        }
        self.set_mask(state, plugin, false, &mut Changed::default());
        Ok(())
    }

    async fn one(&self, state: &mut State, name: &str, snap: &Snapshot, changed: &mut Changed) -> anyhow::Result<()> {
        if let Some(p) = snap.get(name) {
            self.index_plugin(state, p, changed).await
        } else if snap.held.contains(name) {
            self.set_mask(state, name, true, changed);
            Ok(())
        } else {
            self.forget(state, name, changed).await
        }
    }

    fn noted(&self, out: &anyhow::Result<Changed>) {
        let mut last = self.last.lock().unwrap();
        last.0 = Some(awm_pycompat::time::now_iso());
        last.1 = out.as_ref().err().map(|e| format!("{e:#}"));
    }

    /// Bring guide in line with the loader's snapshot and the workspace: what is loaded is
    /// indexed and shown, what is skipped or refused is masked, what is gone is deleted.
    pub async fn reconcile(&self) -> anyhow::Result<Changed> {
        let out = self.reconcile_inner().await;
        self.noted(&out);
        out
    }

    async fn reconcile_inner(&self) -> anyhow::Result<Changed> {
        let snap = Snapshot::read(&self.hub.plugins().await.map_err(anyhow::Error::msg)?);
        let mut state = self.state.lock().await;
        let mut changed = Changed::default();
        let (rows, d) = self.file_rows(AWM, "docs", &self.workspace_files());
        if self.write_unit(&mut state, &format!("docs:{AWM}"), rows, d).await? {
            changed.indexed.push(format!("docs:{AWM}"));
        }
        let mut names: BTreeSet<String> = snap.loaded.iter().map(|p| p.name.clone()).collect();
        names.extend(snap.held.iter().cloned());
        names.extend(state.units.keys().map(|k| plugin_of(k).to_string()).filter(|p| p != AWM));
        names.extend(state.masked.iter().cloned());
        for name in names {
            self.one(&mut state, &name, &snap, &mut changed).await?;
        }
        if !changed.indexed.is_empty() || !changed.deleted.is_empty() {
            tracing::info!("guide: reconciled: {changed:?}");
        }
        Ok(changed)
    }

    /// One `awm/plugins` event, `{plugin, event, version, root}`. The snapshot decides what the
    /// plugin is now; without one, the event does.
    pub async fn apply(&self, event: &Value) -> anyhow::Result<Changed> {
        let out = self.apply_inner(event).await;
        if out.is_err() {
            self.noted(&out);
        }
        out
    }

    async fn apply_inner(&self, event: &Value) -> anyhow::Result<Changed> {
        let Some(name) = event["plugin"].as_str().filter(|n| !n.is_empty()) else { return Ok(Changed::default()) };
        let kind = event["event"].as_str().unwrap_or("");
        let mut changed = Changed::default();
        match self.hub.plugins().await {
            Ok(v) => {
                let snap = Snapshot::read(&v);
                let mut state = self.state.lock().await;
                self.one(&mut state, name, &snap, &mut changed).await?;
            }
            Err(e) => {
                tracing::warn!("guide: plugin snapshot unread ({e}); acting on the {kind} event alone");
                let mut state = self.state.lock().await;
                match kind {
                    "removed" => self.forget(&mut state, name, &mut changed).await?,
                    "disabled" | "refused" => self.set_mask(&mut state, name, true, &mut changed),
                    _ => {
                        let root = event["root"].as_str().unwrap_or("");
                        let p = Plugin { name: name.into(), dir: Path::new(root).join(name), tools: vec![] };
                        self.index_plugin(&mut state, &p, &mut changed).await?;
                    }
                }
            }
        }
        Ok(changed)
    }

    /// One `awm/services` event: a service that comes `up` describes its tools, so its plugin's
    /// tools are indexed again.
    pub async fn service_event(&self, event: &Value) -> anyhow::Result<Changed> {
        let mut changed = Changed::default();
        if event["event"].as_str() != Some("up") {
            return Ok(changed);
        }
        let Some(name) = event["plugin"].as_str().filter(|n| !n.is_empty()) else { return Ok(changed) };
        let snap = Snapshot::read(&self.hub.plugins().await.map_err(anyhow::Error::msg)?);
        let Some(p) = snap.get(name) else { return Ok(changed) };
        let mut state = self.state.lock().await;
        if let Some((rows, d)) = self.tool_rows(name, &p.tools).await {
            if self.write_unit(&mut state, &format!("tools:{name}"), rows, d).await? {
                changed.indexed.push(format!("tools:{name}"));
            }
        }
        Ok(changed)
    }

    /// Follow `awm/plugins` and `awm/services` through `hub` for good. Each connect to
    /// `awm/plugins` reconciles first. A dropped socket reconnects after a backoff.
    pub fn follow(self: &Arc<Self>, hub: awm_plugin_sdk::Hub) -> Vec<tokio::task::JoinHandle<()>> {
        ["plugins", "services"]
            .into_iter()
            .map(|topic| {
                let (g, hub) = (self.clone(), hub.clone());
                tokio::spawn(async move { g.follow_topic(hub, topic).await })
            })
            .collect()
    }

    async fn follow_topic(self: Arc<Self>, hub: awm_plugin_sdk::Hub, topic: &'static str) {
        let mut backoff = Duration::from_secs(2);
        let mut reconciled = false;
        loop {
            match hub.subscribe("awm", topic, None, None).await {
                Ok(mut sub) => {
                    backoff = Duration::from_secs(2);
                    if topic == "plugins" {
                        reconciled = true;
                        if let Err(e) = self.reconcile().await {
                            tracing::warn!("guide: reconcile failed: {e:#}");
                        }
                    }
                    while let Some(event) = sub.next().await {
                        let out = if topic == "plugins" { self.apply(&event).await } else { self.service_event(&event).await };
                        if let Err(e) = out {
                            tracing::warn!("guide: awm/{topic} event not applied: {e:#}");
                        }
                    }
                    tracing::info!("guide: awm/{topic} socket closed; reconnecting");
                }
                Err(e) => {
                    tracing::warn!("guide: cannot follow awm/{topic}: {e}");
                    // A gateway without the topic still gets the start's reconcile.
                    if topic == "plugins" && !reconciled {
                        reconciled = true;
                        if let Err(e) = self.reconcile().await {
                            tracing::warn!("guide: reconcile failed: {e:#}");
                        }
                    }
                }
            }
            tokio::time::sleep(backoff).await;
            backoff = (backoff * 2).min(Duration::from_secs(30));
        }
    }

    /// The caller's filter with the masked plugins left out, and for a caller on another node,
    /// the singleton tools, which it may reach only through their host's door.
    async fn scoped(&self, filter: &Map<String, Value>, veil: &Veil) -> Map<String, Value> {
        let mut parts: Vec<Value> = Vec::new();
        if !filter.is_empty() {
            parts.push(Value::Object(filter.clone()));
        }
        let masked: Vec<String> = self.masks.read().unwrap().iter().cloned().collect();
        if !masked.is_empty() {
            parts.push(json!({"plugin": {"$nin": masked}}));
        }
        if *veil != Veil::None {
            let edges = awm_plugin_sdk::config::edges().unwrap_or_default();
            let singletons: Vec<String> = edges.singletons.iter().map(|s| s.tool.clone()).collect();
            if !singletons.is_empty() {
                parts.push(json!({"tool": {"$nin": singletons}}));
            }
        }
        match parts.len() {
            0 => Map::new(),
            1 => parts.pop().and_then(|p| p.as_object().cloned()).unwrap_or_default(),
            _ => json!({"$and": parts}).as_object().cloned().unwrap_or_default(),
        }
    }
}

impl Source for Guide {
    fn name(&self) -> &str {
        NAME
    }

    fn default(&self) -> bool {
        false
    }

    fn fields(&self) -> (&Fields, &[String]) {
        (&self.fields, &self.hit)
    }

    fn takes_vectors(&self) -> bool {
        true
    }

    fn query(&self, ask: Ask) -> BoxFuture<'_, Result<Value, SourceError>> {
        Box::pin(async move {
            let filter = self.scoped(&ask.source.filter, &ask.veil).await;
            let source = SourceFilter { filter, sort: ask.source.sort.clone() };
            let ask = awm_index::Ask::new(&self.fields, ask.text.as_ref(), &source, &ask.veil, ask.max, ask.vector).map_err(SourceError::of)?;
            let mut out = self.index.answer(&ask).await.map_err(SourceError::of)?.to_json();
            out["veiled"] = json!(true);
            Ok(out)
        })
    }

    fn get<'a>(&'a self, id: &'a str, veil: &'a Veil, _session: Option<&'a str>) -> BoxFuture<'a, Result<Option<Map<String, Value>>, SourceError>> {
        Box::pin(async move {
            let filter = self.scoped(&Map::new(), veil).await;
            let cond = awm_index::sql::filter_sql(&self.fields, &filter).map_err(SourceError::of)?;
            self.index.document(id, &cond).await.map_err(SourceError::of)
        })
    }

    fn status(&self) -> BoxFuture<'_, Value> {
        Box::pin(async move {
            let filter = self.scoped(&Map::new(), &Veil::None).await;
            let cond = awm_index::sql::filter_sql(&self.fields, &filter).unwrap_or_else(|_| "true".into());
            let units = self.state.try_lock().ok().map(|s| s.units.len());
            let masked: Vec<String> = self.masks.read().unwrap().iter().cloned().collect();
            let (at, error) = self.last.lock().unwrap().clone();
            json!({
                "count": self.index.count(Some(&cond)).await.ok(),
                "stored": self.index.count(None).await.ok(),
                "units": units,
                "masked": masked,
                "reconciled": at,
                "error": error,
            })
        })
    }
}
