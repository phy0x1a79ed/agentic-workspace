//! The opt-in search source `transcripts`: search asks it in-process, and it answers from the
//! full-text index, after catching it up with what the live transcripts gained. A peer sees nothing of a marked project, nor of a session that
//! entered a marked path, and its `max` fills with hits it may see.

use std::sync::Arc;

use awm_index::fields::Fields;
use awm_index::sql::{and, Veil};
use awm_kb_source::{Ask, Source, SourceError};
use futures_util::future::BoxFuture;
use serde_json::{json, Map, Value};

use crate::index::{self, title_of, TxIndex};
use crate::App;

pub struct Transcripts {
    app: Arc<App>,
    fields: Fields,
    hit: Vec<String>,
}

impl Transcripts {
    pub fn new(app: Arc<App>) -> Self {
        let spec = index::spec();
        Transcripts { app, fields: spec.fields, hit: spec.hit }
    }

    fn ix(&self) -> Result<&TxIndex, SourceError> {
        self.app.ix().map_err(|e| SourceError(e.message))
    }
}

/// Fill each hit's title with the head of its line's text: the title column holds none.
async fn titled(ix: &TxIndex, hits: &mut [Map<String, Value>]) -> Result<(), SourceError> {
    let ids: Vec<String> = hits.iter().filter_map(|h| h.get("id").and_then(Value::as_str)).map(str::to_string).collect();
    if ids.is_empty() {
        return Ok(());
    }
    let texts = ix.texts(&ids).await.map_err(SourceError::of)?;
    for h in hits.iter_mut() {
        let id = h.get("id").and_then(Value::as_str).unwrap_or("").to_string();
        if h.contains_key("title") {
            h.insert("title".into(), json!(title_of(texts.get(&id).map_or("", String::as_str))));
        }
    }
    Ok(())
}

impl Source for Transcripts {
    fn name(&self) -> &str {
        index::SOURCE
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
        Box::pin(async move {
            if let Err(e) = self.app.catch_up().await {
                tracing::warn!("transcripts: live sessions not caught up, answering from the index: {}", e.message);
            }
            let ix = self.ix()?;
            let Some(narrow) = ix.narrow(None, ask.veil != Veil::None) else {
                return Ok(json!({"exact": []}));
            };
            let mut q = awm_index::Ask::new(&self.fields, ask.text.as_ref(), &ask.source, &ask.veil, ask.max, None).map_err(SourceError::of)?;
            q.cond = and(&[&q.cond, &narrow]);
            let mut answer = ix.index.answer(&q).await.map_err(SourceError::of)?;
            titled(ix, &mut answer.exact).await?;
            if let Some(related) = answer.related.as_mut() {
                titled(ix, related).await?;
            }
            Ok(answer.to_json())
        })
    }

    fn get<'a>(&'a self, id: &'a str, veil: &'a Veil, _session: Option<&'a str>) -> BoxFuture<'a, Result<Option<Map<String, Value>>, SourceError>> {
        Box::pin(async move {
            let ix = self.ix()?;
            let Some(narrow) = ix.narrow(None, *veil != Veil::None) else { return Ok(None) };
            let cond = and(&[&awm_index::sql::veil_sql(&self.fields, veil), &narrow]);
            let Some(mut doc) = ix.index.document(id, &cond).await.map_err(SourceError::of)? else { return Ok(None) };
            let text = doc.get("text").and_then(Value::as_str).unwrap_or("").to_string();
            doc.insert("title".into(), json!(title_of(&text)));
            Ok(Some(doc))
        })
    }

    fn status(&self) -> BoxFuture<'_, Value> {
        Box::pin(async move {
            match self.ix() {
                Ok(ix) => ix.status().await,
                Err(e) => json!({"error": e.0}),
            }
        })
    }
}
