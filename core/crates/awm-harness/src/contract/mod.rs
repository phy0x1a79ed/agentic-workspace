//! The platform-neutral half of harness@1: what every harness plugin parses, answers and reports
//! the same way.

pub mod args;
pub mod events;
pub mod target;

pub use events::{lost_notice, HarnessEvent, Kind, Reporter, Sender};
pub use target::{target_params, Target};

use serde_json::{json, Value};

/// A refusal, answered as a successful reply so the caller sees the reason.
pub fn refusal(error: impl Into<String>) -> Value {
    json!({"ok": false, "error": error.into()})
}

/// The refusal of a verb this platform cannot do.
pub fn unsupported(what: impl Into<String>) -> Value {
    json!({"ok": false, "error": what.into(), "unsupported": true})
}

/// The `platform` verb's answer.
pub fn platform(code: &str, name: &str, capabilities: &[&str]) -> Value {
    json!({"ok": true, "code": code, "name": name, "contract": "harness@1", "capabilities": capabilities})
}
