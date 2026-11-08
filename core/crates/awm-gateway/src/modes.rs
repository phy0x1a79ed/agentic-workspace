//! Session modes and the verbs each may call.
//!
//! A mode not listed in [`MODES`], such as the default `worker`, is unrestricted. The one
//! listed mode is [`UNKNOWN`], which a session gets when its mode cannot be read.

/// The mode of a session whose mode could not be established. The most restricted.
pub const UNKNOWN: &str = "unknown";

/// The mode of a caller whose lookup found reflection down or silent past the wait. It is
/// gated as [`UNKNOWN`] is, and refused with its own message.
pub const UNREACHABLE: &str = "unreachable";

/// Each restricted mode with the exact `(tool, verb)` pairs it may call.
const MODES: [(&str, &[(&str, &str)]); 2] = [
    // A session whose mode could not be established can compact itself and nothing else.
    // `reflection mode`, which changes the session's permission mode, is left out on purpose.
    (UNKNOWN, &[("reflection", "compact"), ("reflection", "whoami")]),
    (UNREACHABLE, &[("reflection", "compact"), ("reflection", "whoami")]),
];

fn policy(mode: Option<&str>) -> Option<&'static [(&'static str, &'static str)]> {
    let mode = mode?;
    MODES.iter().find(|(m, _)| *m == mode).map(|(_, pairs)| *pairs)
}

/// Whether calls from a session in `mode` are gated at all.
pub fn is_restricted(mode: Option<&str>) -> bool {
    policy(mode).is_some()
}

/// Why a session in `mode` may not call `tool`'s `verb`, or `None` when it may. `describe`
/// passes for a tool the mode can use at all. A restricted mode may not name a peer.
pub fn refusal(mode: Option<&str>, tool: &str, verb: &str, peer: Option<&str>) -> Option<String> {
    let pairs = policy(mode)?;
    let label = match mode {
        Some(UNREACHABLE) => "reflection is unreachable, so this session's mode cannot be read; retry in a few seconds. This session".to_string(),
        Some(UNKNOWN) => "reflection answered but could not tell this session's mode (mode \"unknown\"); this session".to_string(),
        _ => format!("mode {:?}", mode.unwrap_or_default()),
    };
    if peer.is_some_and(|p| p != "local") {
        return Some(format!("{label} may not run a verb on a peer"));
    }
    if verb == crate::gateway::DESCRIBE {
        return (!pairs.iter().any(|(t, _)| *t == tool)).then(|| format!("{label} may not use the {tool:?} domain"));
    }
    (!pairs.contains(&(tool, verb))).then(|| format!("{label} may not call {tool}.{verb}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_may_only_compact_and_ask_who_it_is() {
        let m = Some(UNKNOWN);
        assert_eq!(refusal(m, "reflection", "compact", None), None);
        assert_eq!(refusal(m, "reflection", "whoami", None), None);
        assert_eq!(refusal(m, "reflection", "describe", None), None);
        assert!(refusal(m, "reflection", "mode", None).is_some());
        assert!(refusal(m, "journal", "post", None).is_some());
        assert!(refusal(m, "journal", "describe", None).is_some());
        assert!(refusal(m, "reflection", "compact", Some("alcor")).is_some());
    }

}
