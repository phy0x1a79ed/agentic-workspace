//! A validated source filter and the private marks, compiled to the Lance SQL prefilter that the
//! entry and chunk tables share. A port of search's `lance_query` filter half.

use crate::fields::{Class, Field, Fields};
use crate::QueryUnserved;
use awm_pycompat::repr::{float_repr, str_repr};
use serde_json::{Map, Value};

const SEP: &str = r"\x1f";

/// One private mark: a project, or one scope of it (nested under `/`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mark {
    pub project: String,
    pub scope: Option<String>,
}

/// What the caller may not see past.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Veil {
    /// A local caller: everything.
    None,
    /// The marks could not be read: only entries no mark could cover.
    Unreadable,
    /// A peer caller: entries no mark covers. `tags` overrides the tag clause with explicit
    /// values, for a source whose tag column holds ids rather than names.
    Marks { marks: Vec<Mark>, tags: Option<Vec<String>> },
}

pub fn quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

pub fn id_list<S: AsRef<str>>(ids: &[S]) -> String {
    format!("id IN ({})", ids.iter().map(|i| quote(i.as_ref())).collect::<Vec<_>>().join(", "))
}

fn regex_value(value: &str) -> String {
    if value == "*" {
        return format!("[^{SEP}]*");
    }
    let mut out = String::new();
    for c in value.chars() {
        if r"\.+*?()|[]{}^$".contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// A regex over a categorical's values joined by U+001F: `value` with `*` as a glob, and with
/// the values nested below it when the field nests.
pub fn pattern(value: &str, f: &Field) -> String {
    let mut body = String::new();
    let mut rest = value;
    while let Some(i) = rest.find('*') {
        if i > 0 {
            body.push_str(&regex_value(&rest[..i]));
        }
        body.push_str(&regex_value("*"));
        rest = &rest[i + 1..];
    }
    if !rest.is_empty() {
        body.push_str(&regex_value(rest));
    }
    let below = if !f.nested.is_empty() && !value.ends_with('*') {
        format!("({}[^{SEP}]*)?", regex_value(&f.nested))
    } else {
        String::new()
    };
    format!("(^|{SEP}){body}{below}({SEP}|$)")
}

fn joined(name: &str) -> String {
    format!("array_to_string({name}, chr(31))")
}

fn text_of(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Number(n) => n.as_i64().map(|i| i.to_string()).unwrap_or_else(|| float_repr(n.as_f64().unwrap_or(f64::NAN))),
        other => other.to_string(),
    }
}

fn equals(name: &str, f: &Field, value: &Value) -> String {
    if f.class == Class::Categorical {
        let text = text_of(value);
        if !text.contains('*') && f.nested.is_empty() {
            return format!("array_has({name}, {})", quote(&text));
        }
        return format!("regexp_like({}, {})", joined(name), quote(&pattern(&text, f)));
    }
    format!("{name} = {}", literal(f, value))
}

fn literal(f: &Field, value: &Value) -> String {
    match f.class {
        Class::Datetime => format!("timestamp {}", quote(&text_of(value))),
        Class::Numeric => float_repr(value.as_f64().unwrap_or(f64::NAN)),
        _ => quote(&text_of(value)),
    }
}

/// `_join`: one part bare, several parenthesised, none as `empty`.
pub fn join(op: &str, parts: Vec<String>, empty: &str) -> String {
    match parts.len() {
        0 => empty.to_string(),
        1 => parts.into_iter().next().unwrap(),
        _ => format!("({})", parts.join(&format!(" {op} "))),
    }
}

/// `NOT expr` that also holds for an entry without the field, as MongoDB's `$ne` does.
fn not(name: &str, f: &Field, expr: String) -> String {
    if f.class == Class::Categorical {
        format!("NOT ({expr})")
    } else {
        format!("({name} IS NULL OR NOT ({expr}))")
    }
}

fn list(value: &Value) -> &[Value] {
    value.as_array().map(Vec::as_slice).unwrap_or(&[])
}

fn field(name: &str, f: &Field, spec: &Value) -> Result<String, QueryUnserved> {
    let eq;
    let ops: Vec<(&str, &Value)> = match spec {
        Value::Object(m) => m.iter().map(|(k, v)| (k.as_str(), v)).collect(),
        other => {
            eq = other.clone();
            vec![("$eq", &eq)]
        }
    };
    let mut parts = Vec::new();
    for (op, value) in ops {
        let any = |vs: &[Value]| join("OR", vs.iter().map(|v| equals(name, f, v)).collect(), "false");
        parts.push(match op {
            "$eq" => equals(name, f, value),
            "$ne" => not(name, f, equals(name, f, value)),
            "$in" => any(list(value)),
            "$nin" => not(name, f, any(list(value))),
            "$all" => join("AND", list(value).iter().map(|v| equals(name, f, v)).collect(), "true"),
            "$gt" | "$gte" | "$lt" | "$lte" => {
                if f.class == Class::Categorical {
                    return Err(QueryUnserved(format!(
                        "{op} on the categorical field {}; use $eq, $ne, $in, $nin or $all",
                        str_repr(name)
                    )));
                }
                let cmp = match op {
                    "$gt" => ">",
                    "$gte" => ">=",
                    "$lt" => "<",
                    _ => "<=",
                };
                format!("{name} {cmp} {}", literal(f, value))
            }
            _ => return Err(QueryUnserved(format!("unknown operator {} on {}", str_repr(op), str_repr(name)))),
        });
    }
    Ok(join("AND", parts, "true"))
}

/// A Lance SQL condition for the validated filter `f`. `{}` gives `true`.
pub fn filter_sql(fields: &Fields, f: &Map<String, Value>) -> Result<String, QueryUnserved> {
    let mut parts = Vec::new();
    for (key, value) in f {
        let subs = || list(value).iter().map(|x| filter_sql(fields, x.as_object().unwrap_or(&Map::new()))).collect::<Result<Vec<_>, _>>();
        parts.push(match key.as_str() {
            "$and" => join("AND", subs()?, "true"),
            "$or" => join("OR", subs()?, "false"),
            "$nor" => format!("NOT {}", join("OR", subs()?, "false")),
            _ => {
                let f = fields.get(key).ok_or_else(|| QueryUnserved(format!("no field {}", str_repr(key))))?;
                field(key, f, value)?
            }
        });
    }
    Ok(join("AND", parts, "true"))
}

/// A condition that keeps only the entries the veil lets through.
///
/// A mark covers an entry by its project and scope, and by a tag equal to the marked project's
/// name, ignoring case. A source without those fields has nothing a mark can cover.
pub fn veil_sql(fields: &Fields, veil: &Veil) -> String {
    let has = |n: &str| fields.get(n).is_some_and(|f| f.class == Class::Categorical);
    let mut present: Vec<&str> = ["project", "scope", "tag"].into_iter().filter(|n| has(n)).collect();
    present.sort();
    let (marks, tags) = match veil {
        Veil::None => return "true".into(),
        Veil::Unreadable => {
            return join("AND", present.iter().map(|n| format!("cardinality({n}) = 0")).collect(), "true");
        }
        Veil::Marks { marks, tags } => (marks, tags),
    };
    let mut clauses = Vec::new();
    if has("project") {
        for mark in marks {
            let mut covered = format!("array_has(project, {})", quote(&mark.project));
            if let (Some(scope), true) = (&mark.scope, has("scope")) {
                covered.push_str(" AND ");
                covered.push_str(&equals("scope", &Field::nested("scope", "/"), &Value::String(scope.clone())));
            }
            clauses.push(format!("({covered})"));
        }
    }
    if has("tag") {
        match tags {
            Some(values) => {
                for v in values {
                    clauses.push(format!("array_has(tag, {})", quote(v)));
                }
            }
            None => {
                let mut projects: Vec<&str> = marks.iter().map(|m| m.project.as_str()).collect();
                projects.sort();
                projects.dedup();
                let tag = fields.get("tag").unwrap();
                for p in projects {
                    clauses.push(format!("regexp_like({}, {})", joined("tag"), quote(&format!("(?i){}", pattern(p, tag)))));
                }
            }
        }
    }
    if clauses.is_empty() {
        "true".into()
    } else {
        format!("NOT {}", join("OR", clauses, "false"))
    }
}

/// `_and`: the parts that are not `true`, joined.
pub fn and(parts: &[&str]) -> String {
    join("AND", parts.iter().filter(|p| !p.is_empty() && **p != "true").map(|p| p.to_string()).collect(), "true")
}

/// The filter and the veil together.
pub fn where_sql(fields: &Fields, f: &Map<String, Value>, veil: &Veil) -> Result<String, QueryUnserved> {
    Ok(and(&[&filter_sql(fields, f)?, &veil_sql(fields, veil)]))
}
