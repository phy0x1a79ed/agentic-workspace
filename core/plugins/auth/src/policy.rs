//! The public edge's path allow-list: what the internet may reach at all.
//!
//! On the public profile (`AWM_EDGE_PROFILE=public`) a request whose path is not listed here is
//! a 404 whether or not it carries a session. The lists are constants with tests, not a per-host
//! string, so a change to the surface is a reviewed diff. The three mounts the edge forwards to
//! an upstream other than the gateway ([`vault`], [`slices`], [`tether`]) live here
//! too, because every verdict is decided against their shapes. The mesh edge's [`viewer`]
//! mount sits beside them and is not on the public surface.
//!
//! Paths are the decoded request path. Each mount's `upstream_raw_path` checks the raw bytes as
//! well, because the edge routes on the decoded path and forwards the raw one.

use std::sync::LazyLock;

use regex::Regex;

/// The answer for one path, before any identity is known.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Verdict {
    /// Not part of the public surface: 404.
    Deny,
    /// Any authenticated session.
    Open,
    /// Only the session whose user the path names.
    User,
    /// The shared knowledge base: any session that belongs to a person.
    Vault,
    /// A public slice of the vault. Allowed with no session: the token is the credential.
    Slice,
    /// The remote-assistance relay. Allowed with no session: the relay gates itself.
    Tether,
    /// `/invoke`, allowed only for a body that [`invoke_allows`] admits.
    Invoke,
}

impl Verdict {
    /// The Python enum's value.
    pub fn as_str(self) -> &'static str {
        match self {
            Verdict::Deny => "deny",
            Verdict::Open => "open",
            Verdict::User => "user",
            Verdict::Vault => "vault",
            Verdict::Slice => "slice",
            Verdict::Tether => "tether",
            Verdict::Invoke => "invoke",
        }
    }
}

/// The trilium page, as the gateway serves it at `/p/<plugin>/<page>/`.
pub const TRILIUM_PAGE: &str = "/p/trilium/trilium";

/// Slashed, with the bare form in [`OPEN_EXACT`]: a bare prefix in a `starts_with` against a
/// deny-by-default door would open every path merely starting with those characters.
pub const OPEN_PREFIXES: &[&str] = &["/p/trilium/trilium/"];
pub const OPEN_EXACT: &[&str] = &["/", TRILIUM_PAGE, "/__auth/portal", "/__auth/logout", "/__auth/whoami"];

/// The gateway's one call path. Every verb of every tool shares it.
pub const INVOKE: &str = "/invoke";
/// The most a public `/invoke` body may carry.
pub const INVOKE_BODY_CAP: usize = 16 * 1024;

/// The vault's own verbs a public `/invoke` may call. An allow-list, because most of the
/// surface is destructive and shared. Defence in depth: the trilium service gates them too.
pub const TRILIUM_OPEN_FNS: &[&str] = &["status", "snapshots", "url"];

/// Python's `re.match` with a trailing `$` matches before a final `\n` too: `\n?\z` here.
static FILES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^/s/fileviewer/projects/userdata/([^/]+)(?:/.*)?\n?\z").unwrap());

/// The static verdict for `path`, before any identity is known.
pub fn classify(path: &str) -> Verdict {
    if vault::owns(path) {
        return Verdict::Vault;
    }
    if slices::owns(path) {
        return Verdict::Slice;
    }
    if tether::owns(path) {
        return Verdict::Tether;
    }
    if tether::refused(path) {
        return Verdict::Deny;
    }
    if OPEN_EXACT.contains(&path) || OPEN_PREFIXES.iter().any(|p| path.starts_with(p)) {
        return Verdict::Open;
    }
    if path == INVOKE {
        return Verdict::Invoke;
    }
    if FILES.is_match(path) {
        return Verdict::User;
    }
    Verdict::Deny
}

/// The shared-password `operator` session, or a name that reads as a node (`peer`,
/// `peer:<node>`), as opposed to a person. The same rule as the gate's.
fn is_machine_sub(sub: &str) -> bool {
    sub == "operator" || sub == "peer" || sub.starts_with("peer:")
}

/// Whether a request acting as `sub` may reach `path`. `None` or `""` is no session.
pub fn allows(path: &str, sub: Option<&str>) -> bool {
    let verdict = classify(path);
    match verdict {
        Verdict::Open | Verdict::Slice | Verdict::Tether => return true,
        Verdict::Deny => return false,
        _ => {}
    }
    let Some(sub) = sub.filter(|s| !s.is_empty()) else { return false };
    match verdict {
        Verdict::Invoke => true,
        Verdict::Vault => !is_machine_sub(sub),
        _ => FILES.captures(path).is_some_and(|m| &m[1] == sub),
    }
}

/// Whether a public `/invoke` body is one of the trilium page's calls: exactly
/// `{"name": "trilium", "args": {"verb": <open verb>, "args": {...}}}`, `args.args` optional.
///
/// The body is read as Python's `json.loads(bytes)` reads it, encoding detection included.
pub fn invoke_allows(body: &[u8]) -> bool {
    let Some(data) = pyjson::loads(body) else { return false };
    let Some(top) = data.object() else { return false };
    if top.keys().any(|k| k != "name" && k != "args") {
        return false;
    }
    if top.get("name").and_then(pyjson::Value::str) != Some("trilium") {
        return false;
    }
    let Some(envelope) = top.get("args").and_then(pyjson::Value::object) else { return false };
    if envelope.keys().any(|k| k != "verb" && k != "args") {
        return false;
    }
    if !envelope.get("verb").and_then(pyjson::Value::str).is_some_and(|v| TRILIUM_OPEN_FNS.contains(&v)) {
        return false;
    }
    envelope.get("args").is_none_or(|a| a.object().is_some())
}

/// Whether `inner` starts with one of `entries`: an entry ending in `/` is a prefix, any other
/// is the path itself or a directory of that name.
fn refused_by(entries: &[(&str, &str)], inner: &str) -> bool {
    entries.iter().any(|(entry, _)| {
        if entry.ends_with('/') {
            inner.starts_with(entry)
        } else {
            inner == *entry || inner.strip_prefix(entry).is_some_and(|rest| rest.starts_with('/'))
        }
    })
}

/// The vault: Trilium, mounted at `/trilium/`.
pub mod vault {
    use serde_json::{json, Value};

    /// The mount, and where the application shell is served.
    pub const PREFIX: &str = "/trilium/";
    pub const SHELL: &str = PREFIX;
    /// Answered with a permanent redirect to [`SHELL`].
    pub const SHELL_BARE: &str = "/trilium";
    /// Served by the edge, never proxied: see [`manifest`].
    pub const MANIFEST: &str = "/trilium/manifest.webmanifest";
    /// Claimed by the edge, so it ends the awm session.
    pub const LOGOUT: &str = "/trilium/logout";

    /// Trilium routes deliberately not forwarded, by their path inside the mount, with the reason.
    pub const NOT_FORWARDED: &[(&str, &str)] = &[
        ("/etapi/", "unauthenticated once Trilium's own login is off"),
        ("/custom/", "dispatches user-authored backend scripts"),
        ("/mcp", "ETAPI-token only, and a name awm wants"),
        ("/share/", "public by design upstream; meaningless behind the session"),
        ("/login", "dead with Trilium's own login"),
        ("/logout", "claimed by the edge, so it ends the awm session"),
        ("/setup", "dead — the vault provisions itself"),
        ("/set-password", "dead with Trilium's own login"),
    ];

    /// Whether `path` is served by the vault rather than by the gateway.
    pub fn owns(path: &str) -> bool {
        if path == SHELL_BARE {
            return true;
        }
        if !path.starts_with(PREFIX) {
            return false;
        }
        !super::refused_by(NOT_FORWARDED, &upstream_path(path))
    }

    /// `path` with the mount taken off: `/trilium/api/tree` is Trilium's `/api/tree`.
    pub fn upstream_path(path: &str) -> String {
        match path.strip_prefix(PREFIX) {
            Some(rest) => format!("/{rest}"),
            None => path.to_string(),
        }
    }

    /// [`upstream_path`] on the raw bytes, or `None` when the mount is only there once decoded.
    pub fn upstream_raw_path(raw: &[u8]) -> Option<Vec<u8>> {
        if raw == SHELL_BARE.as_bytes() {
            return Some(b"/".to_vec());
        }
        raw.strip_prefix(PREFIX.as_bytes()).map(|rest| [b"/".as_slice(), rest].concat())
    }

    /// The PWA manifest, synthesized so an installed vault launches into the vault.
    pub fn manifest() -> Value {
        json!({
            "name": "Trilium",
            "short_name": "Trilium",
            "start_url": SHELL,
            "scope": SHELL,
            "display": "standalone",
            "icons": [{"src": "icon.png", "sizes": "512x512", "type": "image/png"}],
        })
    }
}

/// The shared browser's noVNC viewer, mounted at `/viewer/` on the mesh edge only.
pub mod viewer {
    pub const PREFIX: &str = "/viewer/";

    pub fn owns(path: &str) -> bool {
        path.starts_with(PREFIX)
    }

    /// The raw path with the mount taken off, or `None` when the mount is only there once decoded.
    pub fn upstream_raw_path(raw: &[u8]) -> Option<Vec<u8>> {
        raw.strip_prefix(PREFIX.as_bytes()).map(|rest| [b"/".as_slice(), rest].concat())
    }
}

/// A public slice of the vault: `/slice/<token>/…`, one note and its descendants.
pub mod slices {
    use std::sync::LazyLock;

    use regex::Regex;

    pub const PREFIX: &str = "/slice/";
    /// Refused on top of [`super::vault::NOT_FORWARDED`], which a slice inherits whole.
    pub const NOT_FORWARDED: &[(&str, &str)] = &[
        ("/manifest.webmanifest", "the vault synthesizes its own; a slice installs as nothing"),
        ("/robots.txt", "a link handed to one person is not a site to crawl"),
    ];
    /// The headers the edge stamps on a forwarded slice request, and strips from an inbound one.
    pub const HEADER_ROOT: &str = "X-Awm-Slice-Root";
    pub const HEADER_USER: &str = "X-Awm-Slice-User";
    pub const HEADER_WRITE: &str = "X-Awm-Slice-Write";
    /// Prefix of the cookie naming the visitor of an open token.
    pub const COOKIE_PREFIX: &str = "awm_slice_user_";
    /// The query parameter that names the visitor.
    pub const USER_PARAM: &str = "user";

    /// `re.match(r"^[A-Za-z0-9_-]{16,128}$")`, final-newline rule included.
    static TOKEN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_-]{16,128}\n?\z").unwrap());

    /// The vault's refusals and the slice's own, in that order.
    pub fn all_not_forwarded() -> Vec<(&'static str, &'static str)> {
        super::vault::NOT_FORWARDED.iter().chain(NOT_FORWARDED).copied().collect()
    }

    pub fn cookie_name(token: &str) -> String {
        format!("{COOKIE_PREFIX}{token}")
    }

    /// The token `path` names, or `None`.
    pub fn token_of(path: &str) -> Option<&str> {
        let rest = path.strip_prefix(PREFIX)?;
        let token = rest.split('/').next().unwrap_or("");
        TOKEN.is_match(token).then_some(token)
    }

    /// Whether `path` is served as a slice of the vault.
    pub fn owns(path: &str) -> bool {
        let Some(token) = token_of(path) else { return false };
        if path.len() == PREFIX.len() + token.len() {
            return true;
        }
        !super::refused_by(&all_not_forwarded(), &upstream_path(path))
    }

    /// Whether `path` names a slice but a route inside it that is not forwarded.
    pub fn refused(path: &str) -> bool {
        token_of(path).is_some() && !owns(path)
    }

    /// The slash-less mount, which the edge answers with a 308 to [`shell`].
    pub fn shell_bare(token: &str) -> String {
        format!("{PREFIX}{token}")
    }

    pub fn shell(token: &str) -> String {
        format!("{PREFIX}{token}/")
    }

    /// `path` with the mount and token taken off.
    pub fn upstream_path(path: &str) -> String {
        let Some(token) = token_of(path) else { return path.to_string() };
        let rest = &path[PREFIX.len() + token.len()..];
        if rest.is_empty() { "/".to_string() } else { rest.to_string() }
    }

    /// [`upstream_path`] on the raw bytes, or `None` when they do not carry the mount and a token.
    pub fn upstream_raw_path(raw: &[u8]) -> Option<Vec<u8>> {
        let rest = raw.strip_prefix(PREFIX.as_bytes())?;
        let (token, tail) = match rest.iter().position(|&b| b == b'/') {
            Some(i) => (&rest[..i], Some(&rest[i + 1..])),
            None => (rest, None),
        };
        // Python decodes the token as latin-1: one char per byte.
        let text: String = token.iter().map(|&b| b as char).collect();
        if !TOKEN.is_match(&text) {
            return None;
        }
        Some(match tail {
            None => b"/".to_vec(),
            Some(t) => [b"/".as_slice(), t].concat(),
        })
    }
}

/// The remote-assistance relay: `/tether` and `/room`, an allow-list of exact shapes.
pub mod tether {
    use std::sync::LazyLock;

    use regex::Regex;

    pub const PREFIX: &str = "/tether";
    /// The rooms' mount, forwarded unchanged.
    pub const ROOMS: &str = "/room";

    /// Relay routes not forwarded, with the reason.
    pub const NOT_FORWARDED: &[(&str, &str)] = &[
        ("/health", "a liveness probe for whatever supervises the process, over loopback"),
        ("/issue", "mints a slot behind the bearer, which never leaves the relay's host"),
        ("/status", "the operator's report, behind the same bearer"),
        ("/room", "opens a room behind the same bearer"),
    ];

    /// Every path this mount claims: Python's `re.match` with `$`, so `\n?\z`.
    static SHAPES: LazyLock<Vec<Regex>> = LazyLock::new(|| {
        let slot = r"[1-9][0-9]{0,2}";
        let token = r"[0-9a-f]{32}";
        let word = r"[a-z]{3,8}";
        let room = format!(r"{word}(?:-{word}){{2}}(?:(?:-{word}){{3}})?");
        let asset = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}";
        [
            r"^/tether".to_string(),
            r"^/tether/win".to_string(),
            format!(r"^/tether/bin/{asset}"),
            format!(r"^/tether/claim/{slot}"),
            format!(r"^/tether/join/{slot}/{token}"),
            format!(r"^/tether/poll/{slot}/{token}"),
            format!(r"^/tether/seat/{token}/(?:recv|send|close)"),
            format!(r"^/tether/release/{slot}/{token}"),
            format!(r"^/tether/hold/{slot}/{token}"),
            format!(r"^/room/{room}"),
            format!(r"^/room/{room}/(?:send|recv|ws|leave|file)"),
            format!(r"^/room/{room}/file/{token}"),
        ]
        .iter()
        .map(|p| Regex::new(&format!(r"{p}\n?\z")).unwrap())
        .collect()
    });

    /// Whether `path` is served by the relay rather than by the gateway.
    pub fn owns(path: &str) -> bool {
        SHAPES.iter().any(|s| s.is_match(path))
    }

    /// Whether `path` is inside the mount but not a shape this mount claims.
    pub fn refused(path: &str) -> bool {
        let inside = [PREFIX, ROOMS].iter().any(|m| path == *m || path.strip_prefix(m).is_some_and(|r| r.starts_with('/')));
        inside && !owns(path)
    }

    /// `path` with the mount taken off. The bare mount becomes `/`, the launcher.
    pub fn upstream_path(path: &str) -> String {
        if path == PREFIX {
            return "/".to_string();
        }
        if path == ROOMS || path.starts_with("/room/") {
            return path.to_string();
        }
        if path.starts_with("/tether/") {
            return path[PREFIX.len()..].to_string();
        }
        path.to_string()
    }

    /// [`upstream_path`] on the raw bytes, or `None` when the mount is only there once decoded.
    pub fn upstream_raw_path(raw: &[u8]) -> Option<Vec<u8>> {
        if raw == PREFIX.as_bytes() {
            return Some(b"/".to_vec());
        }
        if raw == ROOMS.as_bytes() || raw.starts_with(b"/room/") {
            return Some(raw.to_vec());
        }
        if !raw.starts_with(b"/tether/") {
            return None;
        }
        Some(raw[PREFIX.len()..].to_vec())
    }
}

/// Python's `json.loads` on bytes, reduced to the shape [`invoke_allows`] checks.
///
/// Accepts what Python accepts and `serde_json` refuses: `NaN` and `Infinity`, lone surrogate
/// escapes, numbers of any size, UTF-16 and UTF-32 bodies. A duplicate key keeps its last value.
mod pyjson {
    use std::collections::BTreeMap;

    /// Deeper than this is Python's `RecursionError`: refused here.
    const MAX_DEPTH: usize = 900;

    pub enum Value {
        Str(String),
        Object(BTreeMap<String, Value>),
        Other,
    }

    impl Value {
        pub fn str(&self) -> Option<&str> {
            match self {
                Value::Str(s) => Some(s),
                _ => None,
            }
        }

        pub fn object(&self) -> Option<&BTreeMap<String, Value>> {
            match self {
                Value::Object(m) => Some(m),
                _ => None,
            }
        }
    }

    /// A surrogate code point, which Python's `surrogatepass` keeps and no Rust `char` holds.
    /// Only its presence matters: it compares unequal to every name and verb, as it does in Python.
    const SURROGATE: char = '\u{fffd}';

    /// UTF-8 under Python's `surrogatepass`: `ED A0..BF xx` is a surrogate, not an error.
    fn utf8(mut b: &[u8]) -> Option<Vec<char>> {
        let mut out = Vec::with_capacity(b.len());
        loop {
            match std::str::from_utf8(b) {
                Ok(s) => {
                    out.extend(s.chars());
                    return Some(out);
                }
                Err(e) => {
                    let (good, rest) = b.split_at(e.valid_up_to());
                    out.extend(std::str::from_utf8(good).ok()?.chars());
                    match rest {
                        [0xed, 0xa0..=0xbf, 0x80..=0xbf, ..] => {
                            out.push(SURROGATE);
                            b = &rest[3..];
                        }
                        _ => return None,
                    }
                }
            }
        }
    }

    /// `json.detect_encoding`, then the decode with `surrogatepass`. `None` is a `UnicodeDecodeError`.
    fn decode(b: &[u8]) -> Option<Vec<char>> {
        let units = |big: bool, w: usize, skip: usize| -> Option<Vec<u32>> {
            let body = &b[skip..];
            if !body.len().is_multiple_of(w) {
                return None;
            }
            Some(
                body.chunks(w)
                    .map(|c| c.iter().enumerate().fold(0u32, |acc, (i, &x)| acc | (x as u32) << (8 * if big { w - 1 - i } else { i })))
                    .collect(),
            )
        };
        let utf16 = |big: bool, skip: usize| -> Option<Vec<char>> {
            let u = units(big, 2, skip)?;
            Some(char::decode_utf16(u.into_iter().map(|x| x as u16)).map(|r| r.unwrap_or(SURROGATE)).collect())
        };
        let utf32 = |big: bool, skip: usize| -> Option<Vec<char>> {
            units(big, 4, skip)?
                .into_iter()
                .map(|x| if (0xd800..0xe000).contains(&x) { Some(SURROGATE) } else { char::from_u32(x) })
                .collect()
        };
        if b.starts_with(&[0, 0, 0xfe, 0xff]) {
            return utf32(true, 4);
        }
        if b.starts_with(&[0xff, 0xfe, 0, 0]) {
            return utf32(false, 4);
        }
        if b.starts_with(&[0xfe, 0xff]) {
            return utf16(true, 2);
        }
        if b.starts_with(&[0xff, 0xfe]) {
            return utf16(false, 2);
        }
        if let Some(rest) = b.strip_prefix(&[0xef, 0xbb, 0xbf]) {
            return utf8(rest);
        }
        if b.len() >= 4 {
            if b[0] == 0 {
                return if b[1] != 0 { utf16(true, 0) } else { utf32(true, 0) };
            }
            if b[1] == 0 {
                return if b[2] != 0 || b[3] != 0 { utf16(false, 0) } else { utf32(false, 0) };
            }
        } else if b.len() == 2 {
            if b[0] == 0 {
                return utf16(true, 0);
            }
            if b[1] == 0 {
                return utf16(false, 0);
            }
        }
        utf8(b)
    }

    pub fn loads(body: &[u8]) -> Option<Value> {
        let text = decode(body)?;
        let mut p = Parser { s: &text, i: 0 };
        p.ws();
        let v = p.value(0)?;
        p.ws();
        (p.i == text.len()).then_some(v)
    }

    struct Parser<'a> {
        s: &'a [char],
        i: usize,
    }

    impl Parser<'_> {
        fn peek(&self) -> Option<char> {
            self.s.get(self.i).copied()
        }

        fn ws(&mut self) {
            while matches!(self.peek(), Some(' ' | '\t' | '\n' | '\r')) {
                self.i += 1;
            }
        }

        fn lit(&mut self, word: &str) -> bool {
            let w: Vec<char> = word.chars().collect();
            if self.s[self.i..].starts_with(&w) {
                self.i += w.len();
                true
            } else {
                false
            }
        }

        fn value(&mut self, depth: usize) -> Option<Value> {
            if depth > MAX_DEPTH {
                return None;
            }
            match self.peek()? {
                '"' => {
                    self.i += 1;
                    self.string().map(Value::Str)
                }
                '{' => {
                    self.i += 1;
                    self.object(depth)
                }
                '[' => {
                    self.i += 1;
                    self.array(depth)
                }
                'n' if self.lit("null") => Some(Value::Other),
                't' if self.lit("true") => Some(Value::Other),
                'f' if self.lit("false") => Some(Value::Other),
                'N' if self.lit("NaN") => Some(Value::Other),
                'I' if self.lit("Infinity") => Some(Value::Other),
                '-' if self.lit("-Infinity") => Some(Value::Other),
                _ => self.number().then_some(Value::Other),
            }
        }

        fn digits(&mut self) -> usize {
            let start = self.i;
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.i += 1;
            }
            self.i - start
        }

        /// `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?`, each optional part all or nothing.
        fn number(&mut self) -> bool {
            if self.peek() == Some('-') {
                self.i += 1;
            }
            match self.peek() {
                Some('0') => self.i += 1,
                Some('1'..='9') => {
                    self.digits();
                }
                _ => return false,
            }
            if self.peek() == Some('.') && self.s.get(self.i + 1).is_some_and(|c| c.is_ascii_digit()) {
                self.i += 1;
                self.digits();
            }
            if matches!(self.peek(), Some('e' | 'E')) {
                let save = self.i;
                self.i += 1;
                if matches!(self.peek(), Some('+' | '-')) {
                    self.i += 1;
                }
                if self.digits() == 0 {
                    self.i = save;
                }
            }
            true
        }

        fn hex4(&mut self) -> Option<u32> {
            let h: String = self.s.get(self.i..self.i + 4)?.iter().collect();
            if !h.chars().all(|c| c.is_ascii_hexdigit()) {
                return None;
            }
            self.i += 4;
            u32::from_str_radix(&h, 16).ok()
        }

        /// Python's strict `scanstring`. A lone surrogate escape is kept, as [`SURROGATE`].
        fn string(&mut self) -> Option<String> {
            let mut out = String::new();
            loop {
                let c = self.peek()?;
                self.i += 1;
                match c {
                    '"' => return Some(out),
                    '\\' => {
                        let e = self.peek()?;
                        self.i += 1;
                        match e {
                            '"' => out.push('"'),
                            '\\' => out.push('\\'),
                            '/' => out.push('/'),
                            'b' => out.push('\u{8}'),
                            'f' => out.push('\u{c}'),
                            'n' => out.push('\n'),
                            'r' => out.push('\r'),
                            't' => out.push('\t'),
                            'u' => {
                                let u = self.hex4()?;
                                if (0xd800..0xdc00).contains(&u) && self.s.get(self.i) == Some(&'\\') && self.s.get(self.i + 1) == Some(&'u') {
                                    let save = self.i;
                                    self.i += 2;
                                    match self.hex4() {
                                        Some(lo) if (0xdc00..0xe000).contains(&lo) => {
                                            out.push(char::from_u32(0x10000 + ((u - 0xd800) << 10) + (lo - 0xdc00))?);
                                            continue;
                                        }
                                        _ => self.i = save,
                                    }
                                }
                                out.push(char::from_u32(u).unwrap_or(SURROGATE));
                            }
                            _ => return None,
                        }
                    }
                    c if (c as u32) < 0x20 => return None,
                    c => out.push(c),
                }
            }
        }

        fn object(&mut self, depth: usize) -> Option<Value> {
            let mut map = BTreeMap::new();
            self.ws();
            if self.peek()? == '}' {
                self.i += 1;
                return Some(Value::Object(map));
            }
            loop {
                if self.peek()? != '"' {
                    return None;
                }
                self.i += 1;
                let key = self.string()?;
                self.ws();
                if self.peek()? != ':' {
                    return None;
                }
                self.i += 1;
                self.ws();
                let v = self.value(depth + 1)?;
                map.insert(key, v);
                self.ws();
                match self.peek()? {
                    ',' => {
                        self.i += 1;
                        self.ws();
                    }
                    '}' => {
                        self.i += 1;
                        return Some(Value::Object(map));
                    }
                    _ => return None,
                }
            }
        }

        fn array(&mut self, depth: usize) -> Option<Value> {
            self.ws();
            if self.peek()? == ']' {
                self.i += 1;
                return Some(Value::Other);
            }
            loop {
                self.value(depth + 1)?;
                self.ws();
                match self.peek()? {
                    ',' => {
                        self.i += 1;
                        self.ws();
                    }
                    ']' => {
                        self.i += 1;
                        return Some(Value::Other);
                    }
                    _ => return None,
                }
            }
        }
    }
}
