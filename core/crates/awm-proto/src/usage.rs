//! The usage event of R132: one JSON object per use of a plugin.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Surface {
    Mcp,
    Cli,
    Http,
    Plugin,
    Page,
    Code,
    Background,
}

impl Surface {
    pub const ALL: [Surface; 7] = [
        Surface::Mcp,
        Surface::Cli,
        Surface::Http,
        Surface::Plugin,
        Surface::Page,
        Surface::Code,
        Surface::Background,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Surface::Mcp => "mcp",
            Surface::Cli => "cli",
            Surface::Http => "http",
            Surface::Plugin => "plugin",
            Surface::Page => "page",
            Surface::Code => "code",
            Surface::Background => "background",
        }
    }

    pub fn parse(raw: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|s| s.as_str() == raw)
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct UsageEvent {
    #[serde(default = "v1")]
    pub v: u32,
    #[serde(default)]
    pub ts: String,
    pub plugin: String,
    pub surface: Surface,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub verb: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ok: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ms: Option<u64>,
}

fn v1() -> u32 {
    1
}

impl UsageEvent {
    pub fn new(plugin: impl Into<String>, surface: Surface) -> Self {
        Self {
            v: 1,
            ts: String::new(),
            plugin: plugin.into(),
            surface,
            tool: None,
            verb: None,
            name: None,
            ok: None,
            ms: None,
        }
    }

    /// Fill `v` and a missing `ts` with now, in RFC 3339 UTC.
    pub fn stamped(mut self) -> Self {
        self.v = 1;
        if self.ts.is_empty() {
            self.ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        }
        self
    }

    /// The day the event belongs to, `YYYY-MM-DD`, from `ts`.
    pub fn day(&self) -> &str {
        self.ts.get(..10).unwrap_or("")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_the_contract_example() {
        let raw = r#"{"v":1,"ts":"2026-11-03T22:10:00Z","plugin":"journal","surface":"mcp","tool":"journal","verb":"post","name":null,"ok":true,"ms":12}"#;
        let ev: UsageEvent = serde_json::from_str(raw).unwrap();
        assert_eq!(ev.surface, Surface::Mcp);
        assert_eq!(ev.day(), "2026-11-03");
        let frame: UsageEvent =
            serde_json::from_str(r#"{"plugin":"x","surface":"background","name":"sweep"}"#).unwrap();
        let frame = frame.stamped();
        assert_eq!(frame.ts.len(), 20);
    }
}
