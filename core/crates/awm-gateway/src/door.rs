//! The client for another host's singleton door (fleet.md R89, R98 item 2).
//!
//! A singleton runs on one host, and each of its callers reaches it through that host's door,
//! never through an edge. One call is one `ssh -l <user> -p <port> <address> port <request>` with this
//! node's service key, which the host forces to `awm port`. The door reads the request from
//! `SSH_ORIGINAL_COMMAND` and prints one JSON line. A shared master connection keeps the link
//! warm (R93). `AWM_DOOR_SSH` names another program to run in place of ssh, with the same
//! arguments.

use std::process::Stdio;
use std::time::Duration;

use awm_proto::Caller;
use awm_trust::port::DoorRequest;
use serde_json::{json, Value};

use crate::peer::PeerError;

pub const DOOR_SSH_ENV: &str = "AWM_DOOR_SSH";
/// This node's service key, below `$HOME`. The host's door lists its public half.
pub const SERVICE_KEY: &str = ".ssh/id_ed25519_awm_service";
const TIMEOUT: Duration = Duration::from_secs(130);

/// Where one singleton's door listens.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DoorRoute {
    pub tool: String,
    pub host: String,
    pub address: String,
    pub port: u16,
    /// The account to log in as, from the door's `user` in edges.yml.
    pub user: Option<String>,
}

impl DoorRoute {
    fn where_(&self) -> String {
        format!("{}'s singleton door at {}:{}", self.host, self.address, self.port)
    }
}

/// The door request for one call by `caller`: the node it started from, and the calling
/// session's id, level and scope, which the host stamps as the calling node's word.
pub fn request(tool: String, envelope: Value, caller: &Caller, here: &str) -> DoorRequest {
    DoorRequest {
        name: tool,
        args: envelope,
        origin: caller.relayed_origin(here),
        session: caller.session.clone(),
        trust: caller.trust.clone(),
        scope: caller.scope.clone(),
    }
}

fn ssh_args(route: &DoorRoute, request: &DoorRequest) -> Vec<String> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    let key = format!("{}/{SERVICE_KEY}", home.trim_end_matches('/'));
    let opts = [
        "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
        "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/cm-%C", "-o", "ControlPersist=10m",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
    ];
    let mut args: Vec<String> = opts.iter().map(|s| s.to_string()).collect();
    if let Some(user) = &route.user {
        args.extend(["-l".into(), user.clone()]);
    }
    args.extend(["-i".into(), key, "-p".into(), route.port.to_string(), route.address.clone()]);
    args.push(format!("port {}", serde_json::to_string(request).expect("json")));
    args
}

/// Run `request` through `route`'s door. Returns the tool's result string, as `/invoke` does.
pub async fn call(route: &DoorRoute, request: &DoorRequest) -> Result<String, PeerError> {
    let program = std::env::var(DOOR_SSH_ENV).ok().filter(|p| !p.trim().is_empty()).unwrap_or_else(|| "ssh".into());
    let unreachable = |reason: String| PeerError::Unreachable {
        peer: route.host.clone(),
        reason: format!("{} did not answer for {}: {reason}", route.where_(), route.tool),
    };
    let child = tokio::process::Command::new(&program)
        .args(ssh_args(route, request))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| unreachable(format!("{program}: {e}")))?;
    let out = match tokio::time::timeout(TIMEOUT, child.wait_with_output()).await {
        Err(_) => return Err(unreachable(format!("no reply within {TIMEOUT:?}"))),
        Ok(Err(e)) => return Err(unreachable(e.to_string())),
        Ok(Ok(out)) => out,
    };
    let reply = String::from_utf8_lossy(&out.stdout)
        .lines()
        .find_map(|l| serde_json::from_str::<Value>(l).ok().filter(|v| v.get("ok").is_some()));
    let Some(reply) = reply else {
        let stderr = String::from_utf8_lossy(&out.stderr);
        let why = stderr.lines().rev().find(|l| !l.trim().is_empty()).map(str::to_string).unwrap_or_else(|| out.status.to_string());
        return Err(unreachable(why));
    };
    if reply["ok"] == Value::Bool(true) {
        return Ok(match reply.get("result").cloned().unwrap_or(Value::Null) {
            Value::String(s) => s,
            other => other.to_string(),
        });
    }
    let error = match reply.get("error").cloned().unwrap_or(Value::Null) {
        Value::String(s) => s,
        other => other.to_string(),
    };
    let status = if error.starts_with("Unknown tool") { 404 } else { 502 };
    Err(PeerError::Refused { peer: route.host.clone(), status, body: json!({"detail": error}).to_string() })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_request_rides_in_the_ssh_command() {
        let route = DoorRoute { tool: "vault".into(), host: "h".into(), address: "203.0.113.7".into(), port: 2201, user: None };
        let req = DoorRequest { name: "vault".into(), args: json!({"verb": "get"}), origin: None, session: None, trust: None, scope: None };
        let args = ssh_args(&route, &req);
        let n = args.len();
        assert_eq!(&args[n - 4..n - 1], ["-p", "2201", "203.0.113.7"]);
        assert_eq!(args[n - 1], r#"port {"name":"vault","args":{"verb":"get"}}"#);
        assert!(args.iter().any(|a| a.ends_with(SERVICE_KEY)));
        let parsed = awm_trust::port::parse(&args[n - 1]).unwrap();
        assert_eq!((parsed.name.as_str(), parsed.origin), ("vault", None));
        assert!(!args.iter().any(|a| a == "-l"), "no user named, so ssh logs in as the caller");
    }

    #[test]
    fn the_door_user_is_the_login_name() {
        let route = DoorRoute { tool: "vault".into(), host: "h".into(), address: "100.1.1.1".into(), port: 2201, user: Some("awm".into()) };
        let req = DoorRequest { name: "vault".into(), args: json!({}), origin: None, session: None, trust: None, scope: None };
        let args = ssh_args(&route, &req);
        let at = args.iter().position(|a| a == "-l").expect("-l");
        assert_eq!(args[at + 1], "awm");
        assert!(at < args.len() - 2, "the login precedes the address and the command");
    }
}
