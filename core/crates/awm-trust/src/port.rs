//! The service door: the forced command of a service key (fleet.md R92 item 3, R98 item 2).
//!
//! A service key's `authorized_keys` line reads
//! `restrict,command="awm port --key awm:<holder>:service" ssh-ed25519 …`. sshd
//! puts what the caller asked for in `SSH_ORIGINAL_COMMAND`. The door takes one
//! JSON request `{"name","args"}`, answers any tool outside its set as unknown, logs every
//! call, and hands the rest to the local gateway at the service lane.

use std::io::Write;
use std::path::Path;

use serde::Deserialize;
use serde_json::{json, Value};

#[derive(Debug, Deserialize)]
pub struct DoorRequest {
    pub name: String,
    #[serde(default)]
    pub args: Value,
}

/// Parse `SSH_ORIGINAL_COMMAND`: `port <json>` or the bare JSON.
pub fn parse(original: &str) -> Result<DoorRequest, String> {
    let body = original.trim().strip_prefix("port").map(str::trim).unwrap_or(original.trim());
    serde_json::from_str(body).map_err(|e| format!("bad door request: {e}"))
}

/// The verb named by a request's args, for the log.
fn verb(req: &DoorRequest) -> &str {
    req.args.get("verb").and_then(Value::as_str).unwrap_or("")
}

/// Run one door call: check, log, invoke. Returns the JSON line to print.
pub fn serve<F>(original: &str, key: &str, allowed: &[String], log: &Path, invoke: F) -> String
where
    F: FnOnce(&DoorRequest) -> Result<Value, String>,
{
    let (outcome, reply) = match parse(original) {
        Err(e) => (("?".to_string(), String::new(), "refused"), json!({"ok": false, "error": e})),
        Ok(req) if !allowed.iter().any(|t| t == &req.name) => (
            (req.name.clone(), verb(&req).to_string(), "refused"),
            json!({"ok": false, "error": format!("Unknown tool {:?}", req.name)}),
        ),
        Ok(req) => match invoke(&req) {
            Ok(v) => ((req.name.clone(), verb(&req).to_string(), "ok"), json!({"ok": true, "result": v})),
            Err(e) => ((req.name.clone(), verb(&req).to_string(), "error"), json!({"ok": false, "error": e})),
        },
    };
    let (tool, verb, result) = outcome;
    let line = json!({"ts": chrono::Utc::now().to_rfc3339(), "key": key, "tool": tool, "verb": verb, "result": result});
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(log) {
        let _ = writeln!(f, "{line}");
    }
    reply.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuses_outside_the_set_and_logs_every_call() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("port.log");
        let allowed = vec!["2fa".to_string()];
        let out = serve(r#"port {"name":"reflection","args":{"verb":"start_other"}}"#, "awm:work:service", &allowed, &log, |_| {
            panic!("must not invoke")
        });
        assert!(out.contains(r#"Unknown tool \"reflection\""#), "{out}");
        let out = serve(r#"{"name":"2fa","args":{"verb":"arm"}}"#, "awm:work:service", &allowed, &log, |r| {
            Ok(json!({"armed": r.args["verb"]}))
        });
        assert_eq!(serde_json::from_str::<Value>(&out).unwrap()["result"]["armed"], "arm");
        let out = serve("rm -rf /", "awm:work:service", &allowed, &log, |_| panic!("must not invoke"));
        assert!(out.contains("bad door request"));
        let lines: Vec<Value> = std::fs::read_to_string(&log).unwrap().lines().map(|l| serde_json::from_str(l).unwrap()).collect();
        assert_eq!(lines.len(), 3);
        assert_eq!(lines[0]["result"], "refused");
        assert_eq!(lines[1]["tool"], "2fa");
        assert_eq!(lines[1]["verb"], "arm");
        assert_eq!(lines[1]["key"], "awm:work:service");
    }
}
