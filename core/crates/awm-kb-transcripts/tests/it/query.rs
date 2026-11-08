//! `query` and `search` on the archive's index, ported from `test_query.py`. Both read message
//! text, not the raw JSON line.

use crate::common::*;
use serde_json::{json, Value};

async fn archive() -> Rig {
    let r = rig();
    write_gz(&r.archive().join("-home-awm/s1.jsonl.gz"), &msgs(&["we chose the Deep Kitchen Sink", "unrelated"]), 30.0);
    write_gz(&r.archive().join("-home-other/s2.jsonl.gz"), &msgs(&["the deep kitchen sink again (v2)"]), 30.0);
    r.indexed().await;
    r
}

fn ids(out: &Value) -> Vec<String> {
    let mut v: Vec<String> = out["hits"].as_array().unwrap().iter().map(|h| h["id"].as_str().unwrap().to_string()).collect();
    v.sort();
    v
}

async fn q(r: &Rig, words: &str, project: Option<&str>, limit: i64) -> Value {
    let mut a = json!({"query": words, "limit": limit});
    if let Some(p) = project {
        a["project"] = json!(p);
    }
    r.app.query(&a, None).await.unwrap()
}

#[tokio::test]
async fn a_query_is_matched_as_words_ignoring_case() {
    let r = archive().await;
    let out = q(&r, "deep kitchen sink", None, 20).await;
    assert_eq!(ids(&out), ["s1:1", "s2:1"]);
    assert_eq!(out["truncated"], false);
}

#[tokio::test]
async fn a_hit_has_the_shape_search_reads() {
    let r = archive().await;
    let out = q(&r, "unrelated", None, 20).await;
    assert_eq!(out["hits"][0], json!({"id": "s1:2", "title": "-home-awm", "snippet": "unrelated", "project": "-home-awm"}));
}

/// The listed change: a pattern reads message text, so JSON keys and ids no longer match.
#[tokio::test]
async fn search_reads_message_text_not_the_json_line() {
    let r = rig();
    let lines = [
        json!({"type": "user", "uuid": "u-1", "message": {"role": "user", "content": "plain words"}}),
        json!({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "a text block"},
            {"type": "thinking", "thinking": "a thought", "signature": "sig-xyz"},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls /tmp", "description": "list it"}},
        ]}}),
        json!({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "tool output here"}]}]}}),
        json!({"type": "summary", "summary": "a summary line", "leafUid": "leaf-1"}),
        json!({"type": "system", "content": "a system note"}),
        json!({"type": "file-history-snapshot", "snapshot": {"text": "not message text"}}),
    ];
    write_gz(&r.archive().join("p/s.jsonl.gz"), &lines.iter().map(|l| format!("{l}\n")).collect::<String>(), 30.0);
    r.indexed().await;
    let lines_of = |out: Value| out["hits"].as_array().unwrap().iter().map(|h| h["line"].as_i64().unwrap()).collect::<Vec<_>>();
    for (pat, want) in [
        ("plain words", vec![1]),
        ("a text block\na thought\nls /tmp\nlist it", vec![2]),
        ("tool output", vec![3]),
        ("summary line", vec![4]),
        ("system note", vec![5]),
        ("u-1|toolu_1|sig-xyz|leaf-1|Bash|uuid|not message text", vec![]),
    ] {
        assert_eq!(lines_of(r.app.search(&json!({"pattern": pat}), None).await.unwrap()), want, "{pat}");
    }
    assert_eq!(r.app.ix().unwrap().record("p", "s").unwrap().rows, 5, "no row for a line with no text");
}

/// The source keeps the fields and hit the old manifest declared.
#[test]
fn the_source_declares_the_old_fields() {
    let spec = awm_kb_transcripts::index::spec();
    let fields: Vec<(String, String, String)> = spec.fields.iter().map(|f| (f.name.clone(), format!("{:?}", f.class), f.nested.clone())).collect();
    let want = [("title", "Content", ""), ("text", "Content", ""), ("ts", "Datetime", ""), ("project", "Categorical", ""), ("scope", "Categorical", "/")];
    assert_eq!(fields, want.map(|(a, b, c)| (a.to_string(), b.to_string(), c.to_string())));
    assert_eq!(spec.hit, ["title", "ts"]);
}
