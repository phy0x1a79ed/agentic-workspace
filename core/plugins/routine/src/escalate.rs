//! The failure chain of routines.md R47 and R48.
//!
//! 1. Start the fixing agent on the declared peer, when the routine declares one.
//! 2. Otherwise, or when that fails, post a notice to the operator's channel.
//! 3. When that fails too, log locally and stop.
//!
//! The chain never fails: a failure path that failed would hide the failure it reports.

use serde_json::{json, Value};

use crate::outbound::{Outbound, Start};

#[derive(Debug, Clone, PartialEq)]
pub struct Escalation {
    /// `agent`, `notice` or `log`.
    pub via: &'static str,
    pub session_id: String,
    pub peer: String,
    pub errors: Vec<String>,
}

impl Escalation {
    pub fn to_json(&self) -> Value {
        json!({"via": self.via, "session_id": self.session_id, "peer": self.peer, "errors": self.errors})
    }
}

pub async fn escalate(out: &dyn Outbound, start: Start, summary: &str) -> Escalation {
    let peer = start.peer.clone();
    let shown = if peer.is_empty() { "local".to_string() } else { peer.clone() };
    let start = Start { peer: shown.clone(), ..start };
    match out.start_session(start).await {
        Ok(session_id) => Escalation { via: "agent", session_id, peer, errors: vec![] },
        Err(e) => {
            let err = format!("start on {shown}: {e:#}");
            tracing::warn!("fixer start failed on {shown}: {e:#}");
            let text = format!("{summary}\nThe fixing agent could not start on {shown}: {err}");
            notice(out, text, &peer, vec![err]).await
        }
    }
}

/// Post `text` to the operator's channel, or log it when that fails.
pub async fn notice(out: &dyn Outbound, text: String, peer: &str, mut errors: Vec<String>) -> Escalation {
    match out.notify_operator(text.clone()).await {
        Ok(()) => Escalation { via: "notice", session_id: String::new(), peer: peer.into(), errors },
        Err(e) => {
            errors.push(format!("operator notice: {e:#}"));
            tracing::error!("routine failure with no agent and no notice: {text} | {errors:?}");
            Escalation { via: "log", session_id: String::new(), peer: peer.into(), errors }
        }
    }
}
