//! The caller of one call as the gateway stamped it ([`Frame`]), the one helper every
//! self-acting verb resolves it through ([`App::resolve_caller`]), and the one place that builds
//! the text a provider is handed to type ([`App::provider_text`]).

use awm_harness::contract::args::int_lenient;
use awm_plugin_sdk::ids;
use awm_plugin_sdk::service::{CLAIM_KEY, PID_KEY, SESSION_KEY};
use serde_json::{json, Value};

use crate::app::{text, App};
use crate::gateway::Gateway;
use crate::providers::{HarnessError, Provider};

/// What the gateway stamped on one call. No field comes from a caller's own arguments: the
/// gateway overwrites or strips each stamp before the call arrives.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Frame {
    /// `_caller_pid`: the calling session's pid on this node.
    pub pid: Option<u32>,
    /// `_caller_session_id`: an isolated session's R9 id, which has no host pid. Read before
    /// `pid`.
    pub session_id: Option<String>,
    /// `_caller_session`: a door-verified session on another node, `{session_id, node, name?}`.
    pub claim: Option<Value>,
    /// `_caller_plugin`: the calling plugin's service, from the call frame alone.
    pub plugin: Option<String>,
    /// The node a verified order's plugin speaks from, when it is not this node.
    pub plugin_node: Option<String>,
    /// The `X-Awm-As` principal, `peer:<node>` for a call from another node.
    pub as_: Option<String>,
    /// The caller as its harness described it, so one call asks the providers once.
    actor: tokio::sync::OnceCell<Option<Value>>,
}

impl Frame {
    pub fn from_call(call: &awm_plugin_sdk::Call) -> Self {
        Self::from_args(&call.args, call.as_.as_deref(), call.plugin.as_deref())
    }

    /// The frame of a call whose args carry these stamps.
    pub fn from_args(args: &Value, as_: Option<&str>, plugin: Option<&str>) -> Self {
        let pid = int_lenient(args.get(PID_KEY), 0);
        Self {
            pid: (pid > 0).then(|| u32::try_from(pid).ok()).flatten(),
            session_id: args.get(SESSION_KEY).and_then(Value::as_str).and_then(ids::SessionId::checked),
            claim: args.get(CLAIM_KEY).filter(|c| c.is_object()).cloned(),
            plugin: plugin.map(str::trim).filter(|p| !p.is_empty()).map(str::to_string),
            as_: as_.map(str::to_string),
            ..Self::default()
        }
    }

    /// A line that a plugin's service, reflection itself included, has typed.
    pub fn service(plugin: &str) -> Self {
        Self { plugin: Some(plugin.into()), ..Self::default() }
    }

    /// A verified order: alcor's operations speaking, not a local caller.
    pub fn order() -> Self {
        Self { plugin: Some("operations".into()), plugin_node: Some("alcor".into()), ..Self::default() }
    }

    /// How a provider names the caller's own session: by session id when isolated, else by pid.
    pub fn me(&self) -> Value {
        match &self.session_id {
            Some(sid) => json!({"session_id": sid}),
            None => json!({"pid": self.pid_value()}),
        }
    }

    pub fn pid_value(&self) -> Value {
        self.pid.map_or(Value::Null, |p| json!(p))
    }

    /// The pid as the log lines print it, `None` when there is none.
    pub fn pid_text(&self) -> String {
        self.pid.map_or_else(|| "None".into(), |p| p.to_string())
    }
}

/// A session's prefix. A session with no name goes by its session id.
fn sender_prefix(name: Option<&str>, session_id: Option<&str>, node: &str) -> String {
    match (name, session_id) {
        (Some(n), Some(id)) => format!("from {n}@{node} [{id}]: "),
        (Some(n), None) => format!("from {n}@{node}: "),
        (None, Some(id)) => format!("from {id}@{node}: "),
        (None, None) => format!("from shell@{node}: "),
    }
}

impl<G: Gateway> App<G> {
    /// The caller's own session and its provider: by `_caller_session_id` for an isolated
    /// session, else by `_caller_pid`. Every verb that acts on the caller's own session resolves
    /// it here.
    pub async fn resolve_caller(&self, frame: &Frame) -> Result<(Provider, Value), HarnessError> {
        self.for_caller(frame.pid, frame.session_id.as_deref()).await
    }

    /// The calling session as its harness describes it, or None for a caller that is no session.
    pub async fn actor(&self, frame: &Frame) -> Option<Value> {
        if frame.session_id.is_none() && frame.pid.is_none() {
            return None;
        }
        let resolve = || async {
            match self.resolve_caller(frame).await {
                Ok((_, who)) => text(&who, "session_id").is_some().then_some(who),
                Err(_) => frame.session_id.as_ref().map(|sid| json!({"session_id": sid, "known": false})),
            }
        };
        frame.actor.get_or_init(resolve).await.clone()
    }

    /// The text a provider is handed to type on behalf of `frame`'s caller. Every typed line
    /// passes here. A slash command stays as it is, because a prefix would turn it into chat.
    pub async fn provider_text(&self, frame: &Frame, typed: &str) -> String {
        if typed.starts_with('/') {
            return typed.to_string();
        }
        format!("{}{typed}", self.sender(frame).await)
    }

    /// Who the line is from, built from the gateway's stamps alone.
    async fn sender(&self, frame: &Frame) -> String {
        if let Some(c) = &frame.claim {
            return sender_prefix(text(c, "name"), text(c, "session_id"), text(c, "node").unwrap_or("unknown"));
        }
        let node = self.node.name();
        if let Some(plugin) = &frame.plugin {
            return format!("from {plugin}@{}: ", frame.plugin_node.as_deref().unwrap_or(&node));
        }
        match self.actor(frame).await {
            Some(who) => sender_prefix(text(&who, "name"), text(&who, "session_id"), &node),
            None => format!("from shell@{node}: "),
        }
    }

    /// [`App::provider_text`] of an optional argument: a string is built, anything else is kept.
    pub async fn provider_value(&self, frame: &Frame, v: Option<&Value>) -> Value {
        match v {
            Some(Value::String(s)) if !s.is_empty() => Value::String(self.provider_text(frame, s).await),
            Some(other) => other.clone(),
            None => Value::Null,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_frame_reads_only_the_stamps() {
        let args = json!({"_caller_pid": 4242, "_caller_session_id": "cc0e380e380e380e380e380e380e380e38", "_caller_session": {"node": "far"},
                          "_caller_trust": "cooperative", "pid": 7, "session_id": "cc99"});
        let f = Frame::from_args(&args, Some("peer:far"), Some("social"));
        assert_eq!((f.pid, f.session_id.as_deref(), f.plugin.as_deref()), (Some(4242), Some("cc0e380e380e380e380e380e380e380e38"), Some("social")));
        assert_eq!(f.claim, Some(json!({"node": "far"})));
        assert_eq!(f.me(), json!({"session_id": "cc0e380e380e380e380e380e380e380e38"}));
        let bare = Frame::from_args(&json!({"_caller_pid": 0, "_caller_session_id": "builder"}), None, Some(" "));
        assert_eq!(bare, Frame::default());
        assert_eq!((bare.me(), bare.pid_text()), (json!({"pid": null}), "None".to_string()));
    }
}
