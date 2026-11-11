//! The import against a synthetic semdb build, then the source over the generation it wrote.
//! The stub embedder stands in for bge-large on both sides. All text is synthetic.

use awm_embed::{Embedder, Stub};
use awm_index::grammar::{parse_source, parse_text};
use awm_index::Veil;
use awm_kb_papers::import::{build, make_current, Opts};
use awm_kb_papers::{fields, Papers, NAME};
use awm_kb_source::{Ask, Source};
use lancedb::arrow::arrow_array::{ArrayRef, Int64Array, ListArray, RecordBatch, StringArray};
use lancedb::arrow::arrow_array::builder::{ListBuilder, StringBuilder};
use serde_json::{json, Value};
use std::path::Path;
use std::sync::Arc;

/// (id, doi, title, authors, year, domain, abstract, body chunks)
type Fake = (&'static str, &'static str, &'static str, &'static [&'static str], i64, &'static str, &'static str, &'static [&'static str]);

const PAPERS: [Fake; 4] = [
    ("doi:10.1/a", "10.1/a", "Copper bioleaching by acidophiles", &["Ada A", "Ben B"], 2020, "mining", "Acidophiles leach copper from ore.", &["Heap leaching runs for months.", "Ferrous iron oxidation drives it."]),
    ("doi:10.1/b", "10.1/b", "Chalcopyrite passivation layers", &["Ben B"], 2021, "copper", "A jarosite layer slows chalcopyrite.", &["Passivation forms at low potential."]),
    ("doi:10.1/c", "10.1/c", "Gut microbiome of honey bees", &["Cy C"], 2019, "vpri", "Bee guts hold few taxa.", &[]),
    ("doi:10.1/d", "10.1/d", "Rare earth recovery with lanmodulin", &["Dee D"], 2022, "mining", "", &["Lanmodulin binds lanthanides tightly."]),
];

fn utf8(v: Vec<&str>) -> ArrayRef {
    Arc::new(StringArray::from(v))
}

fn write_parquet(path: &Path, batch: RecordBatch) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    let mut w = parquet::arrow::ArrowWriter::try_new(std::fs::File::create(path).unwrap(), batch.schema(), None).unwrap();
    w.write(&batch).unwrap();
    w.close().unwrap();
}

fn write_zone(semdb: &Path, zone: &str, rows: &[(&str, &str)], stub: &Stub) {
    std::fs::create_dir_all(semdb.join("chunks")).unwrap();
    let db = rusqlite::Connection::open(semdb.join("chunks").join(format!("{zone}.sqlite"))).unwrap();
    db.execute_batch("CREATE TABLE chunk(id INTEGER PRIMARY KEY, key TEXT, zone TEXT, ord INTEGER, start INTEGER, end INTEGER, section TEXT, text TEXT); CREATE INDEX chunk_key ON chunk(key);").unwrap();
    for (i, (key, text)) in rows.iter().enumerate() {
        db.execute("INSERT INTO chunk VALUES (?1, ?2, ?3, 0, 0, 0, '', ?4)", rusqlite::params![i as i64, key, zone, text]).unwrap();
    }
    let texts: Vec<&str> = rows.iter().map(|(_, t)| *t).collect();
    let vectors = stub.encode(&texts).unwrap();
    let head = format!("{{'descr': '<f4', 'fortran_order': False, 'shape': ({}, {}), }}", rows.len(), stub.dim);
    let pad = 64 - (10 + head.len() + 1) % 64;
    let mut bytes = b"\x93NUMPY\x01\x00".to_vec();
    bytes.extend(((head.len() + pad + 1) as u16).to_le_bytes());
    bytes.extend(head.as_bytes());
    bytes.extend(std::iter::repeat_n(b' ', pad));
    bytes.push(b'\n');
    for v in vectors {
        bytes.extend(v.iter().flat_map(|x| x.to_le_bytes()));
    }
    std::fs::create_dir_all(semdb.join("vectors").join(zone)).unwrap();
    std::fs::write(semdb.join("vectors").join(zone).join("index.npy"), bytes).unwrap();
}

fn fake_semdb(semdb: &Path, stub: &Stub) {
    let mut authors = ListBuilder::new(StringBuilder::new());
    for p in &PAPERS {
        for a in p.3 {
            authors.values().append_value(a);
        }
        authors.append(true);
    }
    let authors: ListArray = authors.finish();
    let paper = RecordBatch::try_from_iter([
        ("paper_id", utf8(PAPERS.iter().map(|p| p.0).collect())),
        ("doi", utf8(PAPERS.iter().map(|p| p.1).collect())),
        ("title", utf8(PAPERS.iter().map(|p| p.2).collect())),
        ("authors", Arc::new(authors) as ArrayRef),
        ("year", Arc::new(Int64Array::from(PAPERS.iter().map(|p| p.4).collect::<Vec<_>>())) as ArrayRef),
        ("journal", utf8(PAPERS.iter().map(|_| "J Synth").collect())),
    ])
    .unwrap();
    write_parquet(&semdb.join("registry/paper.parquet"), paper);
    let domain = RecordBatch::try_from_iter([("paper_id", utf8(PAPERS.iter().map(|p| p.0).collect())), ("domain", utf8(PAPERS.iter().map(|p| p.5).collect()))]).unwrap();
    write_parquet(&semdb.join("registry/paper_domain.parquet"), domain);
    write_zone(semdb, "titles", &PAPERS.iter().map(|p| (p.0, p.2)).collect::<Vec<_>>(), stub);
    write_zone(semdb, "abstracts", &PAPERS.iter().filter(|p| !p.6.is_empty()).map(|p| (p.0, p.6)).collect::<Vec<_>>(), stub);
    write_zone(semdb, "chunks", &PAPERS.iter().flat_map(|p| p.7.iter().map(|c| (p.0, *c))).collect::<Vec<_>>(), stub);
}

fn fake_graph(path: &Path) {
    let db = rusqlite::Connection::open(path).unwrap();
    db.execute_batch(
        "CREATE TABLE node(nid INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL);
         CREATE TABLE edge(src INTEGER NOT NULL, dst INTEGER NOT NULL, kind TEXT NOT NULL, weight REAL NOT NULL);
         INSERT INTO node VALUES (0, 'paper:doi:10.1/a', 'paper'), (1, 'paper:doi:10.1/b', 'paper'), (2, 'author:x', 'author');
         INSERT INTO edge VALUES (1, 0, 'cites', 1.0), (2, 0, 'author', 1.0);",
    )
    .unwrap();
}

const ZOTERO: &str = r#"{"library": "users/0", "key": "K1", "version": 5, "doi": "10.1/B", "title": "Chalcopyrite passivation layers", "year": 2021, "creators": ["Ben B"], "abstract": "", "publication": "J Synth"}
{"library": "users/0", "key": "K2", "version": 6, "doi": "", "title": "Field notes on tailings ponds", "year": 2024, "creators": ["Eve E"], "abstract": "Tailings ponds host sulfate reducers.", "publication": "Notebook"}
"#;

async fn papers(tmp: &Path, stub: Arc<Stub>) -> Arc<Papers> {
    let semdb = tmp.join("semdb");
    fake_semdb(&semdb, &stub);
    fake_graph(&tmp.join("graph.db"));
    std::fs::write(tmp.join("zotero.jsonl"), ZOTERO).unwrap();
    let root = tmp.join("papers");
    let opts = Opts { semdb, graph: Some(tmp.join("graph.db")), zotero: Some(tmp.join("zotero.jsonl")), model: tmp.join("unused"), root: root.clone() };
    let out = build(&opts, &root.join("gen-1"), stub.as_ref()).await.unwrap();
    assert_eq!((out["papers"].clone(), out["chunks"].clone(), out["zotero"]["held"].clone()), (json!(5), json!(11), json!(1)));
    make_current(&root, "gen-1").unwrap();
    Papers::at(&root, Some(stub as Arc<dyn Embedder>)).await.unwrap().unwrap()
}

async fn ask(src: &Papers, text: Value, filter: Value) -> Vec<String> {
    let ask = Ask {
        text: Some(parse_text(&text).unwrap()),
        source: parse_source(&filter, &fields(), NAME, NAME).unwrap(),
        max: 10,
        vector: None,
        veil: Veil::None,
        session: None,
    };
    let out = src.query(ask).await.unwrap();
    out["exact"].as_array().unwrap().iter().map(|h| h["id"].as_str().unwrap().to_string()).collect()
}

#[tokio::test]
async fn import_then_query_by_meaning_keyword_and_collection() {
    let tmp = tempfile::tempdir().unwrap();
    let src = papers(tmp.path(), Arc::new(Stub::new("stub-bge", 1024))).await;

    let hits = ask(&src, json!({"$about": "chalcopyrite passivation"}), json!({})).await;
    assert_eq!(hits.first().map(String::as_str), Some("doi:10.1/b"));
    let hits = ask(&src, json!({"$contains": "lanmodulin"}), json!({})).await;
    assert_eq!(hits, ["doi:10.1/d"], "the body is searchable by keyword");

    let library = ask(&src, json!({"$about": "copper"}), json!({"collection": "tony"})).await;
    let mut library = library;
    library.sort();
    assert_eq!(library, ["doi:10.1/b", "zotero:users/0/K2"], "a matched item joins tony, an unmatched one is held");
    let either = ask(&src, json!({"$about": "copper bees"}), json!({"collection": {"$in": ["vpri", "copper"]}})).await;
    assert!(either.iter().all(|i| i == "doi:10.1/b" || i == "doi:10.1/c") && either.len() == 2, "{either:?}");
    let both = ask(&src, json!({"$about": "copper"}), json!({"collection": {"$all": ["copper", "tony"]}})).await;
    assert_eq!(both, ["doi:10.1/b"]);

    assert_eq!(ask(&src, json!({"$about": "leaching"}), json!({"cited_by": "doi:10.1/b"})).await, ["doi:10.1/a"]);
    assert_eq!(ask(&src, json!({"$about": "leaching"}), json!({"author": "Ben B"})).await.len(), 2);
    let mut recent = ask(&src, json!({"$about": "leaching"}), json!({"ts": {"$gte": "2022-01-01"}})).await;
    recent.sort();
    assert_eq!(recent, ["doi:10.1/d", "zotero:users/0/K2"]);

    let whole = src.get("doi:10.1/a", &Veil::None, None).await.unwrap().unwrap();
    assert_eq!(whole["body"], json!("Heap leaching runs for months.\n\nFerrous iron oxidation drives it."));
    assert_eq!(whole["cited_by"], json!(["doi:10.1/b"]));
}

#[tokio::test]
async fn a_new_current_generation_is_served_from_the_next_query() {
    let tmp = tempfile::tempdir().unwrap();
    let stub = Arc::new(Stub::new("stub-bge", 1024));
    let src = papers(tmp.path(), stub.clone()).await;
    let root = tmp.path().join("papers");
    let opts = Opts { semdb: tmp.path().join("semdb"), graph: None, zotero: None, model: tmp.path().join("unused"), root: root.clone() };
    build(&opts, &root.join("gen-2"), stub.as_ref()).await.unwrap();
    make_current(&root, "gen-2").unwrap();
    assert_eq!(src.status().await["generation"], json!("gen-2"));
    assert!(ask(&src, json!({"$about": "tailings"}), json!({"collection": "tony"})).await.is_empty(), "gen-2 has no Zotero items");
}
