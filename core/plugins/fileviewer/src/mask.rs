//! The deny list that hides secrets from a mount that exposes the whole filesystem. Globs
//! are gitignore-shaped: a leading `!` re-exposes, and the last matching glob wins.

use std::path::Path;

use crate::glob::{BadGlob, Glob};
use crate::resolve::realpath;

/// Matched whole against the resolved path relative to the root. A negation must come
/// before the secret patterns, or it re-exposes them.
pub const DEFAULT_MASK: &[&str] = &[
    "**/.git/**",
    "**/.ssh/**", "**/.ssh",
    "**/.gnupg/**", "**/.gnupg",
    "**/id_rsa", "**/id_dsa", "**/id_ecdsa", "**/id_ed25519",
    "**/*_rsa", "**/*_dsa", "**/*_ecdsa", "**/*_ed25519",
    "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx",
    "**/.certs/**", "**/.certs",
    "**/*.token", "**/auth.token",
    "**/.aws/**", "**/.aws",
    "**/.netrc", "**/.pgpass",
    "**/credentials", "**/credentials.json",
    "**/secrets/**", "**/*.secret",
    "**/.env", "**/.env.*",
    // The pre-redux state copies, the stores a restore moved aside, and dvc's Globus token.
    "**/.pre-rust/**", "**/.pre-rust.tmp/**", "**/.rust-*/**",
    "**/globus-tokens.json",
];

#[derive(Debug, Clone)]
pub struct Mask {
    pub globs: Vec<String>,
    /// `Err` when a glob does not compile: the mask then hides everything.
    compiled: Result<Vec<(bool, Glob)>, BadGlob>,
}

impl Mask {
    pub fn new(globs: Vec<String>) -> Self {
        let compiled = globs
            .iter()
            .filter_map(|g| {
                let (negated, pattern) = g.strip_prefix('!').map_or((false, g.as_str()), |p| (true, p));
                (!pattern.is_empty()).then(|| Glob::parse(pattern).map(|glob| (negated, glob)))
            })
            .collect();
        Self { globs, compiled }
    }

    /// The default mask plus the globs of `mask_file` (one per line, `#` comments), and the
    /// mask file itself, by name and by its path under `root`.
    pub fn load(mask_file: Option<&str>, root: &Path) -> Self {
        let mut globs: Vec<String> = DEFAULT_MASK.iter().map(|g| g.to_string()).collect();
        if let Some(mf) = mask_file.filter(|m| !m.is_empty()) {
            let p = expand_user(mf);
            match std::fs::read(&p) {
                Ok(bytes) => match String::from_utf8(bytes) {
                    Ok(text) => globs.extend(text.split(is_line_break).map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')).map(String::from)),
                    Err(_) => {
                        tracing::error!("mask file {mf} is not UTF-8; the mount hides everything");
                        return Self { globs, compiled: Err(BadGlob { glob: mf.into(), why: "mask file is not UTF-8" }) };
                    }
                },
                Err(_) => tracing::warn!("mask file {mf} unreadable; using default mask only"),
            }
            if let Some(name) = p.file_name() {
                globs.push(format!("**/{}", name.to_string_lossy()));
            }
            if let Some(rel) = realpath(&absolute(&p)).and_then(|r| r.strip_prefix(root).ok().map(Path::to_path_buf)) {
                globs.push(if rel.as_os_str().is_empty() { ".".into() } else { rel.to_string_lossy().into_owned() });
            }
        }
        let mut seen = std::collections::HashSet::new();
        globs.retain(|g| seen.insert(g.clone()));
        let mask = Self::new(globs);
        if let Err(e) = &mask.compiled {
            tracing::error!("{e}; the mount hides everything");
        }
        mask
    }

    pub fn is_bad(&self) -> bool {
        self.compiled.is_err()
    }

    /// Whether the relative path `rel` is hidden. A mask with a bad glob hides every path.
    pub fn hides(&self, rel: &str) -> bool {
        let Ok(globs) = &self.compiled else { return true };
        let mut masked = false;
        for (negated, glob) in globs {
            if glob.matches(rel) {
                masked = !negated;
            }
        }
        masked
    }
}

/// The line boundaries of Python's `str.splitlines`.
fn is_line_break(c: char) -> bool {
    matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

fn expand_user(p: &str) -> std::path::PathBuf {
    match (p.strip_prefix('~'), std::env::var_os("HOME")) {
        (Some(rest), Some(home)) if rest.is_empty() || rest.starts_with('/') => Path::new(&home).join(rest.trim_start_matches('/')),
        _ => p.into(),
    }
}

fn absolute(p: &Path) -> std::path::PathBuf {
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        std::env::current_dir().map(|d| d.join(p)).unwrap_or_else(|_| p.to_path_buf())
    }
}
