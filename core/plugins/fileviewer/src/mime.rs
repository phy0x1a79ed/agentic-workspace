//! A file's `Content-Type`, as Python 3.12's `mimetypes.guess_type` gave it: the built-in
//! table, then each system `mime.types` file that exists, later entries winning.

use std::collections::HashMap;

use crate::mime_table::{ENCODINGS, KNOWN_FILES, SUFFIXES, TYPES};

pub const FALLBACK: &str = "application/octet-stream";

/// The URL schemes whose last path segment `urlparse` splits `;params` from.
const USES_PARAMS: &[&str] =
    &["", "ftp", "hdl", "prospero", "http", "imap", "https", "shttp", "rtsp", "rtsps", "rtspu", "sip", "sips", "mms", "sftp", "tel"];

#[derive(Debug, Clone)]
pub struct MimeDb {
    types: HashMap<String, String>,
}

impl MimeDb {
    /// The built-in table alone.
    pub fn defaults() -> Self {
        Self { types: TYPES.iter().map(|(e, t)| (e.to_string(), t.to_string())).collect() }
    }

    /// The table the Python service used on this host: the defaults, then every known file.
    pub fn system() -> Self {
        let mut db = Self::defaults();
        for f in KNOWN_FILES {
            if std::path::Path::new(f).is_file() {
                match std::fs::read_to_string(f) {
                    Ok(text) => db.read(&text),
                    Err(e) => tracing::warn!("skipping {f}: {e}"),
                }
            }
        }
        db
    }

    /// Add the entries of a `mime.types` text: a type, then its suffixes, `#` to end of line ignored.
    pub fn read(&mut self, text: &str) {
        for line in text.lines() {
            let mut words: Vec<&str> = line.split_whitespace().collect();
            if let Some(i) = words.iter().position(|w| w.starts_with('#')) {
                words.truncate(i);
            }
            let Some((ty, suffixes)) = words.split_first() else { continue };
            for s in suffixes {
                self.types.insert(format!(".{s}"), ty.to_string());
            }
        }
    }

    /// The type `guess_type(name)[0]` returned, or `None`.
    pub fn guess(&self, name: &str) -> Option<String> {
        let url = match split_scheme(name) {
            Some((scheme, rest)) if scheme.len() > 1 => {
                let mut rest = rest.split('#').next().unwrap_or("");
                rest = rest.split('?').next().unwrap_or("");
                if USES_PARAMS.contains(&scheme.as_str()) {
                    rest = rest.split(';').next().unwrap_or("");
                }
                if scheme == "data" {
                    return data_type(rest);
                }
                rest.to_string()
            }
            _ => name.to_string(),
        };
        let (mut base, mut ext) = splitext(&url);
        while let Some((_, to)) = SUFFIXES.iter().find(|(from, _)| *from == ext.to_lowercase()) {
            (base, ext) = splitext(&format!("{base}{to}"));
        }
        if ENCODINGS.iter().any(|(e, _)| *e == ext) {
            (_, ext) = splitext(&base);
        }
        self.types.get(&ext.to_lowercase()).cloned()
    }
}

/// `urlsplit`'s scheme: the text before the first `:`, when it starts with an ASCII letter
/// and holds only scheme characters. It is read after stripping leading controls and spaces
/// and every tab and line break, so `rest` is that stripped text.
fn split_scheme(name: &str) -> Option<(String, String)> {
    let cleaned: String = name.trim_start_matches(|c: char| c <= ' ').chars().filter(|c| !matches!(c, '\t' | '\r' | '\n')).collect();
    let i = cleaned.find(':')?;
    let scheme = &cleaned[..i];
    let first = scheme.chars().next()?;
    (first.is_ascii_alphabetic() && scheme.chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c)))
        .then(|| (scheme.to_lowercase(), cleaned[i + 1..].to_string()))
}

fn data_type(url: &str) -> Option<String> {
    let comma = url.find(',')?;
    let ty = match url[..comma].find(';') {
        Some(semi) => &url[..semi],
        None => &url[..comma],
    };
    Some(if ty.contains('=') || !ty.contains('/') { "text/plain".into() } else { ty.into() })
}

/// `posixpath.splitext`: the extension starts at the last dot of the last segment, unless
/// only dots come before it.
fn splitext(p: &str) -> (String, String) {
    let file_start = p.rfind('/').map_or(0, |i| i + 1);
    match p.rfind('.') {
        Some(dot) if dot >= file_start && p[file_start..dot].chars().any(|c| c != '.') => (p[..dot].to_string(), p[dot..].to_string()),
        _ => (p.to_string(), String::new()),
    }
}
