//! The `telemetry` tool's verbs, as one table that the service registers and the describe
//! baseline (`docs/plugin-redux/ports/baseline/telemetry.describe.json`) is checked against.

use serde_json::{json, Value};

pub const TOOL: &str = "telemetry";

pub const NODES: &str = "Each node of the swarm that reports to this roster: its role, portal link and when it last reported; its live sessions, each with its claude.ai link and task id; its count of sessions in the last 7 days; its services with whether each is up; and the tools it can use, each with when it was last checked and last used.";
pub const SERVICES: &str = "Each singleton service of the edge list, with its host node, its health there (up, down, or unknown while the host has not reported it), when its host last answered a call to it, and when any node last used it.";
pub const SEATS: &str = "Every seat on social's roster, as social last reported it: its state, node, Remote Control switch and claude.ai link.";
pub const REPORT: &str = "Take a batch of facts from one node's telemetry-node part: a hello with its whole state, or session, service, usage and tool changes. Answers resync true when the roster needs a hello first. Only that node may report for itself.";

pub const ROSTER_TOPIC: &str = "Each change of the roster: {rev, node, changed, ts}, where changed names the parts that changed (nodes, sessions, services, usage, seats).";
pub const SESSIONS_TOPIC: &str = "Each session that goes live, goes, is renamed or changes state: {event: live | gone | renamed | state, session_id, task_id?, node, ts, name?, state?, needs?, retired?}. state is working, blocked or done, needs is why while blocked, and retired marks a gone that was a retire.";

pub fn params(verb: &str) -> Value {
    match verb {
        "nodes" => json!([{"name": "node", "type": "string", "required": false,
                           "description": "Only this node."}]),
        "report" => json!([
            {"name": "swarm", "type": "string", "required": true, "description": "The swarm the node opted in to, from its install config's [telemetry] swarm."},
            {"name": "node", "type": "string", "required": true, "description": "The reporting node."},
            {"name": "items", "type": "array", "required": true, "description": "The facts, each {type: hello | session | service | usage | tools, ...}."},
        ]),
        _ => json!([]),
    }
}

/// The tool's describe, as the baseline file holds it: verbs sorted by name.
pub fn describe() -> Value {
    let mut verbs: Vec<(&str, &str)> = vec![("nodes", NODES), ("report", REPORT), ("seats", SEATS), ("services", SERVICES)];
    verbs.sort();
    json!({
        "plugin": TOOL,
        "tool": TOOL,
        "verbs": verbs.iter().map(|(v, d)| json!({"description": d, "params": params(v), "verb": v})).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn the_describe_matches_its_baseline() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../docs/plugin-redux/ports/baseline/telemetry.describe.json");
        let text = std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{path}: {e}"));
        let baseline: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(super::describe(), baseline, "update the baseline with the verb change");
    }
}
