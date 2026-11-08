//! Grep the archive without unpacking it, and the live `transcripts` search source.
//!
//! A session is filed under the directory it started in, but it can `cd` anywhere. Each line
//! records its `cwd`, so a caller that may not see some paths is shown nothing from a session
//! that ever worked in one of them.

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::sync::LazyLock;

use flate2::read::MultiGzDecoder;
use serde_json::{json, Value};

use crate::layout::{sessions, Side, Tree};

pub const MAX_HITS: i64 = 200;
pub const QUERY_MAX_HITS: i64 = 20;
const LINE_CHARS: usize = 400;

static CWD: LazyLock<regex::Regex> = LazyLock::new(|| regex::Regex::new(r#""cwd"\s*:\s*"((?:[^"\\]|\\.)*)""#).expect("the cwd pattern"));

/// Whether a line ran in a hidden path, testing each distinct `cwd` once.
pub struct Entered<'a> {
    hide_path: &'a dyn Fn(&str) -> bool,
    seen: HashMap<String, bool>,
}

impl<'a> Entered<'a> {
    pub fn new(hide_path: &'a dyn Fn(&str) -> bool) -> Self {
        Self { hide_path, seen: HashMap::new() }
    }

    pub fn test(&mut self, line: &str) -> bool {
        if !line.contains("\"cwd\"") {
            return false;
        }
        for m in CWD.captures_iter(line) {
            let raw = &m[1];
            let cwd = serde_json::from_str::<String>(&format!("\"{raw}\"")).unwrap_or_else(|_| raw.to_string());
            let hidden = match self.seen.get(&cwd) {
                Some(h) => *h,
                None => {
                    let h = (self.hide_path)(&cwd);
                    self.seen.insert(cwd, h);
                    h
                }
            };
            if hidden {
                return true;
            }
        }
        false
    }
}

/// The lines of a gzipped text file as Python's `gzip.open(..., "rt", errors="replace")` yields
/// them: universal newlines, invalid UTF-8 replaced, terminators dropped.
pub struct Lines {
    inner: BufReader<MultiGzDecoder<BufReader<File>>>,
    carry_cr: bool,
}

impl Lines {
    pub fn open(path: &Path) -> std::io::Result<Self> {
        Ok(Self { inner: BufReader::with_capacity(1 << 16, MultiGzDecoder::new(BufReader::new(File::open(path)?))), carry_cr: false })
    }
}

impl Iterator for Lines {
    type Item = std::io::Result<String>;

    fn next(&mut self) -> Option<Self::Item> {
        let mut line: Vec<u8> = vec![];
        loop {
            let buf = match self.inner.fill_buf() {
                Ok(b) => b,
                Err(e) => return Some(Err(e)),
            };
            if buf.is_empty() {
                return (!line.is_empty()).then(|| Ok(String::from_utf8_lossy(&line).into_owned()));
            }
            let mut start = 0;
            if std::mem::take(&mut self.carry_cr) && buf[0] == b'\n' {
                start = 1;
            }
            if let Some(i) = buf[start..].iter().position(|&b| b == b'\n' || b == b'\r') {
                let end = start + i;
                line.extend_from_slice(&buf[start..end]);
                let cr = buf[end] == b'\r';
                let crlf = cr && buf.get(end + 1) == Some(&b'\n');
                self.inner.consume(end + 1 + crlf as usize);
                self.carry_cr = cr && !crlf;
                return Some(Ok(String::from_utf8_lossy(&line).into_owned()));
            }
            line.extend_from_slice(&buf[start..]);
            let n = buf.len();
            self.inner.consume(n);
        }
    }
}

/// Whether a gzipped transcript ever ran in a hidden path. An unreadable one counts as having
/// done so.
pub fn entered(transcript: &Path, hide_path: &dyn Fn(&str) -> bool) -> bool {
    let mut test = Entered::new(hide_path);
    let Ok(lines) = Lines::open(transcript) else { return true };
    for line in lines {
        match line {
            Ok(l) if test.test(&l) => return true,
            Ok(_) => {}
            Err(_) => return true,
        }
    }
    false
}

/// A compiled `transcripts_search` pattern.
pub type Pattern = fancy_regex::Regex;

/// Compile a Python pattern. `fancy-regex` covers its lookarounds and backreferences.
pub fn compile(pattern: &str) -> Result<Pattern, String> {
    fancy_regex::Regex::new(pattern).map_err(|e| e.to_string())
}

/// Archived lines matching `rx`, at most `limit`, as `{project, session, line, text}`. With
/// `hide_path`, a session that ever entered a hidden path gives no hit, so the limit fills
/// with lines the caller may see.
pub fn find(tree: &Tree, pattern: &str, rx: &Pattern, project: Option<&str>, limit: i64, hide: Option<&dyn Fn(&str) -> bool>, hide_path: Option<&dyn Fn(&str) -> bool>) -> Value {
    let mut hits: Vec<Value> = vec![];
    for s in sessions(&tree.archive, Side::Archive, hide) {
        if project.is_some_and(|p| !p.is_empty() && !s.project.contains(p)) {
            continue;
        }
        let Some(transcript) = &s.transcript else { continue };
        let room = limit - hits.len() as i64;
        let mut found: Vec<Value> = vec![];
        let mut test = hide_path.map(Entered::new);
        let mut tainted = false;
        match Lines::open(transcript) {
            Ok(lines) => {
                for (i, line) in lines.enumerate() {
                    let Ok(line) = line else {
                        tainted |= test.is_some();
                        break;
                    };
                    if test.as_mut().is_some_and(|t| t.test(&line)) {
                        tainted = true;
                        break;
                    }
                    if (found.len() as i64) < room && rx.is_match(&line).unwrap_or(false) {
                        let text = awm_pycompat::pystr::rstrip(awm_pycompat::pystr::head(&line, LINE_CHARS));
                        found.push(json!({"project": s.project, "session": s.session_id, "line": i + 1, "text": text}));
                        if found.len() as i64 >= room && test.is_none() {
                            break;
                        }
                    }
                }
            }
            Err(_) => tainted |= test.is_some(),
        }
        if tainted {
            continue;
        }
        hits.extend(found);
        if hits.len() as i64 >= limit {
            return json!({"pattern": pattern, "hits": hits, "truncated": true});
        }
    }
    json!({"pattern": pattern, "hits": hits, "truncated": false})
}

/// The live source: archived lines that hold `query` as typed, ignoring case, as search hits.
pub fn query(tree: &Tree, query: &str, project: Option<&str>, limit: i64, hide: Option<&dyn Fn(&str) -> bool>, hide_path: Option<&dyn Fn(&str) -> bool>) -> Value {
    let words = awm_pycompat::pystr::strip(query);
    let pattern = format!("(?i){}", regex::escape(words));
    let rx = compile(&pattern).expect("an escaped literal compiles");
    let found = find(tree, &pattern, &rx, project, limit, hide, hide_path);
    let hits: Vec<Value> = found["hits"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|h| json!({"id": format!("{}:{}", h["session"].as_str().unwrap_or(""), h["line"]), "title": h["project"], "snippet": h["text"], "project": h["project"]}))
        .collect();
    json!({"hits": hits, "truncated": found["truncated"]})
}
