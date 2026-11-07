//! The per-source half of search's `query` grammar: the text tree, a source's filter and `$sort`.
//!
//! Error texts match the Python's byte for byte (`<json path>: <problem>; allowed: <values>`),
//! because callers and skills match on them.

use crate::fields::{Class, Field, Fields};
use awm_pycompat::chars::is_word;
use awm_pycompat::pystr::strip;
use awm_pycompat::repr::repr;
use awm_pycompat::time::{fromisoformat, zulu, DateTime};
use serde_json::{Map, Value};

pub const DEFAULT_MAX: usize = 10;
pub const MAX_MAX: usize = 100;
pub const LOGIC: [&str; 3] = ["$and", "$or", "$nor"];
pub const SCALAR_OPS: [&str; 6] = ["$eq", "$ne", "$gt", "$gte", "$lt", "$lte"];
pub const LIST_OPS: [&str; 3] = ["$in", "$nin", "$all"];

/// A malformed request.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct QueryError(pub String);

/// `_fail(path, problem, allowed)`.
pub fn fail(path: &str, problem: &str, allowed: &[&str]) -> QueryError {
    let mut msg = format!("{path}: {problem}");
    if !allowed.is_empty() {
        msg.push_str("; allowed: ");
        msg.push_str(&allowed.join(", "));
    }
    QueryError(msg)
}

fn fail1(path: &str, problem: &str, allowed: &str) -> QueryError {
    fail(path, problem, if allowed.is_empty() { &[] } else { std::slice::from_ref(&allowed) })
}

/// A `$contains` tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Node {
    Lit(String),
    /// `and`, `or` or `nor`.
    Op(String, Vec<Node>),
}

/// The parsed `text` of a query.
#[derive(Debug, Clone, PartialEq)]
pub struct Text {
    pub about: Option<String>,
    /// An `$about` beside a `$contains` under `$or`: the answer gains a `related` section.
    pub under_or: bool,
    pub contains: Option<Node>,
    /// The text as the caller wrote it, forwarded unchanged to a peer or a source.
    pub raw: Value,
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

fn has_alnum(s: &str) -> bool {
    s.chars().any(|c| c != '_' && is_word(c))
}

fn single(v: &Value) -> Option<(&String, &Value)> {
    match v {
        Value::Object(m) if m.len() == 1 => m.iter().next(),
        _ => None,
    }
}

fn contains_tree(node: &Value, path: &str) -> Result<Node, QueryError> {
    let allowed = ["$contains", "$and", "$or", "$nor"];
    let Some((key, value)) = single(node) else {
        return Err(fail(path, "a text term is an object with exactly one key", &allowed));
    };
    if key == "$contains" {
        return match value {
            Value::String(s) if has_alnum(s) => Ok(Node::Lit(s.clone())),
            _ => Err(fail(&format!("{path}.$contains"), "needs a string with a letter or digit", &[])),
        };
    }
    if LOGIC.contains(&key.as_str()) {
        let items = match value {
            Value::Array(a) if !a.is_empty() => a,
            _ => return Err(fail(&format!("{path}.{key}"), "needs a non-empty list", &[])),
        };
        let subs = items
            .iter()
            .enumerate()
            .map(|(i, sub)| contains_tree(sub, &format!("{path}.{key}[{i}]")))
            .collect::<Result<_, _>>()?;
        return Ok(Node::Op(key[1..].to_string(), subs));
    }
    if key == "$about" {
        return Err(fail(path, "$about sits only directly under the top $and or $or", &[]));
    }
    Err(fail(&format!("{path}.{key}"), "not allowed in text", &allowed))
}

fn about_string(value: &Value, path: &str) -> Result<String, QueryError> {
    match value {
        Value::String(s) if !s.is_empty() => Ok(s.clone()),
        _ => Err(fail(path, "$about needs a non-empty string", &[])),
    }
}

/// `_text`: the `text` of a query.
pub fn parse_text(value: &Value) -> Result<Text, QueryError> {
    let path = "query.text";
    if let Value::String(s) = value {
        if s.is_empty() {
            return Err(fail(path, "text is empty", &[]));
        }
        return Ok(Text { about: Some(s.clone()), under_or: true, contains: Some(Node::Lit(s.clone())), raw: value.clone() });
    }
    let Some((key, body)) = single(value) else {
        return Err(fail(path, "text is a string or an object with exactly one key", &["$contains", "$about", "$and", "$or", "$nor"]));
    };
    if key == "$about" {
        return Ok(Text { about: Some(about_string(body, &format!("{path}.$about"))?), under_or: false, contains: None, raw: value.clone() });
    }
    if key == "$and" || key == "$or" {
        let items = match body {
            Value::Array(a) if !a.is_empty() => a,
            _ => return Err(fail(&format!("{path}.{key}"), "needs a non-empty list", &[])),
        };
        let mut about = None;
        let mut rest = Vec::new();
        for (i, item) in items.iter().enumerate() {
            let at = format!("{path}.{key}[{i}]");
            match item {
                Value::Object(m) if m.len() == 1 && m.contains_key("$about") => {
                    if about.is_some() {
                        return Err(fail(&format!("{path}.{key}"), "more than one $about", &[]));
                    }
                    about = Some(about_string(&m["$about"], &format!("{at}.$about"))?);
                }
                _ => rest.push(contains_tree(item, &at)?),
            }
        }
        let contains = match rest.len() {
            0 => None,
            1 => rest.pop(),
            _ => Some(Node::Op(key[1..].to_string(), rest)),
        };
        let under_or = key == "$or" && about.is_some() && contains.is_some();
        return Ok(Text { about, under_or, contains, raw: value.clone() });
    }
    Ok(Text { about: None, under_or: false, contains: Some(contains_tree(value, path)?), raw: value.clone() })
}

/// `store.normalize_ts`: `2026-03-01T12:00:00Z` for ISO 8601 text, or `None`.
pub fn normalize_ts(value: &str) -> Option<String> {
    let s = strip(value);
    if s.is_empty() {
        return None;
    }
    let dt = fromisoformat(s)?;
    Some(zulu(&DateTime::from_utc_us(dt.utc_us())))
}

fn scalar(value: &Value, f: &Field, path: &str) -> Result<Value, QueryError> {
    match value {
        Value::String(_) | Value::Number(_) => {}
        _ => return Err(fail(path, &format!("{} is not a scalar", type_name(value)), &["string", "number"])),
    }
    match f.class {
        Class::Datetime => match value.as_str().and_then(normalize_ts) {
            Some(stamp) => Ok(Value::String(stamp)),
            None => Err(fail(
                path,
                &format!("{} is not an ISO 8601 date or time", repr(value)),
                &["2026-03-01", "2026-03-01T12:00:00Z"],
            )),
        },
        Class::Numeric if !value.is_number() => Err(fail1(path, &format!("{} is not a number", repr(value)), "a number")),
        Class::Numeric => Ok(value.clone()),
        _ => Ok(Value::String(match value {
            Value::String(s) => s.clone(),
            Value::Number(n) => match n.as_i64() {
                Some(i) => i.to_string(),
                None => awm_pycompat::repr::float_repr(n.as_f64().unwrap_or(f64::NAN)),
            },
            _ => unreachable!("checked above"),
        })),
    }
}

fn field_value(value: &Value, f: &Field, path: &str) -> Result<Value, QueryError> {
    let ops: Vec<&str> = SCALAR_OPS.iter().chain(LIST_OPS.iter()).copied().collect();
    match value {
        Value::Array(_) => Err(fail1(path, "a bare list is not a value", "a scalar, or an object with $in, $nin or $all")),
        Value::Object(m) if m.is_empty() => Err(fail(path, "empty operator object", &ops)),
        Value::Object(m) => {
            let mut out = Map::new();
            for (op, operand) in m {
                let at = format!("{path}.{op}");
                if SCALAR_OPS.contains(&op.as_str()) {
                    out.insert(op.clone(), scalar(operand, f, &at)?);
                } else if LIST_OPS.contains(&op.as_str()) {
                    let items = match operand {
                        Value::Array(a) if !a.is_empty() => a,
                        _ => return Err(fail(&at, "needs a non-empty list", &[])),
                    };
                    let vals = items.iter().enumerate().map(|(i, x)| scalar(x, f, &format!("{at}[{i}]"))).collect::<Result<_, _>>()?;
                    out.insert(op.clone(), Value::Array(vals));
                } else {
                    return Err(fail(&at, "unknown operator", &ops));
                }
            }
            Ok(Value::Object(out))
        }
        _ => scalar(value, f, path),
    }
}

/// `_filter`: a source filter validated against its fields, with values normalised.
pub fn parse_filter(f: &Value, fields: &Fields, kind: &str, path: &str) -> Result<Map<String, Value>, QueryError> {
    let Value::Object(m) = f else {
        return Err(fail(path, "a source filter is an object", &[]));
    };
    let filterable: Vec<&str> = fields.iter().filter(|x| x.class != Class::Content).map(|x| x.name.as_str()).collect();
    let mut out = Map::new();
    for (key, value) in m {
        let at = format!("{path}.{key}");
        if LOGIC.contains(&key.as_str()) {
            let items = match value {
                Value::Array(a) if !a.is_empty() => a,
                _ => return Err(fail(&at, "needs a non-empty list", &[])),
            };
            let subs = items
                .iter()
                .enumerate()
                .map(|(i, sub)| parse_filter(sub, fields, kind, &format!("{at}[{i}]")).map(Value::Object))
                .collect::<Result<_, _>>()?;
            out.insert(key.clone(), Value::Array(subs));
        } else if key == "$about" || key == "$contains" {
            return Err(fail(&at, &format!("{key} belongs in text, not in a source filter"), &[]));
        } else if key == "$sort" {
            return Err(fail(&at, "$sort sits only at the top of a source filter", &[]));
        } else if key.starts_with('$') {
            let mut allowed: Vec<&str> = LOGIC.to_vec();
            allowed.push("$sort");
            allowed.extend(&filterable);
            return Err(fail(&at, "operator not allowed at a field position", &allowed));
        } else {
            match fields.get(key) {
                None => return Err(fail(&at, &format!("{kind} has no field {}", awm_pycompat::repr::str_repr(key)), &filterable)),
                Some(field) if field.class == Class::Content => {
                    return Err(fail(&at, &format!("{key} is content: search it with text"), &filterable))
                }
                Some(field) => {
                    out.insert(key.clone(), field_value(value, field, &at)?);
                }
            }
        }
    }
    Ok(out)
}

/// `_sort`: `(field, descending)` from `+name`, `-name` or `name`.
pub fn parse_sort(value: &Value, fields: &Fields, kind: &str, path: &str) -> Result<(String, bool), QueryError> {
    let parsed = value.as_str().and_then(|s| {
        let (desc, name) = match s.as_bytes().first() {
            Some(b'-') => (true, &s[1..]),
            Some(b'+') => (false, &s[1..]),
            _ => (false, s),
        };
        let mut cs = name.chars();
        let ok = cs.next().is_some_and(|c| c.is_ascii_lowercase())
            && cs.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
            && fields.get(name).is_some();
        ok.then(|| (name.to_string(), desc))
    });
    parsed.ok_or_else(|| {
        let allowed: Vec<String> = fields.iter().map(|f| format!("-{}", f.name)).collect();
        let allowed: Vec<&str> = allowed.iter().map(String::as_str).collect();
        fail(path, &format!("{} is not one field of {kind} with + or -", repr(value)), &allowed)
    })
}

/// One source's part of a query: its validated filter and `$sort`.
#[derive(Debug, Clone, PartialEq)]
pub struct SourceFilter {
    pub filter: Map<String, Value>,
    pub sort: Option<(String, bool)>,
}

/// `_source`'s filter half: `$sort` split off, the rest validated.
pub fn parse_source(value: &Value, fields: &Fields, kind: &str, path: &str) -> Result<SourceFilter, QueryError> {
    let mut value = value.clone();
    let mut sort = None;
    if let Value::Object(m) = &mut value {
        if let Some(s) = m.shift_remove("$sort") {
            sort = Some(parse_sort(&s, fields, kind, &format!("{path}.$sort"))?);
        }
    }
    Ok(SourceFilter { filter: parse_filter(&value, fields, kind, path)?, sort })
}

/// `_max`: 1 to [`MAX_MAX`], from an integer or a string of digits.
pub fn parse_max(value: &Value) -> Result<usize, QueryError> {
    let n = match value {
        Value::Number(n) => n.as_u64(),
        Value::String(s) => {
            let t = s.trim_matches(|c: char| awm_pycompat::chars::is_space(c));
            (!t.is_empty() && t.bytes().all(|b| b.is_ascii_digit())).then(|| t.parse::<u64>().unwrap_or(u64::MAX))
        }
        _ => None,
    };
    match n {
        Some(n) if (1..=MAX_MAX as u64).contains(&n) => Ok(n as usize),
        _ => Err(fail1("max", &format!("{} is not an integer", repr(value)), &format!("1-{MAX_MAX}"))),
    }
}
