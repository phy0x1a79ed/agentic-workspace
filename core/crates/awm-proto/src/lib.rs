//! Shared types of the awm core, as `docs/redux/drafts/plugin-contract.md` defines them.

pub mod manifest;
pub mod usage;
pub mod wire;

pub use manifest::{LegacyServiceToml, Manifest, Tier, Trust, MANIFEST_FILE};
pub use usage::{Surface, UsageEvent};
pub use wire::{CallContext, Caller, Lane};

/// A tool name is letters, digits and hyphens, starting with a letter or digit.
pub fn valid_tool_name(name: &str) -> bool {
    let mut chars = name.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '-')
}

#[cfg(test)]
mod tests {
    use super::*;

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
