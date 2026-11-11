//! The rig for search's tests: a LanceDB journals source like kb's, fake sources that stall,
//! fail or skip the veil, a fake gateway and fake peers. All text is synthetic.
#![allow(dead_code)]

use awm_embed::{Embedder, Stub};
use awm_index::fields::{Class, Field, Fields};
use awm_index::{Index, Spec, Store, Veil};
use awm_kb_search::{BoxFut, CallError, Gateway, Peers, Search};
use awm_kb_source::{Ask, Source, SourceError};
use awm_plugin_sdk::config::EdgeList;
use futures_util::future::BoxFuture;
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, Once};
use std::time::Duration;

pub const HERE: &str = "local";

static INIT: Once = Once::new();

/// One AWM_DIR and node name per test process, so no test reads the live marks or edges.
pub fn init() {
    INIT.call_once(|| {
        let dir = tempfile::tempdir().unwrap().keep();
        std::env::set_var("AWM_DIR", &dir);
        std::env::set_var("AWM_NODE_NAME", HERE);
        std::env::set_var("AWM_EDGES_FILE", dir.join("edges.yml"));
    });
}

/// The journal's fields.
pub fn journal_fields() -> Fields {
    Fields(vec![
        Field::new("title", Class::Content),
        Field::new("text", Class::Content),
        Field::new("ts", Class::Datetime),
        Field::new("project", Class::Categorical),
        Field::nested("scope", "/"),
        Field::new("tag", Class::Categorical),
        Field::new("replies_to", Class::Categorical),
    ])
}

pub fn hit() -> Vec<String> {
    vec!["title".into(), "ts".into()]
}

/// A kb source on awm-index, as journals and guide build theirs: it applies the veil itself.
pub struct Lance {
    pub name: String,
    pub index: Index,
    pub fields: Fields,
    pub hit: Vec<String>,
    pub default: bool,
    pub gets: Mutex<Vec<(String, Option<String>)>>,
}

impl Lance {
    pub async fn open(dir: &std::path::Path, name: &str, default: bool) -> Arc<Lance> {
        let fields = journal_fields();
        let spec = Spec { name: name.into(), fields: fields.clone(), hit: hit(), extra: vec![], section_by: Some("ts".into()), ..Default::default() };
        let index = Store::open(&dir.join(name)).await.unwrap().index(spec).await.unwrap();
        Arc::new(Lance { name: name.into(), index, fields, hit: hit(), default, gets: Mutex::new(vec![]) })
    }

    pub async fn put(&self, docs: &[(&str, Value)]) {
        let rows: Vec<Map<String, Value>> = docs
            .iter()
            .map(|(id, d)| {
                let mut m = d.as_object().unwrap().clone();
                m.insert("id".into(), json!(id));
                m
            })
            .collect();
        self.index.upsert(&rows, Some(&Stub::default())).await.unwrap();
    }
}

impl Source for Lance {
    fn name(&self) -> &str {
        &self.name
    }
    fn default(&self) -> bool {
        self.default
    }
    fn fields(&self) -> (&Fields, &[String]) {
        (&self.fields, &self.hit)
    }
    fn takes_vectors(&self) -> bool {
        true
    }
    fn query(&self, ask: Ask) -> BoxFuture<'_, Result<Value, SourceError>> {
        Box::pin(async move {
            let a = awm_index::Ask::new(&self.fields, ask.text.as_ref(), &ask.source, &ask.veil, ask.max, ask.vector).map_err(SourceError::of)?;
            let mut out = self.index.answer(&a).await.map_err(SourceError::of)?.to_json();
            out["veiled"] = json!(true);
            Ok(out)
        })
    }
    fn get<'a>(&'a self, id: &'a str, veil: &'a Veil, session: Option<&'a str>) -> BoxFuture<'a, Result<Option<Map<String, Value>>, SourceError>> {
        Box::pin(async move {
            self.gets.lock().unwrap().push((id.to_string(), session.map(String::from)));
            let cond = awm_index::sql::veil_sql(&self.fields, veil);
            self.index.document(id, &cond).await.map_err(SourceError::of)
        })
    }
    fn status(&self) -> BoxFuture<'_, Value> {
        Box::pin(async move { json!({"count": self.index.count(None).await.unwrap()}) })
    }
}

/// How a fake source behaves.
#[derive(Clone)]
pub enum Act {
    /// Answers these hits, saying it applied the veil.
    Hits(Value),
    /// Answers these hits, with no word of the veil.
    Unveiled(Value),
    Stall,
    Down(String),
}

pub struct Fake {
    pub name: String,
    pub act: Act,
    pub fields: Fields,
    pub hit: Vec<String>,
    pub asked: Mutex<Vec<Ask>>,
}

impl Fake {
    pub fn new(name: &str, act: Act) -> Arc<Fake> {
        Arc::new(Fake { name: name.into(), act, fields: journal_fields(), hit: hit(), asked: Mutex::new(vec![]) })
    }
}

impl Source for Fake {
    fn name(&self) -> &str {
        &self.name
    }
    fn default(&self) -> bool {
        false
    }
    fn fields(&self) -> (&Fields, &[String]) {
        (&self.fields, &self.hit)
    }
    fn takes_vectors(&self) -> bool {
        false
    }
    fn query(&self, ask: Ask) -> BoxFuture<'_, Result<Value, SourceError>> {
        self.asked.lock().unwrap().push(ask);
        let act = self.act.clone();
        Box::pin(async move {
            match act {
                Act::Hits(h) => Ok(json!({"exact": h, "veiled": true})),
                Act::Unveiled(h) => Ok(json!({"exact": h})),
                Act::Stall => {
                    futures_util::future::pending::<()>().await;
                    unreachable!()
                }
                Act::Down(e) => Err(SourceError(e)),
            }
        })
    }
    fn get<'a>(&'a self, id: &'a str, _veil: &'a Veil, _s: Option<&'a str>) -> BoxFuture<'a, Result<Option<Map<String, Value>>, SourceError>> {
        let act = self.act.clone();
        Box::pin(async move {
            match act {
                Act::Stall => {
                    futures_util::future::pending::<()>().await;
                    unreachable!()
                }
                Act::Down(e) => Err(SourceError(e)),
                _ => Ok(Some(json!({"id": id, "title": "fake"}).as_object().unwrap().clone())),
            }
        })
    }
    fn status(&self) -> BoxFuture<'_, Value> {
        Box::pin(async { json!({}) })
    }
}

type Handler = Arc<dyn Fn(&Value) -> Result<Value, CallError> + Send + Sync>;

/// The gateway: `awm services` rows, the loader's archives, and one handler per tool verb.
#[derive(Default)]
pub struct FakeGateway {
    pub rows: Mutex<Vec<Value>>,
    pub archives: Mutex<Vec<Value>>,
    pub handlers: Mutex<HashMap<(String, String), Handler>>,
    pub calls: Mutex<Vec<(String, String, Value)>>,
    pub down: Mutex<bool>,
}

impl FakeGateway {
    pub fn new() -> Arc<FakeGateway> {
        Arc::new(FakeGateway::default())
    }

    pub fn on(&self, tool: &str, verb: &str, f: impl Fn(&Value) -> Result<Value, CallError> + Send + Sync + 'static) {
        self.handlers.lock().unwrap().insert((tool.into(), verb.into()), Arc::new(f));
    }

    /// A ready service declaring live tables, its verbs answered by `f`.
    pub fn live(&self, service: &str, tool: &str, tables: Value) {
        self.rows.lock().unwrap().push(json!({"name": service, "plugin": service, "status": "ready", "tools": [tool], "archive": tables}));
    }

    /// A kind declared on disk, whether or not it runs here.
    pub fn declare(&self, plugin: &str, tables: Value) {
        self.archives.lock().unwrap().push(json!({"plugin": plugin, "service": plugin, "archive": tables}));
    }
}

impl Gateway for FakeGateway {
    fn services(&self) -> BoxFut<'_, Result<Vec<Value>, CallError>> {
        Box::pin(async move {
            if *self.down.lock().unwrap() {
                return Err(CallError::new("ConnectionError", "the gateway is down"));
            }
            Ok(self.rows.lock().unwrap().clone())
        })
    }
    fn plugins(&self) -> BoxFut<'_, Result<Value, CallError>> {
        Box::pin(async move { Ok(json!({"archives": self.archives.lock().unwrap().clone()})) })
    }
    fn call<'a>(&'a self, tool: &'a str, verb: &'a str, args: Value, _t: Duration) -> BoxFut<'a, Result<Value, CallError>> {
        Box::pin(async move {
            self.calls.lock().unwrap().push((tool.into(), verb.into(), args.clone()));
            let h = self.handlers.lock().unwrap().get(&(tool.to_string(), verb.to_string())).cloned();
            match h {
                Some(h) => h(&args),
                None => Err(CallError::new("ServiceCallError", format!("{tool}.{verb}: not installed"))),
            }
        })
    }
}

/// Other nodes: one answer per node, and the edge list that reaches them.
pub struct FakePeers {
    pub fleet: Mutex<Value>,
    pub answers: Mutex<HashMap<String, Handler>>,
    pub calls: Mutex<Vec<(String, String, Value)>>,
}

impl FakePeers {
    pub fn new() -> Arc<FakePeers> {
        Arc::new(FakePeers {
            fleet: Mutex::new(json!({"nodes": {HERE: {"role": "homebase"}}, "edges": {HERE: {}}})),
            answers: Mutex::new(HashMap::new()),
            calls: Mutex::new(vec![]),
        })
    }

    pub fn add(&self, name: &str, edge: bool, role: &str, f: impl Fn(&Value) -> Result<Value, CallError> + Send + Sync + 'static) {
        let mut fleet = self.fleet.lock().unwrap();
        fleet["nodes"][name] = json!({"role": role});
        if edge {
            fleet["edges"][HERE][name] = json!("lan");
        }
        self.answers.lock().unwrap().insert(name.into(), Arc::new(f));
    }

    pub fn asked(&self) -> Vec<String> {
        self.calls.lock().unwrap().iter().map(|c| c.0.clone()).collect()
    }
}

impl Peers for FakePeers {
    fn edges(&self) -> EdgeList {
        EdgeList::parse(&serde_yaml::to_string(&*self.fleet.lock().unwrap()).unwrap()).unwrap()
    }
    fn invoke<'a>(&'a self, peer: &'a str, tool: &'a str, verb: &'a str, args: Value, _t: Duration) -> BoxFut<'a, Result<Value, CallError>> {
        Box::pin(async move {
            self.calls.lock().unwrap().push((peer.into(), format!("{tool}_{verb}"), args.clone()));
            let h = self.answers.lock().unwrap().get(peer).cloned().expect("a peer the test added");
            h(&args)
        })
    }
}

pub struct Rig {
    pub dir: tempfile::TempDir,
    pub gateway: Arc<FakeGateway>,
    pub peers: Arc<FakePeers>,
    pub search: Arc<Search>,
    pub journals: Arc<Lance>,
}

/// The kinds the Python suite's manifests declared: kb and transcripts, beside kb's journals.
pub fn declare_python_kinds(g: &FakeGateway) {
    g.declare("papers", json!([
        {"name": "kb", "mode": "live", "query": "kb_query", "get": "kb_get", "hit": ["title", "ts"],
         "fields": {"title": "content", "text": "content", "ts": "datetime", "author": "categorical", "tag": "categorical",
                    "cites": "categorical", "cited_by": "categorical"}},
        {"name": "library", "mode": "live", "query": "kb_library", "get": "kb_get"},
    ]));
    g.declare("transcripts", json!([
        {"name": "transcripts", "mode": "live", "query": "transcripts_query", "optin": true, "hit": ["title", "ts"],
         "fields": {"title": "content", "text": "content", "ts": "datetime", "project": "categorical",
                    "scope": {"class": "categorical", "nested": "/"}}},
    ]));
}

/// search over a journals source and any `extra` sources.
pub async fn rig_with(extra: Vec<Arc<dyn Source>>) -> Rig {
    init();
    let dir = tempfile::tempdir().unwrap();
    let journals = Lance::open(&dir.path().join("lance"), "journals", true).await;
    let gateway = FakeGateway::new();
    declare_python_kinds(&gateway);
    let peers = FakePeers::new();
    let mut sources: Vec<Arc<dyn Source>> = vec![journals.clone()];
    sources.extend(extra);
    let emb: Arc<dyn Embedder> = Arc::new(Stub::default());
    let search = Search::new(&dir.path().join("search"), sources, Some(emb), gateway.clone(), peers.clone()).unwrap().named(HERE);
    Rig { dir, gateway, peers, search: Arc::new(search), journals }
}

pub async fn rig() -> Rig {
    rig_with(vec![]).await
}

pub fn doc(title: &str, text: &str, ts: &str) -> Value {
    json!({"title": title, "text": text, "ts": ts})
}

pub fn with(mut d: Value, extra: Value) -> Value {
    for (k, v) in extra.as_object().unwrap() {
        d[k] = v.clone();
    }
    d
}

pub fn ids(hits: &Value) -> Vec<String> {
    hits.as_array().map(|a| a.iter().map(|h| h["id"].as_str().unwrap().to_string()).collect()).unwrap_or_default()
}

pub fn set(hits: &Value) -> std::collections::BTreeSet<String> {
    ids(hits).into_iter().collect()
}

/// Every hit of an answer: source by source, each `exact` then its `related`.
pub fn every(result: &Value) -> Vec<Value> {
    let mut out = vec![];
    for (_, a) in result.as_object().unwrap() {
        for s in ["exact", "related"] {
            out.extend(a.get(s).and_then(Value::as_array).cloned().unwrap_or_default());
        }
    }
    out
}

pub fn every_ids(result: &Value) -> std::collections::BTreeSet<String> {
    every(result).iter().map(|h| h["id"].as_str().unwrap().to_string()).collect()
}

impl Rig {
    pub async fn ask(&self, args: Value) -> Value {
        self.search.query(&args, None, None).await.unwrap()
    }

    pub async fn ask_as(&self, args: Value, as_: Option<&str>) -> Result<Value, awm_kb_search::Fail> {
        self.search.query(&args, as_, None).await
    }
}
