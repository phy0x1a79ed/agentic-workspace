//! The engine against a temp store, with the stub embedder. All text is synthetic.

use awm_embed::{EmbedError, Embedder, Stub};
use awm_index::grammar::{parse_source, parse_text};
use awm_index::{Ask, Class, Field, Fields, Index, Kind, Mark, Spec, Store, Veil};
use serde_json::{json, Map, Value};
use std::sync::atomic::Ordering;

fn spec() -> Spec {
    Spec {
        name: "posts".into(),
        fields: Fields(vec![
            Field::new("title", Class::Content),
            Field::new("text", Class::Content),
            Field::new("ts", Class::Datetime),
            Field::new("project", Class::Categorical),
            Field::nested("scope", "/"),
            Field::new("tag", Class::Categorical),
        ]),
        hit: vec!["title".into(), "ts".into()],
        extra: vec![("author".into(), Kind::Text)],
        section_by: Some("ts".into()),
    }
}

fn row(id: &str, title: &str, text: &str, ts: &str, project: &str, scope: &str) -> Map<String, Value> {
    json!({"id": id, "title": title, "text": text, "ts": ts, "project": [project], "scope": [scope],
           "tag": ["journal"], "author": "agent-1"})
    .as_object()
    .unwrap()
    .clone()
}

fn corpus() -> Vec<Map<String, Value>> {
    vec![
        row("p1", "Lantern repair notes", "The brass lantern needed a new wick and a glass chimney.", "2026-03-01T10:00:00+00:00", "alpha", "main"),
        row("p2", "Garden plan", "Plant tomatoes along the south fence; basil between them.", "2026-04-02T09:30:00+00:00", "alpha", "main/beds"),
        row("p3", "Kettle descaling", "Vinegar and water dissolve the scale inside the kettle.", "2025-11-20T08:00:00+00:00", "beta", "main"),
        row("p4", "Lantern shopping list", "Wick, lamp oil, a spare chimney for the lantern.", "2025-06-15T12:00:00+00:00", "beta", "side"),
        row("p5", "Undated thought", "A note about the river and the old mill.", "", "gamma", "main"),
    ]
}

async fn open(dir: &tempfile::TempDir) -> Index {
    Store::open(dir.path()).await.unwrap().index(spec()).await.unwrap()
}

fn ask(index: &Index, text: Value, filter: Value, veil: &Veil, limit: usize, emb: Option<&Stub>) -> Ask {
    let text = if text.is_null() { None } else { Some(parse_text(&text).unwrap()) };
    let source = parse_source(&filter, &index.spec.fields, "posts", "sources.posts").unwrap();
    let vector = match (emb, text.as_ref().and_then(|t| t.about.clone())) {
        (Some(e), Some(about)) => Some(e.encode(&[about.as_str()]).unwrap().remove(0)),
        _ => None,
    };
    Ask::new(&index.spec.fields, text.as_ref(), &source, veil, limit, vector).unwrap()
}

fn ids(hits: &[Map<String, Value>]) -> Vec<String> {
    hits.iter().map(|h| h["id"].as_str().unwrap().to_string()).collect()
}

#[tokio::test]
async fn sections_follow_the_entry_year() {
    let dir = tempfile::tempdir().unwrap();
    let index = open(&dir).await;
    let stub = Stub::default();
    let w = index.upsert(&corpus(), Some(&stub)).await.unwrap();
    assert_eq!(w.entries, 5);
    assert_eq!(w.waiting, 0);
    let keys: Vec<String> = index.sections().iter().map(|s| s.key.clone()).collect();
    assert_eq!(keys, ["2026", "2025", "undated"]);
    assert_eq!(index.count(None).await.unwrap(), 5);

    // A redated entry moves to its new year's section, chunks and all.
    let mut moved = corpus()[2].clone();
    moved.insert("ts".into(), json!("2024-01-05T00:00:00Z"));
    index.upsert(&[moved], Some(&stub)).await.unwrap();
    let keys: Vec<String> = index.sections().iter().map(|s| s.key.clone()).collect();
    assert_eq!(keys, ["2026", "2025", "2024", "undated"]);
    assert_eq!(index.count(None).await.unwrap(), 5);
    for sec in index.sections() {
        let n = sec.chunks.count_rows(Some("id = 'p3'".into())).await.unwrap();
        assert_eq!(n, if sec.key == "2024" { 1 } else { 0 }, "section {}", sec.key);
    }

    // A reopened store finds the same sections.
    drop(index);
    let index = open(&dir).await;
    assert_eq!(index.sections().len(), 4);
    assert_eq!(index.count(None).await.unwrap(), 5);
}

#[tokio::test]
async fn a_query_spans_every_section() {
    let dir = tempfile::tempdir().unwrap();
    let index = open(&dir).await;
    let stub = Stub::default();
    index.upsert(&corpus(), Some(&stub)).await.unwrap();

    let a = ask(&index, json!({"$contains": "lantern"}), json!({}), &Veil::None, 10, None);
    let mut got = ids(&index.answer(&a).await.unwrap().exact);
    got.sort();
    assert_eq!(got, ["p1", "p4"]);

    let a = ask(&index, json!({"$about": "lantern wick chimney"}), json!({}), &Veil::None, 3, Some(&stub));
    let ans = index.answer(&a).await.unwrap();
    let got = ids(&ans.exact);
    assert_eq!(got.len(), 3);
    assert!(got[..2].contains(&"p1".to_string()) && got[..2].contains(&"p4".to_string()), "{got:?}");
    assert_eq!(ans.exact[0].keys().collect::<Vec<_>>(), ["id", "title", "ts"]);
    let p1 = ans.exact.iter().find(|h| h["id"] == "p1").unwrap();
    assert_eq!(p1["ts"], "2026-03-01T10:00:00Z");

    // Sorted by date, newest first, across sections; the undated entry goes last.
    let a = ask(&index, Value::Null, json!({"$sort": "-ts"}), &Veil::None, 10, None);
    assert_eq!(ids(&index.answer(&a).await.unwrap().exact), ["p2", "p1", "p3", "p4", "p5"]);
    let a = ask(&index, Value::Null, json!({"$sort": "+ts"}), &Veil::None, 10, None);
    assert_eq!(ids(&index.answer(&a).await.unwrap().exact), ["p4", "p3", "p1", "p2", "p5"]);
}

#[tokio::test]
async fn filters_and_veils_compile_to_prefilters() {
    let dir = tempfile::tempdir().unwrap();
    let index = open(&dir).await;
    index.upsert(&corpus(), None).await.unwrap();
    let all = |a: &Ask| {
        let a = a.clone();
        let index = &index;
        async move {
            let mut v = ids(&index.answer(&a).await.unwrap().exact);
            v.sort();
            v
        }
    };
    let a = ask(&index, Value::Null, json!({"project": "alpha"}), &Veil::None, 10, None);
    assert_eq!(all(&a).await, ["p1", "p2"]);
    // A nested scope covers the scopes below it.
    let a = ask(&index, Value::Null, json!({"scope": "main"}), &Veil::None, 10, None);
    assert_eq!(all(&a).await, ["p1", "p2", "p3", "p5"]);
    let a = ask(&index, Value::Null, json!({"ts": {"$gte": "2026-01-01"}}), &Veil::None, 10, None);
    assert_eq!(all(&a).await, ["p1", "p2"]);

    let marks = Veil::Marks { marks: vec![Mark { project: "beta".into(), scope: None }], tags: None };
    let a = ask(&index, Value::Null, json!({}), &marks, 10, None);
    assert_eq!(all(&a).await, ["p1", "p2", "p5"]);
    let marks = Veil::Marks { marks: vec![Mark { project: "alpha".into(), scope: Some("main/beds".into()) }], tags: None };
    let a = ask(&index, Value::Null, json!({}), &marks, 10, None);
    assert_eq!(all(&a).await, ["p1", "p3", "p4", "p5"]);
    let a = ask(&index, Value::Null, json!({}), &Veil::Unreadable, 10, None);
    assert!(all(&a).await.is_empty());

    let doc = index.document("p4", "true").await.unwrap().unwrap();
    assert_eq!(doc["author"], "agent-1");
    assert_eq!(doc["project"], json!(["beta"]));
    assert!(!doc.contains_key("_chunked"));
    let hidden = awm_index::sql::veil_sql(&index.spec.fields, &Veil::Marks { marks: vec![Mark { project: "beta".into(), scope: None }], tags: None });
    assert!(index.document("p4", &hidden).await.unwrap().is_none());
}

/// An embedder that is not there yet.
struct Down;

impl Embedder for Down {
    fn name(&self) -> &str {
        "down"
    }
    fn dim(&self) -> usize {
        384
    }
    fn count_tokens(&self, texts: &[&str]) -> Vec<usize> {
        awm_embed::chunk::approx_tokens(texts)
    }
    fn encode(&self, _: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        Err(EmbedError::Unavailable("no model".into()))
    }
}

#[tokio::test]
async fn without_the_model_entries_wait_and_keep_their_vectors() {
    let dir = tempfile::tempdir().unwrap();
    let index = open(&dir).await;
    let w = index.upsert(&corpus(), None).await.unwrap();
    assert_eq!((w.entries, w.waiting), (5, 5));
    let chunks = |index: &Index| {
        let secs = index.sections();
        async move {
            let mut n = 0;
            for s in secs {
                n += s.chunks.count_rows(None).await.unwrap();
            }
            n
        }
    };
    assert_eq!(chunks(&index).await, 0);
    // Keywords still answer.
    let a = ask(&index, json!({"$contains": "kettle"}), json!({}), &Veil::None, 10, None);
    assert_eq!(ids(&index.answer(&a).await.unwrap().exact), ["p3"]);

    // A failing embedder leaves them waiting and reports why.
    let w = index.embed_waiting(&Down).await.unwrap();
    assert!(w.error.is_some());
    assert_eq!(chunks(&index).await, 0);

    // The model loading embeds them.
    let stub = Stub::default();
    let w = index.embed_waiting(&stub).await.unwrap();
    assert_eq!((w.entries, w.waiting, w.error.clone()), (5, 0, None));
    assert_eq!(chunks(&index).await, 5);
    assert_eq!(index.count(Some("_chunked IS NULL")).await.unwrap(), 0);
    assert_eq!(index.embed_waiting(&stub).await.unwrap().entries, 0);

    // A retag with no model keeps the stored vectors and moves the new tags into the chunks.
    let before = stub.encoded.load(Ordering::SeqCst);
    let mut retag = corpus()[0].clone();
    retag.insert("tag".into(), json!(["journal", "lamp"]));
    let w = index.upsert(&[retag.clone()], None).await.unwrap();
    assert_eq!(w.waiting, 0);
    let sec = index.sections().into_iter().find(|s| s.key == "2026").unwrap();
    assert_eq!(sec.chunks.count_rows(Some("array_has(tag, 'lamp')".into())).await.unwrap(), 1);
    // With the model, an unchanged text embeds nothing.
    index.upsert(&[retag], Some(&stub)).await.unwrap();
    assert_eq!(stub.encoded.load(Ordering::SeqCst), before);

    // A changed text with no model waits again, with its stale chunks gone.
    let mut edit = corpus()[0].clone();
    edit.insert("text".into(), json!("Entirely new words about bicycles."));
    let w = index.upsert(&[edit], None).await.unwrap();
    assert_eq!(w.waiting, 1);
    assert_eq!(sec.chunks.count_rows(Some("id = 'p1'".into())).await.unwrap(), 0);
}

