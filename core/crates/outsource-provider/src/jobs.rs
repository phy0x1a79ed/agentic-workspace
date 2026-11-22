//! Pure helpers for job kinds: shell quoting, globs, filesystem sizes and cargo discovery.

use std::path::{Path, PathBuf};

pub fn q(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

/// `cargo` from PATH, else `~/.cargo/bin`.
pub fn find_cargo() -> Option<PathBuf> {
    let mut dirs: Vec<String> =
        std::env::var("PATH").unwrap_or_default().split(':').map(String::from).collect();
    dirs.push(format!("{}/.cargo/bin", std::env::var("HOME").unwrap_or_default()));
    dirs.into_iter().map(|d| PathBuf::from(d).join("cargo")).find(|p| p.exists())
}

/// `*` and `?` stay inside a path segment, `**` crosses them.
pub fn glob_match(pat: &str, text: &str) -> bool {
    fn go(p: &[u8], s: &[u8]) -> bool {
        match p.first() {
            None => s.is_empty(),
            Some(b'*') => {
                let double = p.get(1) == Some(&b'*');
                let rest = if double { &p[2..] } else { &p[1..] };
                if double && rest.first() == Some(&b'/') && go(&rest[1..], s) {
                    return true;
                }
                (0..=s.len()).any(|i| (double || !s[..i].contains(&b'/')) && go(rest, &s[i..]))
            }
            Some(b'?') => s.first().is_some_and(|&c| c != b'/') && go(&p[1..], &s[1..]),
            Some(c) => s.first() == Some(c) && go(&p[1..], &s[1..]),
        }
    }
    go(pat.as_bytes(), text.as_bytes())
}

/// Relative paths of the regular files under `root`; symlinks are skipped.
pub fn walk_files(root: &Path) -> Vec<String> {
    fn go(root: &Path, dir: &Path, out: &mut Vec<String>) {
        let Ok(rd) = std::fs::read_dir(dir) else { return };
        for e in rd.flatten() {
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                go(root, &e.path(), out);
            } else if ft.is_file() {
                if let Ok(rel) = e.path().strip_prefix(root) {
                    out.push(rel.to_string_lossy().into_owned());
                }
            }
        }
    }
    let mut out = vec![];
    go(root, root, &mut out);
    out.sort();
    out
}

/// True size from filesystem metadata; a directory sums its files. Never reads bytes.
pub fn disk_size(path: &Path) -> u64 {
    let Ok(md) = std::fs::symlink_metadata(path) else { return 0 };
    if md.is_dir() {
        std::fs::read_dir(path)
            .map(|rd| rd.flatten().map(|e| disk_size(&e.path())).sum())
            .unwrap_or(0)
    } else if md.is_file() {
        md.len()
    } else {
        0
    }
}

pub fn remove_path(path: &Path) {
    match std::fs::symlink_metadata(path) {
        Ok(md) if md.is_dir() => drop(std::fs::remove_dir_all(path)),
        Ok(_) => drop(std::fs::remove_file(path)),
        Err(_) => {}
    }
}

pub fn is_local_route(route: &str) -> bool {
    route.is_empty() || route == "local"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn globs() {
        assert!(glob_match("*.txt", "a.txt"));
        assert!(!glob_match("*.txt", "d/a.txt"));
        assert!(glob_match("**/*.txt", "d/e/a.txt"));
        assert!(glob_match("**/*.txt", "a.txt"));
        assert!(glob_match("a?c", "abc"));
    }
}
