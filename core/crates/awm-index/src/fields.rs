//! A source's declared fields: each with a class, the schema its filters are checked against.

use awm_pycompat::repr::{repr, str_repr};
use serde_json::Value;

/// How a field is stored, filtered and searched.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Class {
    /// Full-text searched and chunked for vectors. Never filtered.
    Content,
    /// A list of strings, filtered by value.
    Categorical,
    Numeric,
    Datetime,
}

impl Class {
    pub const NAMES: [&'static str; 4] = ["content", "categorical", "numeric", "datetime"];

    pub fn parse(s: &str) -> Option<Class> {
        Some(match s {
            "content" => Class::Content,
            "categorical" => Class::Categorical,
            "numeric" => Class::Numeric,
            "datetime" => Class::Datetime,
            _ => return None,
        })
    }
}

/// One declared field. `nested` is the separator under which a categorical value covers the
/// values below it, such as `/` for a scope, or empty.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Field {
    pub name: String,
    pub class: Class,
    pub nested: String,
}

impl Field {
    pub fn new(name: &str, class: Class) -> Self {
        Field { name: name.into(), class, nested: String::new() }
    }

    pub fn nested(name: &str, sep: &str) -> Self {
        Field { name: name.into(), class: Class::Categorical, nested: sep.into() }
    }
}

/// The fields of a source, in declaration order.
#[derive(Debug, Clone, Default, PartialEq, Eq, Hash)]
pub struct Fields(pub Vec<Field>);

impl Fields {
    pub fn get(&self, name: &str) -> Option<&Field> {
        self.0.iter().find(|f| f.name == name)
    }

    pub fn iter(&self) -> impl Iterator<Item = &Field> {
        self.0.iter()
    }

    pub fn content(&self) -> Vec<&str> {
        self.of(Class::Content)
    }

    pub fn of(&self, class: Class) -> Vec<&str> {
        self.0.iter().filter(|f| f.class == class).map(|f| f.name.as_str()).collect()
    }
}

fn field_name_ok(name: &str) -> bool {
    let mut cs = name.chars();
    cs.next().is_some_and(|c| c.is_ascii_lowercase())
        && name.len() <= 32
        && cs.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// `registry.parse_fields`: the `fields` and `hit` of an archive table, or the first mistake.
pub fn parse_fields(table: &Value) -> Result<(Fields, Vec<String>), String> {
    let empty = serde_json::Map::new();
    let raw = match table.get("fields") {
        None | Some(Value::Null) => &empty,
        Some(Value::Object(m)) => m,
        Some(_) => return Err("`fields` must be a table of name = class".into()),
    };
    let mut fields = Vec::new();
    for (name, spec) in raw {
        let (cls, nested) = match spec {
            Value::String(s) => (s.clone(), String::new()),
            Value::Object(m) => {
                let text = |k: &str| m.get(k).and_then(Value::as_str).unwrap_or("").to_string();
                (text("class"), text("nested"))
            }
            _ => return Err(format!("field {} must be a class name or a table with `class`", str_repr(name))),
        };
        if !field_name_ok(name) || name == "id" {
            return Err(format!("field name {} must match [a-z][a-z0-9_]{{0,31}} and not be `id`", str_repr(name)));
        }
        let Some(class) = Class::parse(&cls) else {
            let names: Vec<Value> = Class::NAMES.iter().map(|n| Value::String(n.to_string())).collect();
            let tuple = awm_pycompat::repr::Py::Tuple(names.iter().map(Into::into).collect()).repr();
            return Err(format!("field {} has class {}, not one of {tuple}", str_repr(name), str_repr(&cls)));
        };
        if !nested.is_empty() && class != Class::Categorical {
            return Err(format!("field {}: only a categorical field nests", str_repr(name)));
        }
        fields.push(Field { name: name.clone(), class, nested });
    }
    let fields = Fields(fields);
    let hit = match table.get("hit") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(a)) if a.iter().all(|h| h.as_str().is_some_and(|s| fields.get(s).is_some())) => {
            a.iter().map(|h| h.as_str().unwrap().to_string()).collect()
        }
        Some(other) => return Err(format!("`hit` must list declared fields; it is {}", repr(other))),
    };
    Ok((fields, hit))
}
