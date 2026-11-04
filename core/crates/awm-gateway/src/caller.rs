//! Who a local call comes from: the calling session's R9 id, trust level, mode and own scope.
//!
//! A caller names a pid: `awm mcp` its session's (`X-Awm-Session-Pid`), the CLI its own
//! (`X-Awm-Caller-Pid`). The gateway walks that pid's ancestors and asks reflection
//! `session_of` for each, which asks the harness plugins (install.md R135 item 6). The first
//! session found is the caller. A chain no harness claims is a user's shell, which passes as
//! trusted. A lookup that fails is untrusted with mode `unknown` (fleet.md R103 item 2).

use std::path::Path;

use awm_proto::Caller;
use serde_json::Value;

pub const TRUST_KEY: &str = "_caller_trust";
pub const SCOPE_KEY: &str = "_caller_scope";
/// The R9 id of an isolated session, which has no pid. Only the isolated door writes it.
pub const SESSION_KEY: &str = "_caller_session_id";
/// The calling session's pid on this node: reflection, operations and cch find the session
/// by it (contract § Caller arguments).
pub const PID_KEY: &str = "_caller_pid";
/// A session claim `{kind, job, name}` from another node. It survives only on an edge call.
pub const CLAIM_KEY: &str = "_caller_session";
/// Where `resolve_caller` records which pid of the walk the session answered for.
pub const FOUND_PID: &str = "_found_pid";
pub const FAILED_LOOKUP_TRUST: &str = "untrusted";
/// A caller that is no session: a user's shell on this node, or a plugin's service.
pub const NO_SESSION_TRUST: &str = "trusted";
pub const TRUSTED: &str = "trusted";
/// The tool and verb the gateway asks who owns a pid.
pub const LOOKUP_TOOL: &str = "reflection";
pub const LOOKUP_VERB: &str = "session_of";
/// How many ancestors the walk asks about, as `mcp_caller.MAX_ANCESTRY_HOPS`.
pub const MAX_HOPS: usize = 8;

/// What the walk from a caller's pid found.
#[derive(Debug, Clone, PartialEq)]
pub enum Lookup {
    /// reflection's `session_of` answer for the nearest session in the chain.
    Session(Value),
    /// reflection answered for every pid and no harness claims any: a user's shell. `mode` is
    /// what the harnesses said of the first pid.
    NoOwner { mode: Option<String> },
    /// reflection did not answer, or answered nonsense.
    Failed,
}

/// The parent of `pid`, from `/proc/<pid>/stat` field 4. The comm field may hold spaces
/// and parens, so the fields are counted from the last `)`.
pub fn ppid_of(pid: u32) -> Option<u32> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    stat[stat.rfind(')')? + 2..].split_whitespace().nth(1)?.parse().ok()
}

/// `<project>/<scope>` of a directory under `projects`, as `awm.config.scope_of` reads it:
/// the deepest enclosing directory that holds a `.awm/`, else the path below the project.
pub fn scope_of(cwd: &Path, projects: &Path) -> Option<String> {
    let root = projects.canonicalize().ok()?;
    let cwd = cwd.canonicalize().ok()?;
    let parts: Vec<String> =
        cwd.strip_prefix(&root).ok()?.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
    if parts.len() < 2 {
        return None;
    }
    let depth = (2..=parts.len()).rev().find(|&d| root.join(parts[..d].join("/")).join(".awm").is_dir()).unwrap_or(parts.len());
    Some(format!("{}/{}", parts[0], parts[1..depth].join("/")))
}

/// Fill `session`, `trust` and `scope` of a local caller from what the walk found, and
/// return the mode the session runs in, when a mode may gate it.
pub fn fill(caller: &mut Caller, lookup: &Lookup, projects: &Path) -> Option<String> {
    let text = |a: &Value, k: &str| a.get(k).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string);
    match lookup {
        Lookup::Session(a) => {
            caller.session = text(a, "session_id");
            caller.trust = Some(text(a, "trust").unwrap_or_else(|| FAILED_LOOKUP_TRUST.to_string()));
            caller.scope = text(a, "cwd").and_then(|cwd| scope_of(Path::new(&cwd), projects));
            // reflection says `null` for a session no restricted mode started, and leaves the
            // key out only when it could not tell.
            match a.get("mode") {
                None => Some(crate::modes::UNKNOWN.to_string()),
                Some(m) => m.as_str().map(str::to_string),
            }
        }
        Lookup::NoOwner { mode } => {
            caller.session = None;
            caller.trust = Some(NO_SESSION_TRUST.to_string());
            caller.scope = None;
            mode.clone()
        }
        Lookup::Failed => {
            caller.session = None;
            caller.trust = Some(FAILED_LOOKUP_TRUST.to_string());
            caller.scope = None;
            Some(crate::modes::UNKNOWN.to_string())
        }
    }
}

/// Write the caller's trust and scope into a call's arguments, for services that predate the
/// `caller` frame. Any value the caller sent is dropped first.
pub fn stamp(args: &mut Value, caller: &Caller, pid: Option<u32>) {
    let Some(map) = args.as_object_mut() else { return };
    map.remove(TRUST_KEY);
    map.remove(SCOPE_KEY);
    map.remove(SESSION_KEY);
    map.remove(PID_KEY);
    if caller.lane != awm_proto::Lane::Edge {
        map.remove(CLAIM_KEY);
    }
    if let Some(pid) = pid.filter(|_| caller.lane == awm_proto::Lane::Local) {
        map.insert(PID_KEY.into(), Value::from(pid));
    }
    if let Some(trust) = &caller.trust {
        map.insert(TRUST_KEY.into(), Value::String(trust.clone()));
    }
    if let Some(scope) = &caller.scope {
        map.insert(SCOPE_KEY.into(), Value::String(scope.clone()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn projects() -> (tempfile::TempDir, std::path::PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let projects = tmp.path().join("projects");
        std::fs::create_dir_all(projects.join("awm/redux-x/.awm")).unwrap();
        std::fs::create_dir_all(projects.join("awm/redux-x/core/src")).unwrap();
        std::fs::create_dir_all(projects.join("loose/a/b")).unwrap();
        (tmp, projects)
    }

    #[test]
    fn a_session_gets_its_level_mode_and_scope() {
        let (_tmp, projects) = projects();
        let cwd = projects.join("awm/redux-x/core/src");
        let answer = json!({"ok": true, "known": true, "session_id": "cc0f1e", "trust": "cooperative", "mode": null, "cwd": cwd});
        let unread = json!({"known": true, "session_id": "cc0f1e", "trust": "cooperative", "cwd": cwd});
        let mut c = Caller::default();
        let mode = fill(&mut c, &Lookup::Session(answer), &projects);
        assert_eq!((c.session.as_deref(), c.trust.as_deref(), c.scope.as_deref()), (Some("cc0f1e"), Some("cooperative"), Some("awm/redux-x")));
        assert_eq!(mode, None, "a session no restricted mode started is ungated");
        assert_eq!(fill(&mut c, &Lookup::Session(unread), &projects).as_deref(), Some(crate::modes::UNKNOWN), "an unreadable mode is gated hardest");
    }

    #[test]
    fn a_shell_no_harness_claims_is_trusted_and_ungated() {
        let (_tmp, projects) = projects();
        let mut c = Caller::default();
        assert_eq!(fill(&mut c, &Lookup::NoOwner { mode: None }, &projects), None);
        assert_eq!((c.session.as_deref(), c.trust.as_deref(), c.scope.as_deref()), (None, Some("trusted"), None));
    }

    #[test]
    fn a_failed_lookup_is_untrusted_with_mode_unknown() {
        let (_tmp, projects) = projects();
        let mut c = Caller { trust: Some("trusted".into()), ..Caller::default() };
        assert_eq!(fill(&mut c, &Lookup::Failed, &projects).as_deref(), Some("unknown"));
        assert_eq!((c.session.as_deref(), c.trust.as_deref()), (None, Some("untrusted")));
    }

    #[test]
    fn a_known_session_without_a_level_is_untrusted() {
        let (_tmp, projects) = projects();
        let mut c = Caller::default();
        fill(&mut c, &Lookup::Session(json!({"known": true, "session_id": "cc1", "cwd": "/", "mode": "worker"})), &projects);
        assert_eq!((c.trust.as_deref(), c.scope.as_deref()), (Some("untrusted"), None));
    }

    #[test]
    fn ppid_reads_this_process_parent() {
        assert_eq!(ppid_of(std::process::id()), Some(std::os::unix::process::parent_id()));
    }

    #[test]
    fn a_path_under_no_scope_falls_back_to_its_own_path() {
        let (_tmp, projects) = projects();
        assert_eq!(scope_of(&projects.join("loose/a/b"), &projects).as_deref(), Some("loose/a/b"));
        assert_eq!(scope_of(&projects.join("loose"), &projects), None);
    }

    #[test]
    fn stamp_replaces_what_the_caller_sent() {
        let caller = Caller { trust: Some("untrusted".into()), scope: Some("awm/x".into()), ..Caller::default() };
        let mut args = json!({"text": "hi", "_caller_trust": "trusted", "_caller_scope": "a/b", "_caller_pid": 1, "_caller_session": {"job": "x"}});
        stamp(&mut args, &caller, Some(42));
        assert_eq!(args, json!({"text": "hi", "_caller_trust": "untrusted", "_caller_scope": "awm/x", "_caller_pid": 42}));
        let mut args = json!({"_caller_trust": "trusted", "_caller_pid": 7});
        stamp(&mut args, &Caller::default(), None);
        assert_eq!(args, json!({}));
        let edge = Caller { lane: awm_proto::Lane::Edge, ..Caller::default() };
        let mut args = json!({"_caller_session": {"job": "x"}});
        stamp(&mut args, &edge, Some(42));
        assert_eq!(args, json!({"_caller_session": {"job": "x"}}), "a claim survives on an edge call, a pid never crosses");
    }
}
