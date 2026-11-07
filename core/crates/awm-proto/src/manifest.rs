//! `awm-plugin.toml`, the manifest at the top of a plugin folder.

use serde::{Deserialize, Serialize};

pub const MANIFEST_FILE: &str = "awm-plugin.toml";
pub const DEFAULT_RUN: &str = "run.sh";

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub plugin: PluginMeta,
    /// Retired by fleet.md R140: the install config places plugins. Read only to warn.
    #[serde(default, skip_serializing)]
    pub placement: Option<toml::Value>,
    #[serde(default)]
    pub service: Vec<ServiceDecl>,
    #[serde(default)]
    pub tool: Vec<ToolDecl>,
    #[serde(default)]
    pub page: Vec<PageDecl>,
    #[serde(default)]
    pub needs: Needs,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PluginMeta {
    pub name: String,
    #[serde(default = "default_version")]
    pub version: String,
    #[serde(default)]
    pub refresh: Option<String>,
}

fn default_version() -> String {
    "0.0.0".into()
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ServiceDecl {
    pub name: String,
    #[serde(default = "dot")]
    pub path: String,
    #[serde(default = "default_run")]
    pub run: String,
    /// The hook that builds the plugin's runtime, relative to `path` (plugin-contract § Runtime).
    /// None: the plugin needs no install step.
    #[serde(default)]
    pub install: Option<String>,
    #[serde(default)]
    pub profiles: Option<Vec<String>>,
    #[serde(default)]
    pub archive: Vec<toml::Value>,
}

fn dot() -> String {
    ".".into()
}
fn default_run() -> String {
    DEFAULT_RUN.into()
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Tier {
    #[default]
    On,
    Off,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ToolDecl {
    /// Unique across the fleet and the tool's permanent id (R117 item 2).
    pub name: String,
    #[serde(default = "one")]
    pub major: u32,
    #[serde(default)]
    pub service: Option<String>,
    /// Several services of the plugin that publish verbs under this one tool.
    #[serde(default)]
    pub services: Vec<String>,
    #[serde(default)]
    pub tier: Tier,
    /// Contracts this tool serves, as `"<contract>@<major>"`, such as `"harness@1"` (R117 item 4).
    #[serde(default)]
    pub implements: Vec<String>,
}

fn one() -> u32 {
    1
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PageDecl {
    pub name: String,
    /// The built bundle, relative to the plugin folder. Default `pages/<name>/dist`.
    #[serde(default)]
    pub dir: Option<String>,
    #[serde(default = "default_entry")]
    pub entry: String,
}

fn default_entry() -> String {
    "index.html".into()
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Needs {
    #[serde(default)]
    pub secrets: Vec<String>,
    #[serde(default)]
    pub network: Vec<String>,
    #[serde(default)]
    pub tool: Vec<NeedTool>,
    #[serde(default)]
    pub contract: Vec<NeedTool>,
    /// Operating systems the plugin runs on, as Rust names them (`linux`, `windows`, `macos`).
    /// Empty means any.
    #[serde(default)]
    pub platform: Vec<String>,
    /// Whether the plugin needs a GPU. Recorded, not enforced yet.
    #[serde(default)]
    pub gpu: Option<bool>,
}

/// A hard dependency (install.md R117 item 8): the loader refuses the plugin while it is
/// unmet. It lists no verbs, since a plugin may call any tool.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NeedTool {
    /// The tool's name, or for `[[needs.contract]]` the contract's name.
    pub name: String,
    #[serde(default = "one")]
    pub major: u32,
}

/// Parse `"<contract>@<major>"`.
pub fn parse_implements(raw: &str) -> Option<(String, u32)> {
    let (name, m) = raw.split_once('@')?;
    let name = name.trim();
    crate::valid_tool_name(name).then(|| Some((name.to_string(), m.trim().parse().ok()?)))?
}

impl Manifest {
    pub fn parse(text: &str) -> Result<Self, toml::de::Error> {
        toml::from_str(text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_mic_manifest() {
        let m = Manifest::parse(
            r#"
[plugin]
name = "mic"
version = "0.1.0"

[placement]
nodes = ["*"]

[[service]]
name = "mic"

[[tool]]
name = "mic"
major = 1
tier = "off"

[[page]]
name = "mic"

[needs]
secrets = []
network = []

[[needs.tool]]
name = "journal"
major = 1
"#,
        )
        .unwrap();
        assert_eq!(m.plugin.name, "mic");
        assert_eq!(m.service[0].run, "run.sh");
        assert!(m.service[0].install.is_none(), "no install key, no install step");
        assert_eq!(m.tool[0].tier, Tier::Off);
        assert!(m.page[0].dir.is_none());
        assert_eq!(m.needs.tool.len(), 1);
        assert!(m.placement.is_some());
    }

    #[test]
    fn implements() {
        assert_eq!(parse_implements("harness@1"), Some(("harness".to_string(), 1)));
        assert!(parse_implements("nope").is_none());
        assert!(parse_implements("a.b@1").is_none());
        assert!(parse_implements("harness@x").is_none());
    }

    #[test]
    fn a_uuid_is_an_unknown_key() {
        assert!(Manifest::parse("[plugin]\nname='x'\n[[tool]]\nname='x'\nuuid='6f1c2c1e-0d4b-4f3e-9a51-3b8f2d7a9c10'\n").is_err());
    }

    #[test]
    fn trust_is_gone_since_installing_trusts_a_plugin() {
        assert!(Manifest::parse("[plugin]\nname='x'\ntrust='own'\n").is_err());
    }

    #[test]
    fn a_need_has_no_verbs_and_no_optional_form() {
        for key in ["verbs=['get']", "optional=true"] {
            assert!(Manifest::parse(&format!("[plugin]\nname='x'\n[[needs.tool]]\nname='y'\n{key}\n")).is_err(), "{key}");
        }
    }

    #[test]
    fn unknown_key_is_an_error() {
        assert!(Manifest::parse("[plugin]\nname='x'\nbogus=1\n").is_err());
    }
}
