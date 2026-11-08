//! The shape of a hit, and the cleaning of answers that come from elsewhere.
//!
//! A hit is its `id` and the fields its source marks as `hit`. Whatever a source or a peer
//! sends is cut down to those keys, so neither can inject fields nor quote text.

use serde_json::{Map, Value};
pub use awm_pycompat::repr::py_str;

/// The sections of one source's answer, in the order they are read.
pub const SECTIONS: [&str; 2] = ["exact", "related"];
const CHARS: usize = 2000;
const VALUES: usize = 50;

fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn value(raw: Option<&Value>) -> Value {
    match raw {
        Some(Value::Array(items)) => Value::Array(
            items
                .iter()
                .take(VALUES)
                .filter(|x| x.is_string() || x.is_number() || x.is_boolean())
                .map(|x| Value::String(head(&py_str(x), CHARS)))
                .collect(),
        ),
        Some(Value::Number(n)) => Value::Number(n.clone()),
        None | Some(Value::Null) => Value::String(String::new()),
        Some(other) => Value::String(head(&py_str(other), CHARS)),
    }
}

/// A hit from elsewhere as `{id, <fields>}`, or `None` when it names no document.
pub fn clean(raw: &Value, fields: &[String]) -> Option<Map<String, Value>> {
    let m = raw.as_object()?;
    let id = match m.get("id") {
        None | Some(Value::Null) => return None,
        Some(Value::String(s)) if s.is_empty() => return None,
        Some(v) => head(&py_str(v), CHARS),
    };
    let mut out = Map::new();
    out.insert("id".into(), Value::String(id));
    for f in fields {
        out.insert(f.clone(), value(m.get(f)));
    }
    Some(out)
}

/// A section sent as one list per source counts as their concatenation.
pub fn flat(raw: Option<&Value>) -> Vec<Value> {
    match raw {
        Some(Value::Object(m)) => m.values().filter_map(Value::as_array).flatten().cloned().collect(),
        Some(Value::Array(a)) => a.clone(),
        _ => Vec::new(),
    }
}

/// A list of hits from elsewhere, cleaned and capped at `limit`.
pub fn section(raw: Option<&Value>, limit: usize, fields: &[String]) -> Vec<Value> {
    flat(raw).iter().filter_map(|x| clean(x, fields)).take(limit).map(Value::Object).collect()
}
