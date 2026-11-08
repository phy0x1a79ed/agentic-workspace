//! Trust per session (fleet.md R103, R104, R107 item 3) and the level gate (channels.md R125). Twin of `awm.trustpolicy`, but for the gate, which only the Rust gateway runs.

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
    pub const ALL: [Level; 3] = [Level::Untrusted, Level::Cooperative, Level::Trusted];

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
    #[error("raise to {to} refused: {source}")]
    Grant { to: &'static str, source: GrantError },
    #[error("{0}")]
    Refused(String),
}

/// Verbs that act on another session. `message` carries words, never authority.
pub const STEER_VERBS: [&str; 13] = [
    "start_other", "resume_other", "stop_other", "stop", "interrupt", "model", "effort", "rename",
    "cd", "mcp", "compact", "mode", "remote_control",
];

/// What a session reads the first time it sends `message` to a session on its own node.
pub const FIRST_LOCAL_MESSAGE_HINT: &str = "This session runs on your own node. Next time, reach it with your harness's \
own messaging tool (SendMessage in Claude Code). awm carries only messages that cross an edge to another node.";

/// The effort levels a session may run at. High is the ceiling for any model.
pub const EFFORTS: [&str; 3] = ["low", "medium", "high"];
/// Effort levels that exist but lie above the ceiling.
pub const OVER_CEILING: [&str; 2] = ["xhigh", "max"];

/// Why `effort` may not be used, or `Ok` when it may. Twin of cch's `check_effort`.
pub fn check_effort(effort: &str) -> Result<(), TrustError> {
    if OVER_CEILING.contains(&effort) {
        return Err(TrustError::Refused(format!("effort {effort} is above the limit. High is the upper limit for any model.")));
    }
    if !EFFORTS.contains(&effort) {
        return Err(TrustError::Refused(format!("effort must be one of {}", EFFORTS.join(", "))));
    }
    Ok(())
}

/// A raise above the starter: the session, its alcor-signed grant and the check key.
pub struct Raise<'a> {
    pub session_id: &'a str,
    /// The node the grant must cover, or `None` for any.
    pub node: Option<&'a str>,
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
    let expect = Expect { kind: Some("trust_raise"), node: r.node, subject: Some(r.session_id), payload: Some(&payload) };
    verify_grant(r.envelope, r.key, &expect, r.now).map_err(|source| TrustError::Grant { to: want.as_str(), source })?;
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

/// The level a call runs at, from the trust the gateway stamped (R103 item 2). A level
/// that is missing or unknown is untrusted, except on a call the gateway makes itself.
pub fn stamped_level(trust: Option<&str>, gateway_own: bool) -> Level {
    match trust {
        Some(t) => t.parse().unwrap_or(Level::Untrusted),
        None if gateway_own => Level::Trusted,
        None => Level::Untrusted,
    }
}

/// One call, as the level gate sees it.
pub struct Call<'a> {
    pub tool: &'a str,
    pub verb: &'a str,
    /// The tool serves the harness contract. Only reflection drives a harness.
    pub harness: bool,
    /// The call names another session by `job`.
    pub names_other: bool,
}

/// Verbs that bring up a top-level session (channels.md R125 item 3).
pub const START_VERBS: [&str; 3] = ["start_other", "resume_other", "adopt_other"];
/// The `social` verbs a seat holds and answers its own chats with (R69). social keeps them
/// to the chats the session holds, and a mount to chats below trusted that no sibling holds.
pub const SEAT_CHAT_VERBS: [&str; 9] = ["send", "react", "fetch", "history", "download_attachments", "mount", "unmount", "mark_seat_done", "forward"];
/// The `2fa` verbs that only read. ssh arms the approver itself for fir and sockeye.
pub const TWOFA_READS: [&str; 5] = ["devices", "ping", "reachability", "status", "pending"];
const DESCRIBE: &str = "describe";

/// Why a session at `level` may not make `call`, or `None` when it may (channels.md R125,
/// R71 item 3). A trusted session is not gated. A tool refused whole refuses `describe` too.
pub fn call_refusal(level: Level, call: &Call) -> Option<String> {
    if level == Level::Trusted {
        return None;
    }
    let untrusted = level == Level::Untrusted;
    let (tool, verb) = (call.tool, call.verb);
    let partial = |allowed: bool| -> Option<String> {
        (!allowed && verb != DESCRIBE).then(|| format!("a {} session may not call {tool}.{verb}", level.as_str()))
    };
    let whole = || Some(format!("a {} session may not use the {tool:?} tool", level.as_str()));
    if call.harness {
        return whole();
    }
    match tool {
        // A seat answers its own chats and nothing more: no search, lookup, DM or bucket.
        "social" => partial(SEAT_CHAT_VERBS.contains(&verb)),
        // No cards; request_human is the one way to Tony's queue (items 5 and 6).
        "operations" => partial(verb == "request_human"),
        // The vault holds the board's cards and Tony's own notes (items 5 and 10).
        "trilium" => whole(),
        "2fa" if untrusted => whole(),
        "2fa" => partial(TWOFA_READS.contains(&verb)),
        // ssh arms Duo for fir and sockeye, the grant of item 1 that untrusted lacks.
        "ssh" if untrusted => whole(),
        "reflection" if START_VERBS.contains(&verb) => partial(false),
        "reflection" if untrusted && (verb == "message" || (call.names_other && STEER_VERBS.contains(&verb))) => {
            Some("an untrusted session cannot start, steer or message other sessions".into())
        }
        _ => None,
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

impl JournalBound {
    pub fn may_read(&self, node: &str, project: Option<&str>, scope: Option<&str>) -> bool {
        self.read_nodes.as_ref().is_none_or(|n| n.contains(node)) && self.read_scope.as_ref().is_none_or(|s| *s == scope_key(project, scope))
    }

    pub fn may_write(&self, project: Option<&str>, scope: Option<&str>) -> bool {
        self.write_scope.as_ref().is_none_or(|s| *s == scope_key(project, scope))
    }
}

fn scope_key(project: Option<&str>, scope: Option<&str>) -> String {
    format!("{}/{}", project.unwrap_or(""), scope.unwrap_or(""))
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
        let raise = || Raise { session_id: "s1", node: Some("home"), envelope: &env, key: &pk, now: 5 };
        assert_eq!(level_for_start(Level::Cooperative, Some(Level::Trusted), Some(raise())), Ok(Level::Trusted));
        let other = Raise { session_id: "s2", ..raise() };
        assert!(level_for_start(Level::Cooperative, Some(Level::Trusted), Some(other)).is_err());
    }

    #[test]
    fn steering() {
        assert!(may_steer(Level::Untrusted, "message", None).is_err());
        assert!(may_steer(Level::Cooperative, "message", Some(Level::Trusted)).is_ok());
        assert!(may_steer(Level::Cooperative, "stop", Some(Level::Trusted)).is_err());
        assert!(may_steer(Level::Cooperative, "mcp", Some(Level::Trusted)).is_err());
        assert!(may_steer(Level::Trusted, "interrupt", Some(Level::Trusted)).is_ok());
    }

    fn call<'a>(tool: &'a str, verb: &'a str) -> Call<'a> {
        Call { tool, verb, harness: false, names_other: false }
    }

    #[test]
    fn trusted_is_not_gated() {
        assert_eq!(call_refusal(Level::Trusted, &call("social", "search")), None);
        assert_eq!(call_refusal(Level::Trusted, &Call { harness: true, ..call("harness-cc", "start") }), None);
    }

    #[test]
    fn cooperative_keeps_its_chats_ssh_and_request_human() {
        let c = Level::Cooperative;
        for (tool, verb) in [("social", "send"), ("social", "history"), ("social", "mount"), ("social", "describe"), ("operations", "request_human"),
                             ("2fa", "status"), ("ssh", "connect"), ("reflection", "compact"), ("reflection", "message"),
                             ("journal", "post"), ("channel", "open"), ("browser", "tab_open")] {
            assert_eq!(call_refusal(c, &call(tool, verb)), None, "{tool}.{verb}");
        }
        for (tool, verb) in [("social", "search"), ("social", "lookup"), ("social", "open_dm"), ("social", "bucket_get"), ("social", "keepers"),
                             ("operations", "task_report"), ("operations", "approval_request"), ("trilium", "note_get"),
                             ("trilium", "describe"), ("2fa", "burst"), ("reflection", "start_other"), ("reflection", "resume_other")] {
            assert!(call_refusal(c, &call(tool, verb)).is_some(), "{tool}.{verb}");
        }
        assert!(call_refusal(c, &Call { harness: true, ..call("harness-cc", "pool_claim") }).is_some());
        assert_eq!(call_refusal(c, &Call { names_other: true, ..call("reflection", "interrupt") }), None, "reflection checks the target");
    }

    #[test]
    fn untrusted_widens_nothing() {
        let u = Level::Untrusted;
        assert_eq!(call_refusal(u, &call("social", "send")), None);
        assert_eq!(call_refusal(u, &call("operations", "request_human")), None);
        assert_eq!(call_refusal(u, &call("reflection", "compact")), None, "its own session");
        for (tool, verb) in [("2fa", "status"), ("ssh", "connect"), ("reflection", "message"), ("reflection", "start_other")] {
            assert!(call_refusal(u, &call(tool, verb)).is_some(), "{tool}.{verb}");
        }
        assert!(call_refusal(u, &Call { names_other: true, ..call("reflection", "interrupt") }).is_some());
    }

    #[test]
    fn a_missing_level_is_untrusted_unless_the_gateway_calls() {
        assert_eq!(stamped_level(None, false), Level::Untrusted);
        assert_eq!(stamped_level(None, true), Level::Trusted);
        assert_eq!(stamped_level(Some("bogus"), true), Level::Untrusted);
        assert_eq!(stamped_level(Some("cooperative"), false), Level::Cooperative);
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
