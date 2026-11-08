//! The grammar and the SQL it compiles, against search's Python answers (fixtures/capture.py).

use awm_index::grammar::{parse_filter, parse_max, parse_sort, parse_text, Node};
use awm_index::sql::{filter_sql, veil_sql};
use awm_index::{Class, Field, Fields, Mark, Veil};
use serde_json::{json, Value};

fn fields() -> Fields {
    Fields(vec![
        Field::new("title", Class::Content),
        Field::new("text", Class::Content),
        Field::new("ts", Class::Datetime),
        Field::new("score", Class::Numeric),
        Field::new("project", Class::Categorical),
        Field::nested("scope", "/"),
        Field::new("tag", Class::Categorical),
    ])
}

fn node(n: &Node) -> Value {
    match n {
        Node::Lit(s) => json!(s),
        Node::Op(op, items) => json!({ op: items.iter().map(node).collect::<Vec<_>>() }),
    }
}

fn veil(v: &Value) -> Veil {
    match v {
        Value::Null => Veil::Unreadable,
        Value::Array(a) => Veil::Marks {
            marks: a
                .iter()
                .map(|m| Mark { project: m["project"].as_str().unwrap().into(), scope: m["scope"].as_str().map(String::from) })
                .collect(),
            tags: None,
        },
        _ => unreachable!(),
    }
}

fn ours(case: &str, input: &Value) -> Value {
    let fields = fields();
    let result = match case {
        "text" => parse_text(input).map(|t| {
            json!({"about": t.about, "under_or": t.under_or, "contains": t.contains.as_ref().map(node)})
        }),
        "filter" => parse_filter(input, &fields, "posts", "query.posts").map(|f| {
            let sql = match filter_sql(&fields, &f) {
                Ok(s) => json!({"ok": s}),
                Err(e) => json!({"unserved": e.0}),
            };
            json!({"filter": f, "sql": sql})
        }),
        "sort" => parse_sort(input, &fields, "posts", "query.posts.$sort").map(|(f, d)| json!([f, d])),
        "max" => parse_max(input).map(|n| json!(n)),
        "veil" => return json!({"ok": veil_sql(&fields, &veil(input))}),
        _ => unreachable!(),
    };
    match result {
        Ok(v) => json!({"ok": v}),
        Err(e) => json!({"err": e.0}),
    }
}

#[test]
fn every_case_matches_python() {
    let mut failed = Vec::new();
    let mut n = 0;
    for line in include_str!("../fixtures/grammar.jsonl").lines() {
        let mut want: Value = serde_json::from_str(line).unwrap();
        let obj = want.as_object_mut().unwrap();
        let case = obj.remove("case").unwrap();
        let input = obj.remove("input").unwrap();
        let got = ours(case.as_str().unwrap(), &input);
        n += 1;
        if got != want {
            failed.push(format!("{case} {input}\n  python: {want}\n  rust:   {got}"));
        }
    }
    assert!(n > 70);
    assert!(failed.is_empty(), "{} of {n} differ:\n{}", failed.len(), failed.join("\n"));
}
