//! The session id of `sessions.md` R9 and the session name, mirrored by `gatewayclient/ids.py`.
//!
//! A [`SessionId`] is a two-letter platform code then hex. Its canonical form is the only text
//! [`SessionId::parse`] takes. A harness's own form comes in only through
//! [`SessionId::from_harness_native`], where awm reads that harness's files.

use std::fmt;

use serde::{Deserialize, Deserializer, Serialize, Serializer};

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct BadSessionId(pub String);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Platform {
    Claude,
    DeepSeek,
}

impl Platform {
    pub const ALL: [Platform; 2] = [Platform::Claude, Platform::DeepSeek];

    pub const fn code(self) -> &'static str {
        match self {
            Platform::Claude => "cc",
            Platform::DeepSeek => "ds",
        }
    }

    pub fn from_code(code: &str) -> Result<Self, BadSessionId> {
        Self::ALL.into_iter().find(|p| p.code() == code).ok_or_else(|| {
            let known: Vec<_> = Self::ALL.iter().map(|p| p.code()).collect();
            BadSessionId(format!("unknown platform code {code:?}; known: {}", known.join(", ")))
        })
    }
}

fn is_hex(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct SessionId {
    platform: Platform,
    hex: String,
}

impl SessionId {
    /// The id whose canonical form is `text`: lowercase `cc` + 32 hex, or `ds` + hex.
    pub fn parse(text: &str) -> Result<Self, BadSessionId> {
        let bad = |why: &str| {
            BadSessionId(format!("{text:?} is not a canonical session id ({why}); the form is cc<32 hex> or ds<hex>"))
        };
        let (code, hex) = (text.get(..2).ok_or_else(|| bad("too short"))?, text.get(2..).unwrap_or(""));
        let platform = Platform::from_code(code).map_err(|_| bad("no platform code"))?;
        if !is_hex(hex) {
            return Err(bad("not lowercase hex"));
        }
        if platform == Platform::Claude && hex.len() != 32 {
            return Err(bad(&format!("{} hex digits, a cc id has 32", hex.len())));
        }
        Ok(Self { platform, hex: hex.into() })
    }

    /// The id of the harness's own session id `raw`: a Claude session UUID, or a DeepSeek hex id.
    pub fn from_harness_native(platform: Platform, raw: &str) -> Result<Self, BadSessionId> {
        let hex = raw.trim().to_lowercase().replace('-', "");
        Self::parse(&format!("{}{hex}", platform.code()))
            .map_err(|_| BadSessionId(format!("{raw:?} is not a {} harness session id", platform.code())))
    }

    /// `text` if it is a canonical id, else `None`.
    pub fn checked(text: &str) -> Option<String> {
        Self::parse(text).ok().map(|id| id.canonical())
    }

    /// The canonical form of the harness's own id `raw`, or `None` if it is not one.
    pub fn canonical_of_native(platform: Platform, raw: &str) -> Option<String> {
        Self::from_harness_native(platform, raw).ok().map(|id| id.canonical())
    }

    pub fn platform(&self) -> Platform {
        self.platform
    }

    pub fn canonical(&self) -> String {
        format!("{}{}", self.platform.code(), self.hex)
    }

    /// The form the harness itself uses: a dashed UUID for Claude Code, the hex for DeepSeek.
    pub fn harness_native(&self) -> String {
        match self.platform {
            Platform::Claude => {
                let h = &self.hex;
                format!("{}-{}-{}-{}-{}", &h[..8], &h[8..12], &h[12..16], &h[16..20], &h[20..])
            }
            Platform::DeepSeek => self.hex.clone(),
        }
    }

    /// The name of the session's `~/.claude/jobs/` dir. Only Claude Code sessions have one.
    pub fn job_dir(&self) -> Option<&str> {
        (self.platform == Platform::Claude).then(|| &self.hex[..8])
    }
}

impl fmt::Display for SessionId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}{}", self.platform.code(), self.hex)
    }
}

impl std::str::FromStr for SessionId {
    type Err = BadSessionId;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Self::parse(s)
    }
}

impl Serialize for SessionId {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.canonical())
    }
}

impl<'de> Deserialize<'de> for SessionId {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        Self::parse(&String::deserialize(d)?).map_err(serde::de::Error::custom)
    }
}

pub const NAME_MAX: usize = 80;
const RC_MAX: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct BadName(pub String);

/// A session's label. It carries no meaning: no routing, no pool state, no id.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Name(String);

impl Name {
    /// The name `text`, trimmed: 1 to 80 characters, no control characters.
    pub fn new(text: &str) -> Result<Self, BadName> {
        let t = text.trim();
        if t.is_empty() || t.chars().count() > NAME_MAX || t.chars().any(char::is_control) {
            return Err(BadName(format!("a session name is 1 to {NAME_MAX} printable characters, not {text:?}")));
        }
        Ok(Self(t.into()))
    }

    pub fn display(&self) -> &str {
        &self.0
    }

    /// The name as Claude Code's `/rc` takes it: runs outside `[A-Za-z0-9._-]` become one `-`.
    pub fn rc(&self) -> String {
        let mut out = String::new();
        for c in self.0.chars() {
            if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
                out.push(c);
            } else if !out.ends_with('-') {
                out.push('-');
            }
        }
        let out: String = out.trim_matches('-').chars().take(RC_MAX).collect();
        let out = out.trim_end_matches('-');
        if out.is_empty() { "session".into() } else { out.into() }
    }

    /// Whether `text` is this whole name, ignoring case and surrounding space.
    pub fn matches(&self, text: &str) -> bool {
        self.0.to_lowercase() == text.trim().to_lowercase()
    }
}

impl fmt::Display for Name {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl TryFrom<String> for Name {
    type Error = BadName;
    fn try_from(s: String) -> Result<Self, Self::Error> {
        Self::new(&s)
    }
}

impl From<Name> for String {
    fn from(n: Name) -> String {
        n.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    /// Rows shared with `gatewayclient/tests/test_ids.py`.
    const GOLDEN: &str = include_str!("../ids_golden.json");

    #[test]
    fn golden_vector() {
        let g: Value = serde_json::from_str(GOLDEN).unwrap();
        for row in g["ids"].as_array().unwrap() {
            let id = SessionId::parse(row["canonical"].as_str().unwrap()).unwrap();
            assert_eq!(id.harness_native(), row["harness_native"].as_str().unwrap());
            assert_eq!(id.job_dir(), row["job_dir"].as_str());
            let p = Platform::from_code(&row["canonical"].as_str().unwrap()[..2]).unwrap();
            assert_eq!(SessionId::from_harness_native(p, row["harness_native"].as_str().unwrap()).unwrap(), id);
        }
        for bad in g["bad_ids"].as_array().unwrap() {
            assert!(SessionId::parse(bad.as_str().unwrap()).is_err(), "{bad}");
        }
        for row in g["names"].as_array().unwrap() {
            let n = Name::new(row["name"].as_str().unwrap()).unwrap();
            assert_eq!(n.rc(), row["rc"].as_str().unwrap());
            for m in row["matches"].as_array().unwrap() {
                assert!(n.matches(m.as_str().unwrap()), "{m}");
            }
            for m in row["misses"].as_array().unwrap() {
                assert!(!n.matches(m.as_str().unwrap()), "{m}");
            }
        }
        for bad in g["bad_names"].as_array().unwrap() {
            assert!(Name::new(bad.as_str().unwrap()).is_err(), "{bad}");
        }
    }

    #[test]
    fn serde_uses_the_canonical_form() {
        let id = SessionId::parse("cc0f1e2d3c4b5a69788796a5b4c3d2e1f0").unwrap();
        let v = serde_json::to_value(&id).unwrap();
        assert_eq!(v, "cc0f1e2d3c4b5a69788796a5b4c3d2e1f0");
        assert_eq!(serde_json::from_value::<SessionId>(v).unwrap(), id);
        assert!(serde_json::from_value::<SessionId>("0f1e2d3c".into()).is_err());
    }
}
