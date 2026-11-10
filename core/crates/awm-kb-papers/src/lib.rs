//! The papers source: the paper corpus as one LanceDB generation, ranked by bge-large.
//!
//! A generation is a directory under `<AWM_DIR>/services/kb/lance/papers/` that holds the store
//! (`lance/`) and the model files that encoded it (`model/`). The file `CURRENT` names the
//! generation kb serves. The import writes a whole generation, then replaces `CURRENT`, and the
//! next query switches to it. A node without `CURRENT` has no papers source.

pub mod import;
pub mod sync;

use awm_embed::{Embedder, Onnx, BGE_LARGE};
use awm_index::cells::Kind;
use awm_index::fields::{Class, Field, Fields};
use awm_index::grammar::SourceFilter;
use awm_index::sql::{id_list, Veil};
use awm_index::{Index, Spec, Store};
use awm_kb_source::{Ask, Source, SourceError};
use futures::future::BoxFuture;
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

pub const NAME: &str = "papers";
/// The file that names the served generation.
pub const CURRENT: &str = "CURRENT";
/// Fields the chunk rows do not copy: a filter on them resolves to paper ids first.
pub const ENTRY_ONLY: [&str; 3] = ["author", "cites", "cited_by"];
/// A filter matching at most this many papers narrows the vector search by id.
pub const BY_ID: usize = 5000;

pub fn fields() -> Fields {
    Fields(vec![
        Field::new("title", Class::Content),
        Field::new("text", Class::Content),
        Field::new("body", Class::Content),
        Field::new("ts", Class::Datetime),
        Field::new("collection", Class::Categorical),
        Field::new("author", Class::Categorical),
        Field::new("cites", Class::Categorical),
        Field::new("cited_by", Class::Categorical),
    ])
}

pub fn spec() -> Spec {
    Spec {
        name: NAME.into(),
        fields: fields(),
        hit: ["title", "ts", "collection", "doi"].map(String::from).to_vec(),
        extra: vec![("doi".into(), Kind::Text), ("journal".into(), Kind::Text)],
        section_by: None,
        dim: BGE_LARGE.dim,
        entry_only: ENTRY_ONLY.map(String::from).to_vec(),
    }
}

/// `<awm_dir>/services/kb/lance/papers`.
pub fn root(awm_dir: &Path) -> PathBuf {
    awm_dir.join("services").join("kb").join("lance").join(NAME)
}

/// One generation, opened.
pub(crate) struct Generation {
    pub(crate) name: String,
    pub(crate) index: Index,
    pub(crate) embedder: Option<Arc<dyn Embedder>>,
}

impl Generation {
    /// Open generation `name`, with `given` as its model or else the model it holds.
    async fn open(root: &Path, name: &str, given: Option<Arc<dyn Embedder>>) -> anyhow::Result<Generation> {
        let dir = root.join(name);
        let index = Store::open(&dir.join("lance")).await?.index(spec()).await?;
        if given.is_some() {
            return Ok(Generation { name: name.into(), index, embedder: given });
        }
        let model = dir.join("model");
        let embedder = match tokio::task::spawn_blocking(move || Onnx::load_dir(&model, &BGE_LARGE)).await? {
            Ok(m) => Some(Arc::new(m) as Arc<dyn Embedder>),
            Err(e) => {
                tracing::warn!("papers: generation {name} has no model, answering on keywords: {e}");
                None
            }
        };
        Ok(Generation { name: name.into(), index, embedder })
    }
}

pub struct Papers {
    pub(crate) root: PathBuf,
    fields: Fields,
    hit: Vec<String>,
    generation: RwLock<Arc<Generation>>,
    switching: tokio::sync::Mutex<()>,
    /// The model every generation uses instead of its own, for tests.
    given: Option<Arc<dyn Embedder>>,
}

fn current_name(root: &Path) -> Option<String> {
    std::fs::read_to_string(root.join(CURRENT)).ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

impl Papers {
    /// The papers source under `awm_dir`, or `None` when this node has no generation.
    pub async fn open(awm_dir: &Path) -> anyhow::Result<Option<Arc<Papers>>> {
        Self::at(&root(awm_dir), None).await
    }

    /// The papers source in `root`, ranking by `given` when set, else by each generation's model.
    pub async fn at(root: &Path, given: Option<Arc<dyn Embedder>>) -> anyhow::Result<Option<Arc<Papers>>> {
        let Some(name) = current_name(root) else { return Ok(None) };
        let generation = Generation::open(root, &name, given.clone()).await?;
        let spec = spec();
        Ok(Some(Arc::new(Papers {
            root: root.to_path_buf(),
            fields: spec.fields,
            hit: spec.hit,
            generation: RwLock::new(Arc::new(generation)),
            switching: tokio::sync::Mutex::new(()),
            given,
        })))
    }

    /// The generation `CURRENT` names, switching to it when it changed.
    pub(crate) async fn generation(&self) -> Result<Arc<Generation>, SourceError> {
        let held = self.generation.read().unwrap().clone();
        match current_name(&self.root) {
            Some(name) if name != held.name => {}
            _ => return Ok(held),
        }
        let _one = self.switching.lock().await;
        let Some(name) = current_name(&self.root) else { return Ok(held) };
        let held = self.generation.read().unwrap().clone();
        if name == held.name {
            return Ok(held);
        }
        let fresh = Arc::new(Generation::open(&self.root, &name, self.given.clone()).await.map_err(SourceError::of)?);
        tracing::info!("papers: serving generation {name}, was {}", held.name);
        *self.generation.write().unwrap() = fresh.clone();
        Ok(fresh)
    }

    /// The ask for one query: the filter on entries, and on chunks either the same filter, the
    /// matching ids, or nothing (near neighbours are then checked against the entries).
    async fn ask(&self, generation: &Generation, ask: &Ask) -> Result<awm_index::Ask, SourceError> {
        let vector = match (ask.text.as_ref().and_then(|t| t.about.clone()), generation.embedder.clone()) {
            (Some(about), Some(e)) => tokio::task::spawn_blocking(move || e.encode_query(&about)).await.map_err(SourceError::of)?.ok(),
            _ => None,
        };
        let mut q = awm_index::Ask::new(&self.fields, ask.text.as_ref(), &ask.source, &Veil::None, ask.max, vector).map_err(SourceError::of)?;
        if q.cond == "true" || q.vector.is_none() {
            return Ok(q);
        }
        let ids = generation.index.ids(&q.cond, Some(BY_ID + 1)).await.map_err(SourceError::of)?;
        q.chunk_cond = if ids.len() <= BY_ID {
            Some(if ids.is_empty() { "false".into() } else { id_list(&ids) })
        } else if names_entry_only(&ask.source) {
            Some("true".into())
        } else {
            None
        };
        Ok(q)
    }
}

fn names_entry_only(source: &SourceFilter) -> bool {
    let text = Value::Object(source.filter.clone()).to_string();
    ENTRY_ONLY.iter().any(|f| text.contains(&format!("\"{f}\"")))
}

impl Source for Papers {
    fn name(&self) -> &str {
        NAME
    }

    fn default(&self) -> bool {
        false
    }

    fn fields(&self) -> (&Fields, &[String]) {
        (&self.fields, &self.hit)
    }

    /// Papers rank by their own model, so search's MiniLM vector is no use here.
    fn takes_vectors(&self) -> bool {
        false
    }

    fn query(&self, ask: Ask) -> BoxFuture<'_, Result<Value, SourceError>> {
        Box::pin(async move {
            let generation = self.generation().await?;
            let q = self.ask(&generation, &ask).await?;
            let mut out = generation.index.answer(&q).await.map_err(SourceError::of)?.to_json();
            out["veiled"] = json!(true);
            Ok(out)
        })
    }

    fn get<'a>(&'a self, id: &'a str, _veil: &'a Veil, _session: Option<&'a str>) -> BoxFuture<'a, Result<Option<Map<String, Value>>, SourceError>> {
        Box::pin(async move { self.generation().await?.index.document(id, "true").await.map_err(SourceError::of) })
    }

    fn status(&self) -> BoxFuture<'_, Value> {
        Box::pin(async move {
            match self.generation().await {
                Ok(g) => json!({"count": g.index.count(None).await.ok(), "generation": g.name, "semantic": g.embedder.is_some()}),
                Err(e) => json!({"error": e.0}),
            }
        })
    }
}
