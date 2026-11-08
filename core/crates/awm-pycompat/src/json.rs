//! `json.dumps` output, byte for byte.

use crate::repr::float_repr;
use serde_json::Value;
use std::fmt::Write;

/// The `json.dumps` keyword arguments the ports use. The default is `json.dumps(v)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Dumps {
    pub sort_keys: bool,
    pub indent: Option<usize>,
    pub ensure_ascii: bool,
}

impl Default for Dumps {
    fn default() -> Self {
        Dumps { sort_keys: false, indent: None, ensure_ascii: true }
    }
}

/// `json.dumps(v)`: `", "` and `": "` separators, ASCII escapes, keys in insertion order.
pub fn dumps(v: &Value) -> String {
    dumps_with(v, Dumps::default())
}

pub fn dumps_with(v: &Value, opts: Dumps) -> String {
    let mut out = String::new();
    write_value(&mut out, v, opts, 0);
    out
}

fn write_value(out: &mut String, v: &Value, opts: Dumps, level: usize) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                let _ = write!(out, "{i}");
            } else if let Some(u) = n.as_u64() {
                let _ = write!(out, "{u}");
            } else {
                let f = n.as_f64().unwrap_or(f64::NAN);
                out.push_str(&match float_repr(f).as_str() {
                    "nan" => "NaN".to_string(),
                    "inf" => "Infinity".to_string(),
                    "-inf" => "-Infinity".to_string(),
                    s => s.to_string(),
                });
            }
        }
        Value::String(s) => write_str(out, s, opts.ensure_ascii),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                separator(out, i, opts, level + 1);
                write_value(out, item, opts, level + 1);
            }
            close(out, opts, level);
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            let mut pairs: Vec<(&String, &Value)> = map.iter().collect();
            if opts.sort_keys {
                pairs.sort_by(|a, b| a.0.cmp(b.0));
            }
            out.push('{');
            for (i, (k, item)) in pairs.into_iter().enumerate() {
                separator(out, i, opts, level + 1);
                write_str(out, k, opts.ensure_ascii);
                out.push_str(": ");
                write_value(out, item, opts, level + 1);
            }
            close(out, opts, level);
            out.push('}');
        }
    }
}

fn separator(out: &mut String, i: usize, opts: Dumps, level: usize) {
    match opts.indent {
        None if i > 0 => out.push_str(", "),
        None => {}
        Some(n) => {
            if i > 0 {
                out.push(',');
            }
            out.push('\n');
            out.push_str(&" ".repeat(n * level));
        }
    }
}

fn close(out: &mut String, opts: Dumps, level: usize) {
    if let Some(n) = opts.indent {
        out.push('\n');
        out.push_str(&" ".repeat(n * level));
    }
}

fn write_str(out: &mut String, s: &str, ensure_ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x08' => out.push_str("\\b"),
            '\x0c' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c if ensure_ascii && !(' '..='~').contains(&c) => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{:04x}", unit);
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
