//! Reading gzipped transcripts as the Python did, and the `cwd`s their lines name.
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

pub const MAX_HITS: i64 = 200;
pub const QUERY_MAX_HITS: i64 = 20;
pub const LINE_CHARS: usize = 400;

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

    /// Call `each` with every `cwd` the line names, anywhere in it.
    pub fn cwds(line: &str, mut each: impl FnMut(&str)) {
        if !line.contains("\"cwd\"") {
            return;
        }
        for m in CWD.captures_iter(line) {
            let raw = &m[1];
            each(&serde_json::from_str::<String>(&format!("\"{raw}\"")).unwrap_or_else(|_| raw.to_string()));
        }
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
