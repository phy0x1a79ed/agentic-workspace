//! The session behind a caller, and a line typed into a session, through reflection
//! (`gatewayclient/sessions.py`). A service reads no harness's files.

use std::time::Duration;

use serde_json::{json, Value};

use crate::hub::{CallOpts, Hub};

/// The session `pid` belongs to, or None when it is none or reflection cannot answer. The record
/// carries `session_id` (R9), `name`, `job`, `hosting` (`background` or `terminal`), `pid`,
/// `cwd` and `harness`.
pub async fn session_of(hub: &Hub, pid: u32) -> Option<Value> {
    if pid == 0 {
        return None;
    }
    let opts = CallOpts { timeout: Duration::from_secs(10), ..CallOpts::default() };
    let out = hub.call_with("reflection", "session_of", json!({"pid": pid}), &opts).await.ok()?;
    let ok = out.get("ok").and_then(Value::as_bool) == Some(true);
    let id = out.get("session_id").is_some_and(|s| s.as_str().is_some_and(|s| !s.is_empty()));
    (ok && id).then_some(out)
}

/// Type `text` into session `job` and submit it. An error says why it was not delivered.
pub async fn tell(hub: &Hub, job: &str, text: &str) -> anyhow::Result<Value> {
    let opts = CallOpts { timeout: Duration::from_secs(30), ..CallOpts::default() };
    let out = hub.call_with("reflection", "message", json!({"job": job, "text": text}), &opts).await?;
    if out.get("ok").and_then(Value::as_bool) != Some(true) {
        let why = out.get("reason").or_else(|| out.get("error")).cloned().unwrap_or(out.clone());
        anyhow::bail!("could not type into session {job}: {why}");
    }
    Ok(out)
}
