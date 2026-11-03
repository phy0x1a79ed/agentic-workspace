//! Shared types of the awm core, as `docs/redux/drafts/plugin-contract.md` defines them.

pub mod manifest;
pub mod usage;
pub mod wire;

pub use manifest::{LegacyServiceToml, Manifest, Tier, Trust, MANIFEST_FILE};
pub use usage::{Surface, UsageEvent};
pub use wire::{CallContext, Caller, Lane};

/// The UUID a tool gets before its plugin commits one: `uuid5(NAMESPACE_URL, "awm:tool:<name>")`.
pub fn legacy_tool_uuid(tool: &str) -> uuid::Uuid {
    uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_URL, format!("awm:tool:{tool}").as_bytes())
}

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
    fn legacy_uuid_matches_the_plugins_lane() {
        // redux-plugins committed this value for virtmic.
        assert_eq!(
            legacy_tool_uuid("virtmic").to_string(),
            "947d5088-6717-5c6e-b445-dde00eb50c04"
        );
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
