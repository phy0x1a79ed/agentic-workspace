//! A mask glob, matched whole against a relative path as the Python service matched it:
//! its `_glob_regex` translation, then `re.fullmatch`. `*` and `?` stay inside one segment,
//! a `**` segment spans zero or more segments, and a `[...]` class may match `/`.
//!
//! The Python service compiled each glob to a regex and crashed the request on a pattern
//! `re` refused. [`Glob::parse`] refuses the same patterns, and the mask then denies
//! everything.

#[derive(Debug, Clone, PartialEq, Eq)]
enum Tok {
    Lit(char),
    /// `[^/]*`
    Star,
    /// `[^/]`
    One,
    Class { negated: bool, ranges: Vec<(char, char)> },
    /// A `**` segment before the last: `(?:[^/]+/)*`.
    Segments,
    /// A last `**` segment after nothing or after another `**`: `.*`, which stops at `\n`.
    Rest,
    /// A last `**` segment after a separator: `(?:/[^/]+)*`, so `a/**` matches `a` too.
    TailSegments,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Glob(Vec<Tok>);

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("bad mask glob {glob:?}: {why}")]
pub struct BadGlob {
    pub glob: String,
    pub why: &'static str,
}

impl Glob {
    pub fn parse(pattern: &str) -> Result<Self, BadGlob> {
        let bad = |why| BadGlob { glob: pattern.to_string(), why };
        let segs: Vec<&str> = pattern.split('/').collect();
        let mut toks = Vec::new();
        for (i, seg) in segs.iter().enumerate() {
            let last = i == segs.len() - 1;
            if *seg == "**" && last {
                if toks.last() == Some(&Tok::Lit('/')) {
                    toks.pop();
                    toks.push(Tok::TailSegments);
                } else {
                    toks.push(Tok::Rest);
                }
            } else if *seg == "**" {
                toks.push(Tok::Segments);
            } else {
                segment(seg, &mut toks).map_err(bad)?;
                if !last {
                    toks.push(Tok::Lit('/'));
                }
            }
        }
        Ok(Self(toks))
    }

    pub fn matches(&self, path: &str) -> bool {
        let s: Vec<char> = path.chars().collect();
        let (n, t) = (s.len(), self.0.len());
        // ok[ti][pi]: tokens ti.. match s[pi..]. Filled from the end, so every lookup is ready.
        let mut ok = vec![vec![false; n + 1]; t + 1];
        ok[t][n] = true;
        for ti in (0..t).rev() {
            for pi in (0..=n).rev() {
                let c = s.get(pi).copied();
                let next = |k: usize| ok[ti + 1][k];
                ok[ti][pi] = match &self.0[ti] {
                    Tok::Lit(l) => c == Some(*l) && next(pi + 1),
                    Tok::One => c.is_some_and(|c| c != '/') && next(pi + 1),
                    Tok::Class { negated, ranges } => {
                        c.is_some_and(|c| ranges.iter().any(|(lo, hi)| (*lo..=*hi).contains(&c)) != *negated) && next(pi + 1)
                    }
                    Tok::Star => next(pi) || (c.is_some_and(|c| c != '/') && ok[ti][pi + 1]),
                    Tok::Rest => next(pi) || (c.is_some_and(|c| c != '\n') && ok[ti][pi + 1]),
                    Tok::Segments => {
                        let end = s[pi..].iter().position(|&c| c == '/').map(|e| pi + e);
                        next(pi) || end.is_some_and(|e| e > pi && ok[ti][e + 1])
                    }
                    Tok::TailSegments => {
                        let seg_end = |from: usize| s[from..].iter().position(|&c| c == '/').map_or(n, |e| from + e);
                        next(pi) || (c == Some('/') && seg_end(pi + 1) > pi + 1 && ok[ti][seg_end(pi + 1)])
                    }
                };
            }
        }
        ok[0][0]
    }
}

fn segment(seg: &str, toks: &mut Vec<Tok>) -> Result<(), &'static str> {
    let c: Vec<char> = seg.chars().collect();
    let mut i = 0;
    while i < c.len() {
        match c[i] {
            '*' => toks.push(Tok::Star),
            '?' => toks.push(Tok::One),
            // The class ends at the first `]` after the character that follows `[`.
            '[' if i + 2 <= c.len() && c[i + 2..].contains(&']') => {
                let end = i + 2 + c[i + 2..].iter().position(|&x| x == ']').unwrap();
                toks.push(class(&c[i + 1..end])?);
                i = end;
            }
            l => toks.push(Tok::Lit(l)),
        }
        i += 1;
    }
    Ok(())
}

/// A class body as `re` reads `[body]`, after a leading `!` became `^`. A backslash is an
/// ordinary character, because the Python translation doubled it.
fn class(body: &[char]) -> Result<Tok, &'static str> {
    let (negated, rest) = match body.first() {
        Some('!' | '^') => (true, &body[1..]),
        _ => (false, body),
    };
    if rest.is_empty() {
        return Err("unterminated character set");
    }
    let mut ranges = Vec::new();
    let mut i = 0;
    while i < rest.len() {
        let lo = rest[i];
        if rest.get(i + 1) == Some(&'-') {
            let Some(&hi) = rest.get(i + 2) else {
                ranges.extend([(lo, lo), ('-', '-')]);
                break;
            };
            if hi < lo {
                return Err("bad character range");
            }
            ranges.push((lo, hi));
            i += 3;
        } else {
            ranges.push((lo, lo));
            i += 1;
        }
    }
    Ok(Tok::Class { negated, ranges })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hit(g: &str, p: &str) -> bool {
        Glob::parse(g).unwrap().matches(p)
    }

    #[test]
    fn matches_like_full_match() {
        for (g, p, want) in [
            ("**/.git/**", ".git/config", true),
            ("**/.git/**", "a/b/.git/config", true),
            ("**/.git/**", "a/.github/x", false),
            ("**/.ssh", "home/u/.ssh", true),
            ("**/.ssh", "home/u/.ssh/id", false),
            ("**/*.pem", "etc/tls/a.pem", true),
            ("**/*.pem", "a.pem", true),
            ("**/*.pem", "a.pem.bak", false),
            ("*.pem", "etc/a.pem", false),
            ("**/.env*", "srv/.env.local", true),
            ("**/id_[re]*", "u/id_rsa", true),
            ("**/id_[!re]*", "u/id_rsa", false),
            ("a/?/c", "a/b/c", true),
            ("a/?/c", "a/bb/c", false),
            ("a/**", "a/b/c", true),
            ("a/**", "a", true),
            ("a/**/c", "a/c", true),
            ("**", "anything/at/all", true),
            ("**", "a\nb", false),
            ("a/**", "a/x\ny", true),
            ("a[/]b", "a/b", false),
            ("x[!a]y", "x/y", true),
            ("[]]", "]", true),
            ("[a-]", "-", true),
            ("[\\-z]", "a", true),
            ("[", "[", true),
            ("a[b", "a[b", true),
            ("**/**", "a/b", true),
            ("a//**", "a//b", true),
            ("a//**", "a/b", false),
        ] {
            assert_eq!(hit(g, p), want, "{g:?} on {p:?}");
        }
    }

    #[test]
    fn refuses_what_re_refuses() {
        for g in ["[z-a]", "**/[!]x", "[^]", "a/[9-0]/b"] {
            assert!(Glob::parse(g).is_err(), "{g:?}");
        }
    }
}
