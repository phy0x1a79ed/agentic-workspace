//! Every call reflection makes through the gateway sits behind [`Gateway`], and what it reads of
//! its node behind [`Node`], so the tests fake both.

use std::future::Future;
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use awm_plugin_sdk::{config, CallOpts, Hub, HubError};
use serde_json::Value;

/// One `POST /invoke`: `<tool> <verb>` with `args`, on `peer` when set, acting for `as_`.
#[derive(Debug, Clone, PartialEq)]
pub struct Call {
    pub tool: String,
    pub verb: String,
    pub args: Value,
    pub peer: Option<String>,
    pub as_: Option<String>,
    pub timeout: Duration,
}

impl Call {
    /// A local call with no principal.
    pub fn new(tool: &str, verb: &str, args: Value, timeout: Duration) -> Self {
        Self { tool: tool.into(), verb: verb.into(), args, peer: None, as_: None, timeout }
    }

    pub fn peer(mut self, peer: Option<&str>) -> Self {
        self.peer = peer.map(str::to_string);
        self
    }

    pub fn as_(mut self, as_: Option<&str>) -> Self {
        self.as_ = as_.map(str::to_string);
        self
    }
}

/// Why a call through the gateway did not answer.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CallError {
    /// The tool did not answer: a transport fault, a 503, or a 5xx that says it is not up. A
    /// harness provider that fails this way is skipped for a while.
    #[error("{0}")]
    Down(String),
    /// The gateway or the verb refused the call.
    #[error("{0}")]
    Refused(String),
}

/// The gateway, as reflection calls it.
pub trait Gateway: Send + Sync + 'static {
    /// `GET /hub/contract/harness@1`: each provider's `{name, major, plugin, peer}`.
    fn contract_rows(&self) -> impl Future<Output = Result<Vec<Value>, String>> + Send;

    /// One call. A null result reads as `{}`.
    fn call(&self, call: Call) -> impl Future<Output = Result<Value, CallError>> + Send;
}

fn clip(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

/// Read a gateway failure the way the Python client did: 503 and a 5xx that says `not up` mean
/// the tool is down, any other status a refusal.
pub fn classify(call: &Call, e: &HubError) -> CallError {
    let status = e.status();
    let body = e.body().map(|b| b.as_str().map(str::to_string).unwrap_or_else(|| b.to_string())).unwrap_or_default();
    match (e, status) {
        (HubError::Transport { .. }, _) => CallError::Down(e.to_string()),
        (_, Some(503)) => CallError::Down(format!("HTTP 503: {}", clip(&body, 200))),
        (_, Some(s)) if s >= 500 && body.contains("not up") => CallError::Down(format!("HTTP {s}: {}", clip(&body, 200))),
        (_, s) => CallError::Refused(format!("{} {}: HTTP {}: {}", call.tool, call.verb, s.unwrap_or(0), clip(&body, 300))),
    }
}

impl Gateway for Hub {
    async fn contract_rows(&self) -> Result<Vec<Value>, String> {
        self.providers("harness", 1).await.map_err(|e| e.to_string())
    }

    async fn call(&self, call: Call) -> Result<Value, CallError> {
        let opts = CallOpts { peer: call.peer.clone(), as_: call.as_.clone(), caller_pid: None, timeout: call.timeout };
        self.call_with(&call.tool, &call.verb, call.args.clone(), &opts).await.map_err(|e| classify(&call, &e))
    }
}

/// What reflection reads of its node besides the gateway.
pub trait Node: Send + Sync + 'static {
    /// This node's fleet name.
    fn name(&self) -> String;
    /// The nodes this node holds an edge to, which hear of its retires and answer its waits.
    fn peers(&self) -> Result<Vec<String>, String>;
    /// `<workspace>/projects`, under which a cwd names a project and a scope.
    fn projects_dir(&self) -> PathBuf;
    /// Milliseconds since the epoch.
    fn now_ms(&self) -> i64;
}

/// This node: its edge list, its workspace and the system clock.
pub struct LiveNode;

impl Node for LiveNode {
    fn name(&self) -> String {
        config::node_name()
    }

    fn peers(&self) -> Result<Vec<String>, String> {
        let edges = config::edges().map_err(|e| e.to_string())?;
        Ok(crate::peers::reached_peers(&edges, &config::here()))
    }

    fn projects_dir(&self) -> PathBuf {
        config::canonical_workspace().join("projects")
    }

    fn now_ms(&self) -> i64 {
        SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_503_and_a_not_up_body_read_as_down_and_the_rest_as_refused() {
        let call = Call::new("harness-ds", "list", json!({}), Duration::from_secs(1));
        let not_up = HubError::NotUp { what: "x".into(), detail: "d".into(), body: json!({"detail": "d"}) };
        assert!(matches!(classify(&call, &not_up), CallError::Down(_)));
        let slow = HubError::Http { what: "x".into(), status: 502, detail: "d".into(), body: json!({"detail": "harness-ds is not up"}) };
        assert!(matches!(classify(&call, &slow), CallError::Down(_)));
        let gone = HubError::Transport { what: "x".into(), reason: "refused".into() };
        assert!(matches!(classify(&call, &gone), CallError::Down(_)));
        let no = HubError::Http { what: "x".into(), status: 403, detail: "no".into(), body: json!({"detail": "no"}) };
        assert_eq!(classify(&call, &no), CallError::Refused("harness-ds list: HTTP 403: {\"detail\":\"no\"}".into()));
    }
}
