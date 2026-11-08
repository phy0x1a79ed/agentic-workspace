//! `query` and `search` on the archive's index, ported from `test_query.py`. Both read message
//! text, not the raw JSON line.

mod common;

use common::*;
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
async fn regex_characters_in_a_query_match_literally() {
    let r = archive().await;
    assert_eq!(ids(&q(&r, "sink again (v2)", None, 20).await), ["s2:1"]);
    assert!(ids(&q(&r, "sink again v2", None, 20).await).is_empty(), "the words in order, but not as typed");
    assert!(ids(&q(&r, ".*", None, 20).await).is_empty());
}

#[tokio::test]
async fn a_hit_has_the_shape_search_reads() {
    let r = archive().await;
    let out = q(&r, "unrelated", None, 20).await;
    assert_eq!(out["hits"][0], json!({"id": "s1:2", "title": "-home-awm", "snippet": "unrelated", "project": "-home-awm"}));
}

#[tokio::test]
async fn project_and_limit_narrow_it() {
    let r = archive().await;
    assert_eq!(ids(&q(&r, "kitchen", Some("other"), 20).await), ["s2:1"]);
    assert!(ids(&q(&r, "kitchen", Some("nowhere"), 20).await).is_empty());
    let out = q(&r, "kitchen", None, 1).await;
    assert_eq!((ids(&out), out["truncated"].clone()), (vec!["s1:1".to_string()], json!(true)), "archive order");
}

#[tokio::test]
async fn search_takes_python_patterns_and_cuts_long_lines() {
    let r = rig();
    let long = format!("{}   \n", msg(&"x".repeat(500)));
    write_gz(&r.archive().join("p/s.jsonl.gz"), &format!("alpha beta\r\nbeta alpha\rgamma\n{long}"), 30.0);
    r.indexed().await;
    let find = |pat: &str| {
        let pat = pat.to_string();
        let app = &r.app;
        async move { app.search(&json!({"pattern": pat}), None).await.unwrap() }
    };
    let out = find(r"alpha(?= beta)").await;
    assert_eq!(out["hits"], json!([{"project": "p", "session": "s", "line": 1, "text": "alpha beta"}]));
    assert_eq!((out["pattern"].clone(), out["truncated"].clone()), (json!(r"alpha(?= beta)"), json!(false)));
    let out = find(r"(?P<w>a)lpha$").await;
    assert_eq!(out["hits"][0]["line"], 2, "universal newlines: \\r ends a line");
    let out = find("xxxx").await;
    let text = out["hits"][0]["text"].as_str().unwrap();
    assert_eq!((out["hits"][0]["line"].clone(), text.chars().count()), (json!(4), 400));
    let e = r.app.search(&json!({"pattern": "(unclosed"}), None).await.unwrap_err();
    assert_eq!(e.class, "error");
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

#[tokio::test]
async fn search_lists_hits_in_archive_order_and_cuts_at_the_limit() {
    let r = rig();
    write_gz(&r.archive().join("b/s2.jsonl.gz"), &msgs(&["hit", "miss", "hit"]), 30.0);
    write_gz(&r.archive().join("a/s9.jsonl.gz"), &msgs(&["hit"]), 30.0);
    write_gz(&r.archive().join("b/s1.jsonl.gz"), &msgs(&["hit"]), 30.0);
    r.indexed().await;
    let keys = |out: &Value| out["hits"].as_array().unwrap().iter().map(|h| format!("{}/{}:{}", h["project"].as_str().unwrap(), h["session"].as_str().unwrap(), h["line"])).collect::<Vec<_>>();
    let out = r.app.search(&json!({"pattern": "^hit$"}), None).await.unwrap();
    assert_eq!((keys(&out), out["truncated"].clone()), (vec!["a/s9:1".to_string(), "b/s1:1".into(), "b/s2:1".into(), "b/s2:3".into()], json!(false)));
    let out = r.app.search(&json!({"pattern": "hit", "limit": 2}), None).await.unwrap();
    assert_eq!((keys(&out), out["truncated"].clone()), (vec!["a/s9:1".to_string(), "b/s1:1".into()], json!(true)));
    let out = r.app.search(&json!({"pattern": "hit", "project": "b", "limit": 4}), None).await.unwrap();
    assert_eq!((keys(&out).len(), out["truncated"].clone()), (3, json!(false)));
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
