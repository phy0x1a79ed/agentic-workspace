//! A plugin's `state.toml`: the state its port takes over.
//!
//! Every path expands `${VAR}` and `${VAR:-default}` (defaults nest), and a relative path
//! resolves against `AWM_DIR`. `size_only`, `extra` and `transient` paths may hold glob
//! patterns.

use std::path::{Path, PathBuf};

use serde::Deserialize;

pub const STATE_TOML: &str = "state.toml";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Additive {
    pub path: PathBuf,
    pub table: Option<String>,
    pub column: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Transient {
    pub path: PathBuf,
    pub table: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Extra {
    pub name: String,
    pub path: PathBuf,
}

/// What a port's first start copies, and what its check may see change.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decl {
    pub dirs: Vec<PathBuf>,
    pub additive: Vec<Additive>,
    pub transient: Vec<Transient>,
    pub size_only: Vec<PathBuf>,
    pub extra: Vec<Extra>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Raw {
    dirs: Option<Vec<String>>,
    #[serde(default)]
    additive: Vec<RawAdditive>,
    #[serde(default)]
    transient: Vec<RawTransient>,
    #[serde(default)]
    size_only: Vec<RawPath>,
    #[serde(default)]
    extra: Vec<RawExtra>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawAdditive {
    path: String,
    table: Option<String>,
    column: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawTransient {
    path: String,
    table: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawPath {
    path: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawExtra {
    name: String,
    path: String,
}

impl Decl {
    /// The declaration in `<plugin_dir>/state.toml`. With no file, the plugin's state is
    /// `default_dir`, copied whole.
    pub fn load(plugin_dir: &Path, default_dir: &Path, awm_dir: &Path) -> anyhow::Result<Decl> {
        let file = plugin_dir.join(STATE_TOML);
        match std::fs::read_to_string(&file) {
            Ok(text) => Decl::parse(&text, default_dir, awm_dir, |k| std::env::var(k).ok()).map_err(|e| anyhow::anyhow!("{}: {e}", file.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Decl::whole(default_dir)),
            Err(e) => Err(anyhow::anyhow!("{}: {e}", file.display())),
        }
    }

    pub fn whole(dir: &Path) -> Decl {
        Decl { dirs: vec![dir.to_path_buf()], additive: vec![], transient: vec![], size_only: vec![], extra: vec![] }
    }

    /// Parse `text`. `env` answers `${VAR}`; `AWM_DIR` falls back to `awm_dir`.
    pub fn parse(text: &str, default_dir: &Path, awm_dir: &Path, env: impl Fn(&str) -> Option<String>) -> anyhow::Result<Decl> {
        let raw: Raw = toml::from_str(text)?;
        let lookup = |k: &str| env(k).or_else(|| (k == "AWM_DIR").then(|| awm_dir.display().to_string()));
        let path = |p: &str| -> anyhow::Result<PathBuf> {
            let expanded = PathBuf::from(expand(p, &lookup)?);
            Ok(if expanded.is_absolute() { expanded } else { awm_dir.join(expanded) })
        };
        let dirs = match raw.dirs {
            Some(d) => d.iter().map(|p| path(p)).collect::<anyhow::Result<_>>()?,
            None => vec![default_dir.to_path_buf()],
        };
        let mut names = std::collections::BTreeSet::new();
        for e in &raw.extra {
            if e.name.is_empty() || e.name.contains('/') || e.name.starts_with('.') || !names.insert(&e.name) {
                anyhow::bail!("extra name {:?} must be unique, plain and not hidden", e.name);
            }
        }
        Ok(Decl {
            dirs,
            additive: raw.additive.iter().map(|a| Ok(Additive { path: path(&a.path)?, table: a.table.clone(), column: a.column.clone() })).collect::<anyhow::Result<_>>()?,
            transient: raw.transient.iter().map(|t| Ok(Transient { path: path(&t.path)?, table: t.table.clone() })).collect::<anyhow::Result<_>>()?,
            size_only: raw.size_only.iter().map(|s| path(&s.path)).collect::<anyhow::Result<_>>()?,
            extra: raw.extra.iter().map(|e| Ok(Extra { name: e.name.clone(), path: path(&e.path)? })).collect::<anyhow::Result<_>>()?,
        })
    }

    /// Whether `path` is a declared transient file, or lies under one.
    pub fn is_transient_file(&self, path: &Path) -> bool {
        self.transient.iter().any(|t| t.table.is_none() && matches(&t.path, path))
    }

    pub fn is_transient_table(&self, db: &Path, table: &str) -> bool {
        self.transient.iter().any(|t| t.table.as_deref() == Some(table) && matches(&t.path, db))
    }

    pub fn is_size_only(&self, path: &Path) -> bool {
        self.size_only.iter().any(|s| matches(s, path))
    }
}

/// Whether `path` equals `pattern`, matches it as a glob, or lies under such a path.
pub(crate) fn matches(pattern: &Path, path: &Path) -> bool {
    let pat = pattern.to_string_lossy();
    let opts = glob::MatchOptions { require_literal_separator: true, require_literal_leading_dot: false, case_sensitive: true };
    let glob = glob::Pattern::new(&pat).ok();
    path.ancestors().any(|a| a == pattern || glob.as_ref().is_some_and(|g| g.matches_path_with(a, opts)))
}

fn expand(text: &str, env: &dyn Fn(&str) -> Option<String>) -> anyhow::Result<String> {
    let mut out = String::new();
    let mut rest = text;
    while let Some(i) = rest.find("${") {
        out.push_str(&rest[..i]);
        let body = &rest[i + 2..];
        let end = closing(body).ok_or_else(|| anyhow::anyhow!("unclosed ${{ in {text:?}"))?;
        let inner = &body[..end];
        let (name, default) = match inner.find(":-") {
            Some(j) => (&inner[..j], Some(&inner[j + 2..])),
            None => (inner, None),
        };
        if name.is_empty() || !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
            anyhow::bail!("bad variable name {name:?} in {text:?}");
        }
        match (env(name).filter(|v| !v.is_empty()), default) {
            (Some(v), _) => out.push_str(&v),
            (None, Some(d)) => out.push_str(&expand(d, env)?),
            (None, None) => anyhow::bail!("{name} is not set, and {text:?} gives no default"),
        }
        rest = &body[end + 1..];
    }
    out.push_str(rest);
    Ok(out)
}

/// The index of the `}` that closes a `${` whose body starts `body`.
fn closing(body: &str) -> Option<usize> {
    let mut depth = 0usize;
    let bytes = body.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'$' if bytes.get(i + 1) == Some(&b'{') => {
                depth += 1;
                i += 1;
            }
            b'}' if depth == 0 => return Some(i),
            b'}' => depth -= 1,
            _ => {}
        }
        i += 1;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(k: &str) -> Option<String> {
        match k {
            "HOME" => Some("/home/u".into()),
            "EMPTY" => Some(String::new()),
            _ => None,
        }
    }

    #[test]
    fn expands_nested_defaults() {
        assert_eq!(expand("${OPS:-${HOME}/.awm/operations}", &env).unwrap(), "/home/u/.awm/operations");
        assert_eq!(expand("${EMPTY:-x}/y", &env).unwrap(), "x/y");
        assert!(expand("${NOPE}", &env).is_err());
        assert!(expand("${HOME", &env).is_err());
    }

    #[test]
    fn parses_a_declaration() {
        let text = r#"
dirs = ["services/routine", "${AWM_DIR}/land"]
[[additive]]
path = "services/routine/routine.db"
table = "timer_slots"
[[transient]]
path = "land/lease.json"
[[transient]]
path = "services/routine/routine.db"
table = "cache"
[[size_only]]
path = "/srv/big"
[[extra]]
name = "units"
path = "${HOME}/.config/systemd/user/awm-routine-*"
"#;
        let d = Decl::parse(text, Path::new("/a/services/x"), Path::new("/a"), env).unwrap();
        assert_eq!(d.dirs, [PathBuf::from("/a/services/routine"), PathBuf::from("/a/land")]);
        assert!(d.is_transient_file(Path::new("/a/land/lease.json")));
        assert!(!d.is_transient_file(Path::new("/a/land/tickets")));
        assert!(d.is_transient_table(Path::new("/a/services/routine/routine.db"), "cache"));
        assert!(d.is_size_only(Path::new("/srv/big/blob")));
        assert_eq!(d.extra[0].path, PathBuf::from("/home/u/.config/systemd/user/awm-routine-*"));
        assert!(matches(&d.extra[0].path, Path::new("/home/u/.config/systemd/user/awm-routine-x.timer")));
    }

    #[test]
    fn no_dirs_means_the_default_and_bad_extras_refuse() {
        let d = Decl::parse("", Path::new("/a/services/x"), Path::new("/a"), env).unwrap();
        assert_eq!(d.dirs, [PathBuf::from("/a/services/x")]);
        assert!(Decl::parse("dirs = []", Path::new("/x"), Path::new("/a"), env).unwrap().dirs.is_empty());
        assert!(Decl::parse("[[extra]]\nname = \"a/b\"\npath = \"/x\"", Path::new("/x"), Path::new("/a"), env).is_err());
        assert!(Decl::parse("bogus = 1", Path::new("/x"), Path::new("/a"), env).is_err());
    }
}
