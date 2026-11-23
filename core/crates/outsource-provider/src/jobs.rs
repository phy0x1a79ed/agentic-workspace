//! Pure helpers for job kinds: shell quoting, globs, a caller's material sizes and cargo discovery.

use std::path::PathBuf;

pub fn q(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

/// Shell that makes the quoted path `target` once: under a lock, unless it exists, `build` writes
/// `"$t"` (a temp beside it) and the temp is moved into place; a failed build removes the temp and exits 1.
pub fn once(target: &str, build: &str) -> String {
    format!(
        "[ -e {target} ] || ( flock 9; [ -e {target} ] || {{ t={target}.$$; rm -rf \"$t\"; {{ {build}; }} && mv \"$t\" {target} || {{ rm -rf \"$t\"; exit 1; }}; }} ) 9>{target}.lock"
    )
}

/// `cargo` from PATH, else `~/.cargo/bin`.
pub fn find_cargo() -> Option<PathBuf> {
    find_on_path("cargo", &[format!("{}/.cargo/bin", std::env::var("HOME").unwrap_or_default())])
}

pub fn find_on_path(name: &str, extra: &[String]) -> Option<PathBuf> {
    let dirs = std::env::var("PATH").unwrap_or_default();
    dirs.split(':').chain(extra.iter().map(String::as_str)).map(|d| PathBuf::from(d).join(name)).find(|p| p.exists())
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
