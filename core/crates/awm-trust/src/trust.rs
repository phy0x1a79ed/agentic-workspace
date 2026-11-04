//! Trust per session (fleet.md R103, R104, R107 item 3). Twin of `awm.trustpolicy`.

use std::collections::BTreeSet;
use std::str::FromStr;

use serde_json::{json, Value};

use crate::edges::EdgeList;
use crate::grant::{verify_grant, Expect, GrantError};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Level {
    Untrusted,
    Cooperative,
    Trusted,
}

impl Level {
    pub fn as_str(self) -> &'static str {
        match self {
            Level::Untrusted => "untrusted",
            Level::Cooperative => "cooperative",
            Level::Trusted => "trusted",
        }
    }
}

impl FromStr for Level {
    type Err = TrustError;
    fn from_str(s: &str) -> Result<Self, TrustError> {
        match s {
            "untrusted" => Ok(Level::Untrusted),
            "cooperative" => Ok(Level::Cooperative),
            "trusted" => Ok(Level::Trusted),
            other => Err(TrustError::UnknownLevel(other.into())),
        }
    }
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum TrustError {
    #[error("trust level {0:?} is not untrusted, cooperative or trusted")]
    UnknownLevel(String),
    #[error("a raise from {from} to {to} takes a grant Tony approves")]
    NoGrant { from: &'static str, to: &'static str },
    #[error("raise refused: {0}")]
    Grant(#[from] GrantError),
    #[error("{0}")]
    Refused(String),
}

/// Verbs that act on another session. `message` carries words, never authority.
pub const STEER_VERBS: [&str; 12] = [
    "start_other", "resume_other", "stop_other", "stop", "interrupt", "model", "effort", "rename",
    "cd", "compact", "mode", "remote_control",
];

/// A raise above the starter: the session, its alcor-signed grant and the check key.
pub struct Raise<'a> {
    pub session_id: &'a str,
    pub node: &'a str,
    pub envelope: &'a Value,
    pub key: &'a [u8; 32],
    pub now: i64,
}

/// The level a new session starts at: `requested` (default the starter's), never
/// above the starter without a `trust_raise` grant naming the session.
pub fn level_for_start(starter: Level, requested: Option<Level>, raise: Option<Raise>) -> Result<Level, TrustError> {
    let want = requested.unwrap_or(starter);
    if want <= starter {
        return Ok(want);
    }
    let r = raise.ok_or(TrustError::NoGrant { from: starter.as_str(), to: want.as_str() })?;
    let payload = json!({"session_id": r.session_id, "from": starter.as_str(), "to": want.as_str()});
    let expect = Expect { kind: Some("trust_raise"), node: Some(r.node), subject: Some(r.session_id), payload: Some(&payload) };
    verify_grant(r.envelope, r.key, &expect, r.now)?;
    Ok(want)
}

/// Whether a session at `actor` may run `verb` on a session at `target`.
pub fn may_steer(actor: Level, verb: &str, target: Option<Level>) -> Result<(), TrustError> {
    if actor == Level::Untrusted {
        return Err(TrustError::Refused("an untrusted session cannot start, steer or message other sessions".into()));
    }
    if verb == "message" || !STEER_VERBS.contains(&verb) {
        return Ok(());
    }
    match target {
        Some(t) if t > actor => Err(TrustError::Refused(format!(
            "a {} session cannot {verb} a {} session",
            actor.as_str(),
            t.as_str()
        ))),
        _ => Ok(()),
    }
}

/// Whether a session on `from` may act on `to`, and the level it starts there with (R104).
pub fn cross_edge(edges: &EdgeList, from: &str, to: &str, starter: Level) -> Result<Level, TrustError> {
    if !edges.reaches(from, to) {
        return Err(TrustError::Refused(format!("{from} has no edge to {to}")));
    }
    may_steer(starter, "start_other", None)?;
    Ok(starter)
}

/// What a session may read and write in the journals (R107 item 3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JournalBound {
    pub read_nodes: Option<BTreeSet<String>>,
    pub read_scope: Option<String>,
    pub write_scope: Option<String>,
}

pub fn journal_bound(edges: &EdgeList, level: Level, node: &str, project: &str, scope: &str) -> JournalBound {
    let own = format!("{project}/{scope}");
    let here: BTreeSet<String> = [node.to_string()].into();
    match level {
        Level::Trusted => {
            let mut nodes = here;
            nodes.extend(edges.reachable_from(node));
            JournalBound { read_nodes: Some(nodes), read_scope: None, write_scope: None }
        }
        Level::Cooperative => JournalBound { read_nodes: Some(here), read_scope: None, write_scope: Some(own) },
        Level::Untrusted => JournalBound { read_nodes: Some(here), read_scope: Some(own.clone()), write_scope: Some(own) },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::grant::payload_digest;
    use crate::grant::tests::{key, sign};

    #[test]
    fn start_equal_or_lower() {
        assert_eq!(level_for_start(Level::Trusted, Some(Level::Untrusted), None), Ok(Level::Untrusted));
        assert_eq!(level_for_start(Level::Cooperative, None, None), Ok(Level::Cooperative));
        assert!(matches!(level_for_start(Level::Cooperative, Some(Level::Trusted), None), Err(TrustError::NoGrant { .. })));
    }

    #[test]
    fn raise_with_grant() {
        let sk = key();
        let pk = sk.verifying_key().to_bytes();
        let payload = json!({"session_id": "s1", "from": "cooperative", "to": "trusted"});
        let env = sign(&sk, json!({"grant_id": "g", "request_id": "r", "kind": "trust_raise", "node": "home",
            "subject": "s1", "payload_sha256": payload_digest(&payload), "not_before": 0, "not_after": 10,
            "approved_by_device": "d"}));
        let raise = || Raise { session_id: "s1", node: "home", envelope: &env, key: &pk, now: 5 };
        assert_eq!(level_for_start(Level::Cooperative, Some(Level::Trusted), Some(raise())), Ok(Level::Trusted));
        let other = Raise { session_id: "s2", ..raise() };
        assert!(level_for_start(Level::Cooperative, Some(Level::Trusted), Some(other)).is_err());
    }

    #[test]
    fn steering() {
        assert!(may_steer(Level::Untrusted, "message", None).is_err());
        assert!(may_steer(Level::Cooperative, "message", Some(Level::Trusted)).is_ok());
        assert!(may_steer(Level::Cooperative, "stop", Some(Level::Trusted)).is_err());
        assert!(may_steer(Level::Trusted, "interrupt", Some(Level::Trusted)).is_ok());
    }

    #[test]
    fn edges_and_journals() {
        let e = EdgeList::parse(crate::edges::tests::FIXTURE).unwrap();
        assert_eq!(cross_edge(&e, "home", "work", Level::Cooperative), Ok(Level::Cooperative));
        assert!(cross_edge(&e, "work", "home", Level::Trusted).is_err());
        let t = journal_bound(&e, Level::Trusted, "home", "awm", "x");
        assert!(t.read_nodes.unwrap().contains("work"));
        let u = journal_bound(&e, Level::Untrusted, "home", "awm", "x");
        assert_eq!(u.read_scope.as_deref(), Some("awm/x"));
    }
}
