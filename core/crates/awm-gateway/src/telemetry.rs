//! The gateway's service and usage topics (plugin-contract § Service and usage events):
//! `awm/services` carries each service's register and disconnect, `awm/usage` each usage event
//! as it is appended. Both stay on this node.

use serde_json::{json, Value};

pub use crate::lifecycle::SERVICE;
pub const SERVICES_TOPIC: &str = "services";
pub const USAGE_TOPIC: &str = "usage";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServiceEvent {
    Up,
    Down,
}

/// Whether a peer may not subscribe to `topic` of the gateway's own topics.
pub fn is_local_only(topic: &str) -> bool {
    topic == SERVICES_TOPIC || topic == USAGE_TOPIC
}

pub fn service_payload(service: &str, plugin: &str, pid: Option<i64>, event: ServiceEvent) -> Value {
    json!({
        "service": service,
        "plugin": plugin,
        "event": if event == ServiceEvent::Up { "up" } else { "down" },
        "pid": pid,
        "ts": chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
    })
}
