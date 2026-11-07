//! The one parser of the session id of `sessions.md` R9 (`gatewayclient/ids.py`): a two-letter
//! platform code, then the platform's own id as hex of any length.
//!
//! A Claude Code session is `cc` plus its UUID without dashes. A DeepSeek harness session is
//! `ds` plus the hex of the UUID in its `session-<uuid>` id.

pub const PLATFORM_CLAUDE: &str = "cc";
pub const PLATFORM_DEEPSEEK: &str = "ds";
pub const PLATFORMS: [&str; 2] = [PLATFORM_CLAUDE, PLATFORM_DEEPSEEK];

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct BadSessionId(pub String);

fn is_hex(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn is_uuid(s: &str) -> bool {
    let groups: Vec<&str> = s.split('-').collect();
    match groups.len() {
        1 => s.len() == 32 && is_hex(s),
        5 => groups.iter().zip([8, 4, 4, 4, 12]).all(|(g, n)| g.len() == n && is_hex(g)),
        _ => false,
    }
}

/// The R9 id of the platform's own session id `raw`.
pub fn r9(platform: &str, raw: &str) -> Result<String, BadSessionId> {
    if !PLATFORMS.contains(&platform) {
        return Err(BadSessionId(format!("unknown platform code {platform:?}; known: {}", PLATFORMS.join(", "))));
    }
    let digits = raw.trim().to_lowercase().replace('-', "");
    if !is_hex(&digits) {
        return Err(BadSessionId(format!("{raw:?} is not a hex session id")));
    }
    Ok(format!("{platform}{digits}"))
}

/// `(platform, raw)` for an R9 id. `raw` is the platform's own form: a dashed UUID for Claude Code.
pub fn parse_r9(session_id: &str) -> Result<(String, String), BadSessionId> {
    let text = session_id.trim().to_lowercase();
    let bad = || BadSessionId(format!("{session_id:?} is not an R9 session id"));
    let (platform, digits) = (text.get(..2).ok_or_else(bad)?, text.get(2..).ok_or_else(bad)?);
    if !PLATFORMS.contains(&platform) || !is_hex(digits) {
        return Err(bad());
    }
    if platform == PLATFORM_CLAUDE && digits.len() == 32 {
        let d = digits;
        return Ok((platform.into(), format!("{}-{}-{}-{}-{}", &d[..8], &d[8..12], &d[12..16], &d[16..20], &d[20..])));
    }
    Ok((platform.into(), digits.into()))
}

/// The R9 id for `reference`: an R9 id as is, or a bare Claude session UUID.
pub fn normalise(reference: &str) -> Result<String, BadSessionId> {
    let text = reference.trim().to_lowercase();
    if is_uuid(&text) {
        return r9(PLATFORM_CLAUDE, &text);
    }
    parse_r9(&text)?;
    Ok(text)
}

/// [`normalise`] that answers None for anything that is not an id.
pub fn to_r9(reference: Option<&str>) -> Option<String> {
    normalise(reference?).ok()
}

/// The dashed Claude session UUID behind an R9 Claude id.
pub fn claude_uuid(session_id: &str) -> Option<String> {
    match parse_r9(session_id) {
        Ok((p, raw)) if p == PLATFORM_CLAUDE && raw.contains('-') => Some(raw),
        _ => None,
    }
}

pub fn is_r9(reference: &str) -> bool {
    parse_r9(reference).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_round_trip() {
        let uuid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0";
        let id = normalise(uuid).unwrap();
        assert_eq!(id, "cc0f1e2d3c4b5a69788796a5b4c3d2e1f0");
        assert_eq!(normalise(&id.to_uppercase()).unwrap(), id);
        assert_eq!(claude_uuid(&id).as_deref(), Some(uuid));
        assert_eq!(parse_r9("dsabc1").unwrap(), ("ds".into(), "abc1".into()));
        assert!(to_r9(Some("job-1")).is_none() && to_r9(None).is_none());
        assert!(r9("zz", "ab").is_err() && !is_r9("cc") && !is_r9("ccxyz"));
    }
}
