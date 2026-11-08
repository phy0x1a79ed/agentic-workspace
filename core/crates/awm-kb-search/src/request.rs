//! A search request: the arguments of `search query` read once, validated, with the caller's
//! reach applied. The per-source grammar lives in awm-index; this is the request around it.

use awm_index::fields::Fields;
use awm_index::grammar::{fail, parse_max, parse_source, parse_text, QueryError, SourceFilter, Text, DEFAULT_MAX};
use awm_index::Veil;
use awm_pycompat::pystr::{lstrip, strip};
use indexmap::IndexMap;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashSet};

/// The kinds a query may name, each with its fields and the fields its hits show.
#[derive(Debug, Clone, Default)]
pub struct Kinds(pub BTreeMap<String, (Fields, Vec<String>)>);

impl Kinds {
    /// The fields a hit of `kind` shows beside its id: `title` and `ts` for an undeclared kind.
    pub fn hit_fields(&self, kind: &str) -> Vec<String> {
        match self.0.get(kind) {
            Some((_, hit)) if !hit.is_empty() => hit.clone(),
            _ => vec!["title".into(), "ts".into()],
        }
    }
}

/// One source a query names.
#[derive(Debug, Clone, PartialEq)]
pub struct SourceReq {
    /// The key as the caller wrote it, such as `journals@altair`.
    pub name: String,
    pub kind: String,
    /// `""` for this node.
    pub node: String,
    pub filter: SourceFilter,
}

impl SourceReq {
    /// The filter as a caller writes it, `$sort` included.
    pub fn asked(&self) -> Value {
        let mut m = self.filter.filter.clone();
        if let Some((field, desc)) = &self.filter.sort {
            m.insert("$sort".into(), json!(format!("{}{field}", if *desc { "-" } else { "+" })));
        }
        Value::Object(m)
    }
}

#[derive(Debug, Clone)]
pub struct Request {
    pub text: Option<Text>,
    pub sources: Vec<SourceReq>,
    /// The sources named on other nodes, by node label.
    pub remote: IndexMap<String, Vec<SourceReq>>,
    pub max: usize,
    pub relayed: bool,
    pub veil: Veil,
    /// The sources a relayed call named on other nodes, which it does not ask.
    pub elsewhere: Vec<String>,
}

impl Request {
    /// The arguments a peer is asked with: the same question over its own sources, by bare kind.
    pub fn forwarded(&self, node: &str) -> Value {
        let mut body = Map::new();
        if let Some(t) = &self.text {
            body.insert("text".into(), t.raw.clone());
        }
        for s in self.remote.get(node).into_iter().flatten() {
            body.insert(s.kind.clone(), s.asked());
        }
        json!({"query": body, "max": self.max})
    }
}

/// A node name as the edge list writes it.
pub fn label(name: &str) -> String {
    awm_plugin_sdk::config::label(name)
}

/// What the parser needs to know of this node and the caller.
pub struct Context<'a> {
    pub kinds: &'a Kinds,
    /// This node's name.
    pub here: &'a str,
    /// The kinds a query that names no source asks.
    pub defaults: &'a [String],
    pub relayed: bool,
    pub veil: Veil,
}

fn is_here(node: &str, here: &str) -> bool {
    node.is_empty() || label(node) == label(here)
}

/// Python's `^[A-Za-z0-9_.-]+$`, whose `$` also matches before a final newline.
fn node_ok(node: &str) -> bool {
    let body = node.strip_suffix('\n').unwrap_or(node);
    !body.is_empty() && body.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-')
}

fn source(key: &str, value: &Value, ctx: &Context) -> Result<SourceReq, QueryError> {
    let path = format!("query.{key}");
    let (kind, node) = match key.split_once('@') {
        Some((k, n)) => (k, Some(n)),
        None => (key, None),
    };
    let Some((fields, _)) = ctx.kinds.0.get(kind) else {
        let known: Vec<&str> = ctx.kinds.0.keys().map(String::as_str).collect();
        return Err(fail(&path, "not a source", &known));
    };
    let node = node.unwrap_or("");
    if key.contains('@') && !node_ok(node) {
        return Err(fail(&path, "malformed node after @", &["kind@node, such as journals@altair"]));
    }
    let filter = parse_source(value, fields, kind, &path)?;
    Ok(SourceReq { name: key.into(), kind: kind.into(), node: if is_here(node, ctx.here) { String::new() } else { node.into() }, filter })
}

/// A JSON value with every pair of each object kept, so a duplicate key can be named.
enum Tree {
    Leaf(Value),
    Arr(Vec<Tree>),
    Obj(Vec<(String, Tree)>),
}

impl<'de> serde::Deserialize<'de> for Tree {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> serde::de::Visitor<'de> for V {
            type Value = Tree;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("JSON")
            }
            fn visit_bool<E>(self, v: bool) -> Result<Tree, E> {
                Ok(Tree::Leaf(Value::Bool(v)))
            }
            fn visit_i64<E>(self, v: i64) -> Result<Tree, E> {
                Ok(Tree::Leaf(json!(v)))
            }
            fn visit_u64<E>(self, v: u64) -> Result<Tree, E> {
                Ok(Tree::Leaf(json!(v)))
            }
            fn visit_f64<E>(self, v: f64) -> Result<Tree, E> {
                Ok(Tree::Leaf(json!(v)))
            }
            fn visit_str<E>(self, v: &str) -> Result<Tree, E> {
                Ok(Tree::Leaf(json!(v)))
            }
            fn visit_string<E>(self, v: String) -> Result<Tree, E> {
                Ok(Tree::Leaf(Value::String(v)))
            }
            fn visit_unit<E>(self) -> Result<Tree, E> {
                Ok(Tree::Leaf(Value::Null))
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut seq: A) -> Result<Tree, A::Error> {
                let mut out = Vec::new();
                while let Some(x) = seq.next_element()? {
                    out.push(x);
                }
                Ok(Tree::Arr(out))
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(self, mut map: A) -> Result<Tree, A::Error> {
                let mut out = Vec::new();
                while let Some((k, v)) = map.next_entry::<String, Tree>()? {
                    out.push((k, v));
                }
                Ok(Tree::Obj(out))
            }
        }
        d.deserialize_any(V)
    }
}

impl Tree {
    /// `_no_dups`: the first duplicate key, by path, each object's own keys before its values.
    fn dup(&self, path: &str) -> Option<String> {
        match self {
            Tree::Leaf(_) => None,
            Tree::Arr(items) => items.iter().enumerate().find_map(|(i, x)| x.dup(&format!("{path}[{i}]"))),
            Tree::Obj(pairs) => {
                let mut seen = HashSet::new();
                let mut dups: Vec<&str> = pairs.iter().filter(|(k, _)| !seen.insert(k.as_str())).map(|(k, _)| k.as_str()).collect();
                dups.sort();
                if let Some(k) = dups.first() {
                    return Some(format!("{path}.{k}"));
                }
                // Python's dict keeps a key's first place and its last value.
                let mut last: IndexMap<&str, &Tree> = IndexMap::new();
                for (k, v) in pairs {
                    last.insert(k, v);
                }
                last.iter().find_map(|(k, v)| v.dup(&format!("{path}.{k}")))
            }
        }
    }

    fn value(self) -> Value {
        match self {
            Tree::Leaf(v) => v,
            Tree::Arr(items) => Value::Array(items.into_iter().map(Tree::value).collect()),
            Tree::Obj(pairs) => {
                let mut m = Map::new();
                for (k, v) in pairs {
                    m.insert(k, v.value());
                }
                Value::Object(m)
            }
        }
    }
}

fn parse_json(text: &str) -> Result<Value, QueryError> {
    let tree: Tree = serde_json::from_str(text).map_err(|e| fail("query", &format!("not valid JSON ({e})"), &[]))?;
    if let Some(path) = tree.dup("query") {
        return Err(fail(&path, "duplicate key", &[]));
    }
    Ok(tree.value())
}

fn query(raw: &Value, ctx: &Context) -> Result<(Option<Text>, Vec<SourceReq>), QueryError> {
    let parsed;
    let raw = match raw {
        Value::String(s) if lstrip(s).starts_with('{') => {
            parsed = parse_json(s)?;
            &parsed
        }
        other => other,
    };
    let defaults = || -> Result<Vec<SourceReq>, QueryError> {
        let names: Vec<&str> = if ctx.defaults.is_empty() { vec!["journals"] } else { ctx.defaults.iter().map(String::as_str).collect() };
        names.into_iter().map(|k| source(k, &json!({}), ctx)).collect()
    };
    if let Value::String(s) = raw {
        if strip(s).is_empty() {
            return Err(fail("query", "the query is empty", &[]));
        }
        return Ok((Some(parse_text(raw)?), defaults()?));
    }
    let m = match raw {
        Value::Object(m) if !m.is_empty() => m,
        _ => {
            let mut allowed = vec!["text"];
            allowed.extend(ctx.kinds.0.keys().map(String::as_str));
            return Err(fail("query", "a query is a string or a non-empty object", &allowed));
        }
    };
    let text = m.get("text").map(parse_text).transpose()?;
    let mut sources: Vec<SourceReq> = m.iter().filter(|(k, _)| *k != "text").map(|(k, v)| source(k, v, ctx)).collect::<Result<_, _>>()?;
    if sources.is_empty() {
        sources = defaults()?;
    }
    Ok((text, sources))
}

/// The validated request for `search query` arguments, as the caller may see it.
pub fn parse(args: &Value, ctx: Context) -> Result<Request, QueryError> {
    let empty = Map::new();
    let args = args.as_object().unwrap_or(&empty);
    for key in args.keys() {
        if key != "query" && key != "max" && !key.starts_with("_caller_") {
            return Err(fail(key, "unknown argument", &["query", "max"]));
        }
    }
    let Some(q) = args.get("query") else {
        return Err(fail("query", "missing", &["a string, or an object of text and sources"]));
    };
    let max = match args.get("max") {
        None | Some(Value::Null) => DEFAULT_MAX,
        Some(v) => parse_max(v)?,
    };
    let (text, sources) = query(q, &ctx)?;
    let mut here = Vec::new();
    let mut remote: IndexMap<String, Vec<SourceReq>> = IndexMap::new();
    let mut elsewhere = Vec::new();
    let mut seen = HashSet::new();
    for s in sources {
        if !seen.insert((s.kind.clone(), label(&s.node))) {
            return Err(fail(&format!("query.{}", s.name), "this source is named twice", &[]));
        }
        if s.node.is_empty() {
            here.push(s);
        } else if ctx.relayed {
            elsewhere.push(s.name);
        } else {
            remote.entry(label(&s.node)).or_default().push(s);
        }
    }
    Ok(Request { text, sources: here, remote, max, relayed: ctx.relayed, veil: ctx.veil, elsewhere })
}
