//! Shared types of the awm core, as `docs/redux/drafts/plugin-contract.md` defines them.

pub mod manifest;
pub mod network;
pub mod usage;
pub mod wire;

pub use manifest::{LegacyServiceToml, Manifest, Tier, MANIFEST_FILE};
pub use network::NetworkEntry;
pub use usage::{Surface, UsageEvent};
pub use wire::{CallContext, Caller, Lane};

/// A tool name is letters, digits and hyphens, starting with a letter or digit.
pub fn valid_tool_name(name: &str) -> bool {
    let mut chars = name.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '-')
}

/// A verb name is letters, digits and underscores, starting with a letter or digit.
pub fn valid_verb_name(name: &str) -> bool {
    let mut chars = name.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphanumeric()) && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verb_names() {
        assert!(valid_verb_name("sign_peer_token") && valid_verb_name("list"));
        assert!(!valid_verb_name("_x") && !valid_verb_name("a-b") && !valid_verb_name(""));
    }

    #[test]
    fn tool_names() {
        assert!(valid_tool_name("harness-cc"));
        assert!(valid_tool_name("2fa"));
        assert!(!valid_tool_name("harness.cc"));
        assert!(!valid_tool_name("a_b"));
        assert!(!valid_tool_name("-x"));
        assert!(!valid_tool_name(""));
    }
}
