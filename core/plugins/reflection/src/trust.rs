//! Trust per session in `trust.json`, and the one seam to the trust policy in awm-trust.
//!
//! The gateway refuses what a level may never call (channels.md R125). A steer is checked here,
//! because only this ledger knows the target's level. A level belongs to the session id it
//! started under.
//!
//! A session with no recorded level reads as trusted only when a harness plugin knows it: a
//! session started by hand, or before levels existed, acted with full trust. A session id that
//! neither this ledger nor any harness knows reads as untrusted (fleet.md R103 item 2). A caller
//! that is no session, a user's shell or a service, starts sessions as trusted.

use awm_trust::{Level, TrustError, FIRST_LOCAL_MESSAGE_HINT};
use serde_json::{json, Map, Value};

use crate::app::{object, text, truthy, App, Failure};
use crate::gateway::Gateway;
use crate::store::File;

pub const DEFAULT_LEVEL: &str = "trusted";
pub const UNKNOWN_SESSION_LEVEL: &str = "untrusted";

/// What a level allows. [`Policy`] asks awm-trust; a test fakes it.
pub trait TrustLib: Send + Sync {
    /// Whether a session at `actor` may run `verb` on a session at `target`, or why not.
    fn may_steer(&self, actor: &str, verb: &str, target: Option<&str>) -> Result<(), String>;
    /// The level a session started by one at `starter` runs at, or why it may not start.
    fn level_for_start(&self, starter: &str, requested: Option<&str>) -> Result<String, String>;
    /// What a session reads the first time it messages a session on its own node.
    fn first_message_hint(&self) -> String;
}

/// The fleet's trust policy, from awm-trust. reflection never passes a raise grant.
pub struct Policy;

fn level(s: &str) -> Result<Level, String> {
    s.parse().map_err(|e: TrustError| e.to_string())
}

impl TrustLib for Policy {
    fn may_steer(&self, actor: &str, verb: &str, target: Option<&str>) -> Result<(), String> {
        let target = target.map(level).transpose()?;
        awm_trust::may_steer(level(actor)?, verb, target).map_err(|e| e.to_string())
    }

    fn level_for_start(&self, starter: &str, requested: Option<&str>) -> Result<String, String> {
        let requested = requested.map(level).transpose()?;
        awm_trust::level_for_start(level(starter)?, requested, None).map(|l| l.as_str().to_string()).map_err(|e| e.to_string())
    }

    fn first_message_hint(&self) -> String {
        FIRST_LOCAL_MESSAGE_HINT.into()
    }
}

/// Whether two descriptions name one session.
pub fn same_session(a: &Value, b: &Value) -> bool {
    text(a, "session_id").is_some_and(|sa| text(b, "session_id") == Some(sa))
}

/// The level `trust.json` holds for `session_id`.
pub fn recorded_in(data: &Map<String, Value>, session_id: Option<&str>) -> Option<String> {
    let sid = session_id.filter(|s| !s.is_empty())?;
    data.get(sid).and_then(|rec| rec.get("level")).filter(|l| truthy(Some(l))).and_then(Value::as_str).map(str::to_string)
}

impl<G: Gateway> App<G> {
    pub async fn trust_data(&self) -> Map<String, Value> {
        self.store.read_object(File::Trust).await
    }

    /// The level recorded for `session_id`; None for no record.
    pub async fn recorded_level(&self, session_id: Option<&str>) -> Option<String> {
        recorded_in(&self.trust_data().await, session_id)
    }

    async fn harness_knows(&self, session_id: &str) -> bool {
        self.for_session(session_id).await.is_ok()
    }

    /// The level of `session_id`. `known` says a harness already answered for it.
    pub async fn level_of(&self, session_id: Option<&str>, known: bool) -> String {
        let Some(sid) = session_id.filter(|s| !s.is_empty()) else { return DEFAULT_LEVEL.into() };
        if let Some(level) = self.recorded_level(Some(sid)).await {
            return level;
        }
        if known || self.harness_knows(sid).await {
            DEFAULT_LEVEL.into()
        } else {
            UNKNOWN_SESSION_LEVEL.into()
        }
    }

    /// The level of a session as a harness described it.
    pub async fn level(&self, who: &Value) -> String {
        let known = who.get("known").is_none_or(|k| truthy(Some(k)));
        self.level_of(text(who, "session_id"), known).await
    }

    /// Record `level` for a new session.
    pub async fn record_trust(&self, session_id: &str, level: &str, origin: &str) {
        let _held = self.store.lock(File::Trust).await;
        let mut data = self.trust_data().await;
        let mut rec = data.get(session_id).map(object).unwrap_or_default();
        rec.insert("level".into(), json!(level));
        rec.insert("origin".into(), json!(origin));
        rec.insert("at_ms".into(), json!(self.node.now_ms()));
        data.insert(session_id.into(), Value::Object(rec));
        if let Err(e) = self.store.write(File::Trust, &Value::Object(data)).await {
            tracing::warn!("reflection: could not record the trust of {session_id}: {e}");
        }
    }

    /// Refuse when the caller may not run `verb` on another session. `actor` is None for a
    /// caller that is no session. A session acting on itself steers nobody.
    pub async fn check_steer(&self, actor: Option<&Value>, verb: &str, target: Option<&Value>) -> Result<(), Failure> {
        let Some(actor) = actor else { return Ok(()) };
        if target.is_some_and(|t| same_session(actor, t)) {
            return Ok(());
        }
        let actor_level = self.level(actor).await;
        let target_level = match target.filter(|t| text(t, "session_id").is_some()) {
            Some(t) => Some(self.level(t).await),
            None => None,
        };
        self.policy.may_steer(&actor_level, verb, target_level.as_deref()).map_err(Failure::Refused)
    }

    /// The level a new session starts at, refused above the caller's.
    pub async fn start_level(&self, actor: Option<&Value>, requested: Option<&str>) -> Result<String, Failure> {
        let starter = match actor {
            Some(a) => self.level(a).await,
            None => DEFAULT_LEVEL.into(),
        };
        self.policy.level_for_start(&starter, requested).map_err(Failure::Refused)
    }

    /// The hint for a session's first awm message to a local session, once.
    pub async fn first_message_hint(&self, actor_id: Option<&str>) -> Option<String> {
        let actor_id = actor_id.filter(|a| !a.is_empty())?;
        let _held = self.store.lock(File::Trust).await;
        let mut data = self.trust_data().await;
        let mut rec = data.get(actor_id).map(object).unwrap_or_default();
        if truthy(rec.get("hinted")) {
            return None;
        }
        rec.insert("hinted".into(), json!(true));
        data.insert(actor_id.into(), Value::Object(rec));
        self.store.write(File::Trust, &Value::Object(data)).await.ok()?;
        Some(self.policy.first_message_hint())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_policy_reads_levels_through_awm_trust() {
        let p = Policy;
        assert!(p.may_steer("cooperative", "stop", Some("trusted")).is_err());
        assert!(p.may_steer("trusted", "stop", Some("trusted")).is_ok());
        assert_eq!(p.level_for_start("trusted", Some("cooperative")).unwrap(), "cooperative");
        assert!(p.level_for_start("cooperative", Some("trusted")).unwrap_err().contains("grant"));
        assert_eq!(p.first_message_hint(), FIRST_LOCAL_MESSAGE_HINT);
    }

    #[test]
    fn a_level_is_found_by_its_session_id_alone() {
        let data = json!({"cc55": {"level": "cooperative"}, "ds66": {"level": "untrusted"}});
        let data = data.as_object().unwrap();
        assert_eq!(recorded_in(data, Some("cc55")).as_deref(), Some("cooperative"));
        assert_eq!(recorded_in(data, Some("cc77")), None);
        assert!(same_session(&json!({"session_id": "cc1"}), &json!({"session_id": "cc1"})));
        assert!(!same_session(&json!({"session_id": "cc1"}), &json!({"session_id": "cc2"})));
    }
}
