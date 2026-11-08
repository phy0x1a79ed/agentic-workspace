//! Arguments read as the Python handlers read them: `args["k"]` raises `KeyError`,
//! `args.get("k")` is `None` when absent, `bool(...)` is Python truthiness and `int(...)`
//! Python's conversion.

use serde_json::Value;

use crate::Fail;

/// Python truthiness.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's `str()` of a JSON value.
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => awm_pycompat::repr::repr(other),
    }
}

pub fn type_name(v: &Value) -> &'static str {
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

/// Python's `int(v)`.
pub fn py_int(v: &Value) -> Result<i64, Fail> {
    match v {
        Value::Number(n) => Ok(n.as_i64().unwrap_or_else(|| n.as_f64().unwrap_or(0.0).trunc() as i64)),
        Value::Bool(b) => Ok(*b as i64),
        Value::String(s) => awm_pycompat::pystr::strip(s)
            .replace('_', "")
            .parse::<i64>()
            .map_err(|_| Fail::value(format!("invalid literal for int() with base 10: {}", awm_pycompat::repr::str_repr(s)))),
        other => Err(Fail::new("TypeError", format!("int() argument must be a string, a bytes-like object or a real number, not '{}'", type_name(other)))),
    }
}

/// `args["key"]`.
pub fn required<'a>(a: &'a Value, key: &str) -> Result<&'a Value, Fail> {
    a.get(key).ok_or_else(|| Fail::new("KeyError", awm_pycompat::repr::str_repr(key)))
}

/// `args["key"]` used as a string. A non-string value is used as its `str()`.
pub fn req_str(a: &Value, key: &str) -> Result<String, Fail> {
    required(a, key).map(py_str)
}

/// `args.get("key")`: `None` when absent or null.
pub fn opt_str(a: &Value, key: &str) -> Option<String> {
    match a.get(key) {
        None | Some(Value::Null) => None,
        Some(v) => Some(py_str(v)),
    }
}

/// `args.get("key") or None`: `None` when absent, null or empty.
pub fn str_or_none(a: &Value, key: &str) -> Option<String> {
    a.get(key).filter(|v| truthy(v)).map(py_str)
}

/// `bool(args.get("key", False))`.
pub fn flag(a: &Value, key: &str) -> bool {
    a.get(key).is_some_and(truthy)
}

/// `int(args.get("key", default))`.
pub fn int_or(a: &Value, key: &str, default: i64) -> Result<i64, Fail> {
    match a.get(key) {
        None => Ok(default),
        Some(v) => py_int(v),
    }
}

/// `args.get("key", default)` as a string, where an explicit null stays null.
pub fn get_or(a: &Value, key: &str, default: &str) -> Option<String> {
    match a.get(key) {
        None => Some(default.to_string()),
        Some(Value::Null) => None,
        Some(v) => Some(py_str(v)),
    }
}

/// A string-valued argument taken as a list of strings, the way a Python loop over it
/// iterates: a list item by item, a string character by character.
pub fn str_list(v: &Value) -> Result<Vec<String>, Fail> {
    match v {
        Value::Array(a) => Ok(a.iter().map(py_str).collect()),
        Value::String(s) => Ok(s.chars().map(String::from).collect()),
        other => Err(Fail::new("TypeError", format!("'{}' object is not iterable", type_name(other)))),
    }
}
