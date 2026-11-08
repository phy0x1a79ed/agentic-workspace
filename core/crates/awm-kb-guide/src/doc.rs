//! A Markdown file cut at its headings, one part per heading, and a verb's describe as text.

use awm_pycompat::pystr::strip;
use serde_json::Value;
use std::collections::HashMap;
use awm_pycompat::repr::truthy;

/// One part of a file: the text under one heading, up to the next heading of any level.
#[derive(Debug, Clone, PartialEq)]
pub struct Part {
    /// The heading's text, or the file's name for the text before the first heading.
    pub title: String,
    /// The headings above it and its own, joined by ` > `. Empty before the first heading.
    pub heading: String,
    pub text: String,
    /// A key for the part, stable while its headings stay.
    pub slug: String,
}

fn heading(line: &str) -> Option<(usize, String)> {
    let level = line.bytes().take_while(|b| *b == b'#').count();
    if !(1..=6).contains(&level) {
        return None;
    }
    let rest = &line[level..];
    if !rest.is_empty() && !rest.starts_with([' ', '\t']) {
        return None;
    }
    let text = rest.trim().trim_end_matches('#').trim_end().to_string();
    (!text.is_empty()).then_some((level, text))
}

fn fence(line: &str) -> Option<char> {
    let t = line.trim_start();
    if t.starts_with("```") {
        Some('`')
    } else if t.starts_with("~~~") {
        Some('~')
    } else {
        None
    }
}

fn slugify(s: &str) -> String {
    let mut out = String::new();
    for c in s.chars().flat_map(char::to_lowercase) {
        if c.is_alphanumeric() {
            out.push(c);
        } else if !out.ends_with('-') {
            out.push('-');
        }
    }
    out.trim_matches('-').to_string()
}

/// The parts of `text`, in file order. A part with no text under its heading is left out.
pub fn split(text: &str, name: &str) -> Vec<Part> {
    let mut parts = Vec::new();
    let mut stack: Vec<(usize, String)> = Vec::new();
    let mut body: Vec<&str> = Vec::new();
    let mut open: Option<char> = None;
    let mut seen: HashMap<String, usize> = HashMap::new();
    let mut flush = |stack: &[(usize, String)], body: &mut Vec<&str>, parts: &mut Vec<Part>| {
        let text = strip(&body.join("\n")).to_string();
        body.clear();
        if text.is_empty() {
            return;
        }
        let path = stack.iter().map(|(_, t)| t.as_str()).collect::<Vec<_>>().join(" > ");
        let title = stack.last().map(|(_, t)| t.clone()).unwrap_or_else(|| name.to_string());
        let base = if path.is_empty() { "top".to_string() } else { slugify(&path) };
        let n = seen.entry(base.clone()).or_insert(0);
        *n += 1;
        let slug = if *n == 1 { base } else { format!("{base}-{n}") };
        parts.push(Part { title, heading: path, text, slug });
    };
    for line in text.lines() {
        if let Some(c) = fence(line) {
            open = match open {
                None => Some(c),
                Some(o) if o == c => None,
                other => other,
            };
            body.push(line);
            continue;
        }
        if open.is_none() {
            if let Some((level, title)) = heading(line) {
                flush(&stack, &mut body, &mut parts);
                while stack.last().is_some_and(|(l, _)| *l >= level) {
                    stack.pop();
                }
                stack.push((level, title));
                continue;
            }
        }
        body.push(line);
    }
    flush(&stack, &mut body, &mut parts);
    parts
}

/// `_params`: one line per param, its type and whether it is required.
pub fn params(params: &[Value]) -> String {
    let mut lines = Vec::new();
    for p in params.iter().filter(|p| p.is_object()) {
        let mut marks = Vec::new();
        match p.get("type") {
            Some(Value::Array(kinds)) if !kinds.is_empty() => marks.push(kinds.iter().map(py).collect::<Vec<_>>().join(", ")),
            Some(k) if truthy(k) => marks.push(py(k)),
            _ => {}
        }
        if p.get("required").is_some_and(truthy) {
            marks.push("required".into());
        }
        let name = p.get("name").map(py).unwrap_or_else(|| "None".into());
        let mut head = format!("- {name}");
        if !marks.is_empty() {
            head.push_str(&format!(" ({})", marks.join(", ")));
        }
        match p.get("description").filter(|d| truthy(d)) {
            Some(d) => lines.push(format!("{head}: {}", py(d))),
            None => lines.push(head),
        }
    }
    lines.join("\n")
}

fn py(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        other => awm_pycompat::repr::repr(other),
    }
}

/// A verb's title and text, as the `awm` source wrote them: `<tool> <verb>: <first line>`, and
/// the description with its params.
pub fn verb_entry(tool: &str, verb: &Value) -> Option<(String, String, String)> {
    let name = verb.get("verb").and_then(Value::as_str).filter(|v| !v.is_empty())?.to_string();
    let description = strip(&verb.get("description").map(py).filter(|d| d != "None").unwrap_or_default()).to_string();
    let first = description.lines().map(strip).find(|l| !l.is_empty()).unwrap_or("").to_string();
    let ps = params(verb.get("params").and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[]));
    let mut text = description;
    if !ps.is_empty() {
        text.push_str("\n\nParams:\n");
        text.push_str(&ps);
    }
    if text.is_empty() {
        text = name.clone();
    }
    let title: String = format!("{tool} {name}: {first}").chars().take(200).collect();
    Some((name, title, text))
}
