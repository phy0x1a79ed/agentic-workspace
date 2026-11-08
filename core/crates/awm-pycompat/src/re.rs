//! Python `re` semantics on str patterns: scanners for the patterns the ports use, and a pattern
//! translator for the `regex` crate.

use crate::chars::{is_space, is_word};
use crate::tables;
use std::fmt::Write;

/// `re.findall(r"\w+", s)`.
pub fn words(s: &str) -> Vec<&str> {
    runs(s, is_word)
}

/// `re.findall(r"\w+|[^\w\s]", s)`: word runs, and each other non-space character alone.
pub fn tokens(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut word_start = None;
    for (i, c) in s.char_indices() {
        if is_word(c) {
            word_start.get_or_insert(i);
            continue;
        }
        if let Some(w) = word_start.take() {
            out.push(&s[w..i]);
        }
        if !is_space(c) {
            out.push(&s[i..i + c.len_utf8()]);
        }
    }
    if let Some(w) = word_start {
        out.push(&s[w..]);
    }
    out
}

/// `re.sub(r"\W+", rep, s)`.
pub fn sub_nonword(s: &str, rep: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_run = false;
    for c in s.chars() {
        if is_word(c) {
            out.push(c);
            in_run = false;
        } else if !in_run {
            out.push_str(rep);
            in_run = true;
        }
    }
    out
}

/// `re.split(r"(?<=[.!?])\s+|\n", s)`: break after sentence punctuation and at each newline.
pub fn sentence_split(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut prev: Option<char> = None;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if is_space(c) && matches!(prev, Some('.' | '!' | '?')) {
            let mut end = i + c.len_utf8();
            while let Some(&(j, d)) = it.peek() {
                if !is_space(d) {
                    break;
                }
                end = j + d.len_utf8();
                it.next();
            }
            out.push(&s[start..i]);
            start = end;
            prev = s[..end].chars().next_back();
        } else if c == '\n' {
            out.push(&s[start..i]);
            start = i + 1;
            prev = Some(c);
        } else {
            prev = Some(c);
        }
    }
    out.push(&s[start..]);
    out
}

fn runs(s: &str, pred: fn(char) -> bool) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = None;
    for (i, c) in s.char_indices() {
        match (pred(c), start) {
            (true, None) => start = Some(i),
            (false, Some(st)) => {
                out.push(&s[st..i]);
                start = None;
            }
            _ => {}
        }
    }
    if let Some(st) = start {
        out.push(&s[st..]);
    }
    out
}

/// A Python construct with no faithful `regex` crate equivalent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unsupported(pub String);

impl std::fmt::Display for Unsupported {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "the pattern needs {}, which the regex crate cannot match as Python does", self.0)
    }
}

impl std::error::Error for Unsupported {}

/// What `$` means in the translated pattern.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dollar {
    /// Refuse `$`: Python's `$` also matches before a final `\n`, which `regex` cannot express.
    Refuse,
    /// Translate `$` to `\z`: the caller matches text that never ends in `\n`, such as one line.
    EndOfText,
}

/// Translate a Python str pattern into `regex` crate syntax with Python's `\w`, `\s` and `\d`.
///
/// Refuses `\b`, `\B`, lookarounds and backreferences, and `$` unless `dollar` allows it.
pub fn translate(pattern: &str, dollar: Dollar) -> Result<String, Unsupported> {
    let mut out = String::with_capacity(pattern.len() * 2);
    let mut chars = pattern.chars().peekable();
    let mut in_class = false;
    let mut class_start = false;
    while let Some(c) = chars.next() {
        let at_class_start = std::mem::take(&mut class_start);
        match c {
            '\\' => {
                let Some(e) = chars.next() else {
                    return Err(Unsupported("a trailing backslash".into()));
                };
                match e {
                    'w' | 'W' | 's' | 'S' | 'd' | 'D' => {
                        let table = match e.to_ascii_lowercase() {
                            'w' => tables::WORD,
                            's' => tables::SPACE,
                            _ => tables::DECIMAL,
                        };
                        let negate = e.is_ascii_uppercase();
                        out.push('[');
                        if negate {
                            out.push('^');
                        }
                        push_ranges(&mut out, table);
                        out.push(']');
                    }
                    'b' if !in_class => return Err(Unsupported("\\b".into())),
                    'B' => return Err(Unsupported("\\B".into())),
                    'Z' => out.push_str("\\z"),
                    '1'..='9' if !in_class => return Err(Unsupported(format!("the backreference \\{e}"))),
                    _ => {
                        out.push('\\');
                        out.push(e);
                    }
                }
            }
            '[' if !in_class => {
                in_class = true;
                class_start = true;
                out.push('[');
                if chars.peek() == Some(&'^') {
                    out.push(chars.next().unwrap());
                }
                if chars.peek() == Some(&']') {
                    chars.next();
                    out.push_str("\\]");
                }
            }
            '[' => out.push_str("\\["),
            ']' if in_class && !at_class_start => {
                in_class = false;
                out.push(']');
            }
            '(' if !in_class && chars.peek() == Some(&'?') => {
                let rest: String = chars.clone().take(3).collect();
                if rest.starts_with("?=") || rest.starts_with("?!") || rest.starts_with("?<=") || rest.starts_with("?<!") {
                    return Err(Unsupported("a lookaround".into()));
                }
                if rest.starts_with("?P=") {
                    return Err(Unsupported("a named backreference".into()));
                }
                out.push('(');
            }
            '$' if !in_class => match dollar {
                Dollar::Refuse => return Err(Unsupported("$".into())),
                Dollar::EndOfText => out.push_str("\\z"),
            },
            '&' | '~' | '-' if in_class && chars.peek() == Some(&c) => {
                out.push('\\');
                out.push(c);
            }
            _ => out.push(c),
        }
    }
    Ok(out)
}

fn push_ranges(out: &mut String, table: &[(u32, u32)]) {
    for &(lo, hi) in table {
        if lo == hi {
            let _ = write!(out, "\\x{{{lo:x}}}");
        } else {
            let _ = write!(out, "\\x{{{lo:x}}}-\\x{{{hi:x}}}");
        }
    }
}
