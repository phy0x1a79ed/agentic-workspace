//! Python `str` methods whose rules differ from Rust's `str`.

use crate::chars::{is_digit, is_linebreak, is_space, lower_char, sigma_cased, sigma_skip};

/// `s.split()` with no separator: runs of `isspace` characters, no empty parts.
pub fn split(s: &str) -> Vec<&str> {
    s.split(is_space).filter(|p| !p.is_empty()).collect()
}

/// `s.strip()`.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// `s.lstrip()`.
pub fn lstrip(s: &str) -> &str {
    s.trim_start_matches(is_space)
}

/// `s.rstrip()`.
pub fn rstrip(s: &str) -> &str {
    s.trim_end_matches(is_space)
}

/// `s.splitlines(keepends)`. `\r\n` is one boundary.
pub fn splitlines(s: &str, keepends: bool) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if !is_linebreak(c) {
            continue;
        }
        let mut end = i + c.len_utf8();
        if c == '\r' {
            if let Some(&(_, '\n')) = it.peek() {
                it.next();
                end += 1;
            }
        }
        out.push(&s[start..if keepends { end } else { i }]);
        start = end;
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// `s.lower()`, with Python's final-sigma rule.
pub fn lower(s: &str) -> String {
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    for (i, &c) in chars.iter().enumerate() {
        if c == '\u{3a3}' {
            out.push(if final_sigma(&chars, i) { '\u{3c2}' } else { '\u{3c3}' });
        } else {
            lower_char(c, &mut out);
        }
    }
    out
}

fn final_sigma(chars: &[char], i: usize) -> bool {
    let before = chars[..i].iter().rev().find(|&&c| !sigma_skip(c));
    if !before.is_some_and(|&c| sigma_cased(c)) {
        return false;
    }
    let after = chars[i + 1..].iter().find(|&&c| !sigma_skip(c));
    !after.is_some_and(|&c| sigma_cased(c))
}

/// `len(s)`, in code points.
pub fn len(s: &str) -> usize {
    s.chars().count()
}

/// `s[:n]`, in code points.
pub fn head(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

/// `s.isdigit()`: true for a non-empty string of digits, superscripts included.
pub fn isdigit(s: &str) -> bool {
    !s.is_empty() && s.chars().all(is_digit)
}

/// `" ".join(s.split()).lower()`, the whitespace-folded lowercase that tag and mark names use.
pub fn norm(s: &str) -> String {
    lower(&split(s).join(" "))
}

/// `unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()`.
pub fn ascii_fold(s: &str) -> String {
    use unicode_normalization::UnicodeNormalization;
    s.nfkd().filter(char::is_ascii).map(|c| c.to_ascii_lowercase()).collect()
}
