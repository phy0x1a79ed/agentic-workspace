//! The sources other plugins declare: one `[[service.archive]]` table per source in a manifest,
//! which the gateway lists under `archive` in each `awm services` row. A live table names the
//! verbs search asks at query time. An index table is listed with its mistake, because search
//! no longer reads change feeds.

use awm_index::fields::{parse_fields, Class, Fields};
use awm_pycompat::repr::str_repr;
use indexmap::IndexMap;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

pub const MODES: [&str; 2] = ["index", "live"];
const READY: &str = "ready";

/// One declared source.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Declared {
    pub name: String,
    pub service: String,
    pub mode: String,
    pub tool: String,
    pub export: String,
    pub query: String,
    pub get: String,
    pub topic: String,
    pub optin: bool,
    /// Set when the table is unusable. The source is still listed, so the mistake shows.
    pub error: String,
    pub table: Value,
}

impl Declared {
    pub fn fields(&self) -> (Fields, Vec<String>) {
        parse_fields(&self.table).unwrap_or_default()
    }
}

fn name_ok(name: &str) -> bool {
    let mut cs = name.chars();
    cs.next().is_some_and(|c| c.is_ascii_lowercase())
        && name.chars().count() <= 32
        && cs.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
}

fn text(table: &Value, key: &str) -> String {
    match table.get(key) {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(false)) => String::new(),
        Some(other) => crate::hits::py_str(other),
    }
}

/// The published `<tool>_<verb>` a table names, or `""` when no tool of the service fits.
fn published(tools: &[String], named: &str) -> String {
    if tools.iter().any(|t| named.starts_with(&format!("{t}_")) && named.len() > t.len() + 1) {
        return named.to_string();
    }
    if tools.len() == 1 && !named.contains('_') {
        return format!("{}_{named}", tools[0]);
    }
    String::new()
}

fn split(published: &str, tools: &[String]) -> (String, String) {
    let tool = tools.iter().filter(|t| published.starts_with(&format!("{t}_"))).max_by_key(|t| t.len()).cloned().unwrap_or_default();
    let verb = published[tool.len() + 1..].to_string();
    (tool, verb)
}

fn source(service: &str, table: &Value, tools: &[String]) -> Declared {
    let name = text(table, "name");
    let mode = text(table, "mode");
    let truthy = |v: Option<&Value>| match v {
        None | Some(Value::Null) | Some(Value::Bool(false)) => false,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(_) => true,
    };
    let base = Declared {
        name: name.clone(),
        service: service.into(),
        mode: mode.clone(),
        topic: text(table, "topic"),
        optin: truthy(table.get("optin")),
        table: table.clone(),
        ..Declared::default()
    };
    let fail = |error: String| Declared { error, ..base.clone() };
    if !name_ok(&name) {
        return fail(format!("source name {} must match [a-z][a-z0-9_-]{{0,31}}", str_repr(&name)));
    }
    if !MODES.contains(&mode.as_str()) {
        return fail(format!("mode {} is not one of ('index', 'live')", str_repr(&mode)));
    }
    let fields = match parse_fields(table) {
        Ok((f, _)) => f,
        Err(e) => return fail(e),
    };
    if mode == "index" && !fields.iter().any(|f| f.class == Class::Content) {
        return fail("an index source declares at least one content field".into());
    }
    let role = if mode == "index" { "export" } else { "query" };
    let named = text(table, role);
    let full = if named.is_empty() { String::new() } else { published(tools, &named) };
    if full.is_empty() {
        let reason = if named.is_empty() { "names none".to_string() } else { format!("names {}, which the service does not publish", str_repr(&named)) };
        return fail(format!("a {mode} source needs a `{role}` verb, and the table {reason}"));
    }
    let (tool, verb) = split(&full, tools);
    let reader = if mode == "live" { text(table, "get") } else { String::new() };
    let get = if reader.is_empty() { String::new() } else { published(std::slice::from_ref(&tool), &reader) };
    if !reader.is_empty() && get.is_empty() {
        return fail(format!("the table's `get` names {}, which the service does not publish under {}", str_repr(&reader), str_repr(&tool)));
    }
    let mut out = Declared { tool: tool.clone(), get: if get.is_empty() { String::new() } else { get[tool.len() + 1..].to_string() }, ..base };
    if mode == "index" {
        out.export = verb;
    } else {
        out.query = verb;
    }
    out
}

/// `({name: source}, running services)` from the rows of `awm services`. Only a `ready` service
/// counts. Two services declaring one name keep the first.
pub fn parse(entries: &[Value]) -> (IndexMap<String, Declared>, BTreeSet<String>) {
    let mut sources = IndexMap::new();
    let mut running = BTreeSet::new();
    for entry in entries {
        if entry.get("status").and_then(Value::as_str) != Some(READY) {
            continue;
        }
        let service = text(entry, "name");
        running.insert(service.clone());
        let mut tools: Vec<String> = entry
            .get("tools")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter(|t| crate::live::truthy(t)).map(crate::hits::py_str).collect())
            .unwrap_or_default();
        if tools.is_empty() {
            tools.push(service.clone());
        }
        for table in entry.get("archive").and_then(Value::as_array).into_iter().flatten() {
            let src = source(&service, table, &tools);
            if let Some(held) = sources.get(&src.name) {
                let held: &Declared = held;
                tracing::warn!("archive source {:?} declared by {service:?} is already held by {:?}", src.name, held.service);
                continue;
            }
            sources.insert(src.name.clone(), src);
        }
    }
    (sources, running)
}

/// `{kind: (fields, hit)}` from the archive tables of every plugin the gateway's loader read
/// (`awm plugins`' `archives`), running or not, so a query can name a kind only another node
/// serves. A malformed table, or one that declares no fields, is left out.
pub fn declared(snapshot: &Value) -> BTreeMap<String, (Fields, Vec<String>)> {
    let mut out = BTreeMap::new();
    for decl in snapshot.get("archives").and_then(Value::as_array).into_iter().flatten() {
        for table in decl.get("archive").and_then(Value::as_array).into_iter().flatten() {
            let name = text(table, "name");
            if name.is_empty() || out.contains_key(&name) {
                continue;
            }
            match parse_fields(table) {
                Ok((fields, hit)) if !fields.0.is_empty() => {
                    out.insert(name, (fields, hit));
                }
                Ok(_) => {}
                Err(e) => tracing::warn!("archive table {name:?}: {e}"),
            }
        }
    }
    out
}
