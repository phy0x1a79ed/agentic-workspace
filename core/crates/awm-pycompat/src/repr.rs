//! Python `repr` of the values the ports format into messages and digests.

use crate::chars::is_printable;
use serde_json::Value;
use std::fmt::Write;

/// A Python value, for the `repr` of shapes JSON lacks: tuples, non-string keys, big ints.
#[derive(Debug, Clone, PartialEq)]
pub enum Py {
    None,
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(String),
    List(Vec<Py>),
    Tuple(Vec<Py>),
    Dict(Vec<(Py, Py)>),
}

impl From<&Value> for Py {
    fn from(v: &Value) -> Self {
        match v {
            Value::Null => Py::None,
            Value::Bool(b) => Py::Bool(*b),
            Value::Number(n) => match (n.as_i64(), n.as_u64()) {
                (Some(i), _) => Py::Int(i.into()),
                (None, Some(u)) => Py::Int(u.into()),
                _ => Py::Float(n.as_f64().unwrap_or(f64::NAN)),
            },
            Value::String(s) => Py::Str(s.clone()),
            Value::Array(a) => Py::List(a.iter().map(Py::from).collect()),
            Value::Object(m) => Py::Dict(m.iter().map(|(k, v)| (Py::Str(k.clone()), Py::from(v))).collect()),
        }
    }
}

impl Py {
    pub fn repr(&self) -> String {
        let mut out = String::new();
        self.write_repr(&mut out);
        out
    }

    fn write_repr(&self, out: &mut String) {
        match self {
            Py::None => out.push_str("None"),
            Py::Bool(true) => out.push_str("True"),
            Py::Bool(false) => out.push_str("False"),
            Py::Int(i) => {
                let _ = write!(out, "{i}");
            }
            Py::Float(f) => out.push_str(&float_repr(*f)),
            Py::Str(s) => out.push_str(&str_repr(s)),
            Py::List(items) => seq(out, '[', items, ']'),
            Py::Tuple(items) if items.len() == 1 => {
                out.push('(');
                items[0].write_repr(out);
                out.push_str(",)");
            }
            Py::Tuple(items) => seq(out, '(', items, ')'),
            Py::Dict(pairs) => {
                out.push('{');
                for (i, (k, v)) in pairs.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    k.write_repr(out);
                    out.push_str(": ");
                    v.write_repr(out);
                }
                out.push('}');
            }
        }
    }
}

fn seq(out: &mut String, open: char, items: &[Py], close: char) {
    out.push(open);
    for (i, item) in items.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        item.write_repr(out);
    }
    out.push(close);
}

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
        other => repr(other),
    }
}

/// `repr` of a JSON value.
pub fn repr(v: &Value) -> String {
    Py::from(v).repr()
}

/// `repr(s)` of a str: single quotes unless the text holds `'` and no `"`.
pub fn str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c if c.is_ascii() || is_printable(c) => out.push(c),
            c if (c as u32) <= 0xff => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c if (c as u32) <= 0xffff => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => {
                let _ = write!(out, "\\U{:08x}", c as u32);
            }
        }
    }
    out.push(quote);
    out
}

/// `repr(f)` of a float: the shortest round-trip digits, in exponent form below 1e-4 and from 1e16.
pub fn float_repr(f: f64) -> String {
    if f.is_nan() {
        return "nan".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf" } else { "-inf" }.into();
    }
    let sci = format!("{:e}", f.abs());
    let (mantissa, exp) = sci.split_once('e').expect("LowerExp writes an exponent");
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let exp: i32 = exp.parse().expect("LowerExp writes an integer exponent");
    let sign = if f.is_sign_negative() { "-" } else { "" };
    let decpt = exp + 1;
    if f == 0.0 {
        return format!("{sign}0.0");
    }
    if decpt <= -4 || decpt > 16 {
        let (head, tail) = digits.split_at(1);
        let dot = if tail.is_empty() { String::new() } else { format!(".{tail}") };
        let esign = if exp < 0 { '-' } else { '+' };
        return format!("{sign}{head}{dot}e{esign}{:02}", exp.abs());
    }
    if decpt <= 0 {
        return format!("{sign}0.{}{digits}", "0".repeat((-decpt) as usize));
    }
    let decpt = decpt as usize;
    if digits.len() <= decpt {
        format!("{sign}{digits}{}.0", "0".repeat(decpt - digits.len()))
    } else {
        format!("{sign}{}.{}", &digits[..decpt], &digits[decpt..])
    }
}
