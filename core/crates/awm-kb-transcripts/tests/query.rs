//! The live archive query behind search's `transcripts` source, and `search`, ported from
//! `test_query.py`.

mod common;

use awm_kb_transcripts::search;
use common::*;
use serde_json::{json, Value};

fn archive() -> Rig {
    let r = rig();
    write_gz(&r.archive().join("-home-awm/s1.jsonl.gz"), "{\"text\":\"we chose the Deep Kitchen Sink\"}\n{\"text\":\"unrelated\"}\n", 30.0);
    write_gz(&r.archive().join("-home-other/s2.jsonl.gz"), "{\"text\":\"the deep kitchen sink again (v2)\"}\n", 30.0);
    r
}

fn ids(out: &Value) -> Vec<String> {
    let mut v: Vec<String> = out["hits"].as_array().unwrap().iter().map(|h| h["id"].as_str().unwrap().to_string()).collect();
    v.sort();
    v
}

fn q(r: &Rig, words: &str, project: Option<&str>, limit: i64) -> Value {
    search::query(&r.app.tree, words, project, limit, None, None)
}

#[test]
fn a_query_is_matched_as_words_ignoring_case() {
    let r = archive();
    let out = q(&r, "deep kitchen sink", None, 20);
    assert_eq!(ids(&out), ["s1:1", "s2:1"]);
    assert_eq!(out["truncated"], false);
}

#[test]
fn regex_characters_in_a_query_match_literally() {
    let r = archive();
    assert_eq!(ids(&q(&r, "sink again (v2)", None, 20)), ["s2:1"]);
    assert!(ids(&q(&r, ".*", None, 20)).is_empty());
}

#[test]
fn a_hit_has_the_shape_search_reads() {
    let r = archive();
    let out = q(&r, "unrelated", None, 20);
    assert_eq!(out["hits"][0], json!({"id": "s1:2", "title": "-home-awm", "snippet": "{\"text\":\"unrelated\"}", "project": "-home-awm"}));
}

#[test]
fn project_and_limit_narrow_it() {
    let r = archive();
    assert_eq!(ids(&q(&r, "kitchen", Some("other"), 20)), ["s2:1"]);
    let out = q(&r, "kitchen", None, 1);
    assert_eq!((out["hits"].as_array().unwrap().len(), out["truncated"].clone()), (1, json!(true)));
}

#[test]
fn search_takes_python_patterns_and_cuts_long_lines() {
    let r = rig();
    let long = format!("{{\"text\":\"{}\"}}   \n", "x".repeat(500));
    write_gz(&r.archive().join("p/s.jsonl.gz"), &format!("alpha beta\r\nbeta alpha\rgamma\n{long}"), 30.0);
    let find = |pat: &str| {
        let rx = search::compile(pat).unwrap();
        search::find(&r.app.tree, pat, &rx, None, 200, None, None)
    };
    let out = find(r"alpha(?= beta)");
    assert_eq!(out["hits"], json!([{"project": "p", "session": "s", "line": 1, "text": "alpha beta"}]));
    assert_eq!(out["pattern"], r"alpha(?= beta)");
    let out = find(r"(?P<w>a)lpha$");
    assert_eq!(out["hits"][0]["line"], 2, "universal newlines: \\r ends a line");
    let out = find("xxxx");
    let text = out["hits"][0]["text"].as_str().unwrap();
    assert_eq!((out["hits"][0]["line"].clone(), text.chars().count()), (json!(4), 400));
    assert!(search::compile("(unclosed").is_err());
}

/// The source is live and opt-in, its tool is off, and the manifest declares no needs.
#[test]
fn the_manifest_keeps_the_live_opt_in_source() {
    let toml = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/awm-plugin.toml")).unwrap();
    for line in [
        "name = \"transcripts\"\nmode = \"live\"\nquery = \"transcripts_query\"\noptin = true\nhit = [\"title\", \"ts\"]",
        "title = \"content\"\ntext = \"content\"\nts = \"datetime\"\nproject = \"categorical\"\nscope = { class = \"categorical\", nested = \"/\" }",
        "tier = \"off\"",
    ] {
        assert!(toml.contains(line), "{line}");
    }
    assert!(!toml.contains("needs.tool"), "no needs");
}
