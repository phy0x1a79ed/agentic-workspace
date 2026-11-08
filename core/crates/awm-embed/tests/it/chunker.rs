//! The chunker against persistence.embeddings' output on synthetic documents.

use awm_embed::chunk::{approx_tokens, chunk, chunks_of, content_hash, header, text_hash, Document, CHUNK_TOKENS};
use serde_json::Value;

fn pairs(v: &Value) -> Vec<(String, String)> {
    v.as_array().unwrap().iter().map(|p| (p[0].as_str().unwrap().to_string(), p[1].as_str().unwrap().to_string())).collect()
}

#[test]
fn chunks_headers_and_hashes_match_the_python() {
    let path = format!("{}/tests/fixtures/chunker.json", env!("CARGO_MANIFEST_DIR"));
    let cases: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let mut failures = Vec::new();
    for (i, c) in cases.iter().enumerate() {
        let s = |k: &str| c[k].as_str().unwrap().to_string();
        let doc = Document { source_id: String::new(), body: s("body"), title: s("title"), context: s("context"), date: s("date") };
        let ntok = &approx_tokens;
        let got = chunk(&doc.body, CHUNK_TOKENS, ntok);
        if approx_tokens(&[&doc.body])[0] != c["approx_tokens"].as_u64().unwrap() as usize {
            failures.push(format!("{i}: approx_tokens"));
        }
        if got != pairs(&c["chunks"]) {
            failures.push(format!("{i}: chunks\n got {got:?}\nwant {:?}", pairs(&c["chunks"])));
        }
        if chunk(&doc.body, 16, ntok) != pairs(&c["chunks16"]) {
            failures.push(format!("{i}: chunks16"));
        }
        let sec = got.first().map(|(s, _)| s.as_str()).unwrap_or("");
        if header(&doc, sec) != s("header") {
            failures.push(format!("{i}: header {:?} vs {:?}", header(&doc, sec), s("header")));
        }
        if content_hash(&doc) != s("content_hash") {
            failures.push(format!("{i}: content_hash"));
        }
        if chunks_of(&doc, ntok) != pairs(&c["chunks_of"]) {
            failures.push(format!("{i}: chunks_of"));
        }
        if text_hash(&doc.title) != s("search_hash") {
            failures.push(format!("{i}: text_hash"));
        }
    }
    assert!(failures.is_empty(), "{} failures:\n{}", failures.len(), failures.iter().take(5).cloned().collect::<Vec<_>>().join("\n"));
}
