//! Who a local call comes from: the calling session's R9 id, trust level and own scope.
//!
//! `awm mcp` names its session by pid (`X-Awm-Session-Pid`). The gateway asks reflection
//! `session_of {pid}`, which asks the harness plugins (install.md R135 item 6). The gateway
//! itself reads no harness's files. No answer, or no session, reads as trusted with no scope.

use std::path::Path;

use awm_proto::Caller;
use serde_json::Value;

pub const TRUST_KEY: &str = "_caller_trust";
pub const SCOPE_KEY: &str = "_caller_scope";
/// The R9 id of an isolated session, which has no pid. Only the isolated door writes it.
pub const SESSION_KEY: &str = "_caller_session_id";
pub const DEFAULT_TRUST: &str = "trusted";
/// The service and function the gateway asks who owns a pid.
pub const LOOKUP_SERVICE: &str = "reflection";
pub const LOOKUP_FN: &str = "session_of";

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

/// Fill `session`, `trust` and `scope` of a local caller from reflection's `session_of`
/// answer. `None` is a failed lookup.
pub fn fill(caller: &mut Caller, answer: Option<&Value>, projects: &Path) {
    let known = answer.filter(|a| a.get("known").and_then(Value::as_bool) == Some(true));
    caller.session = known.and_then(|a| a.get("session_id")?.as_str()).filter(|s| !s.is_empty()).map(str::to_string);
    caller.trust = Some(
        known
            .and_then(|a| a.get("trust")?.as_str())
            .filter(|t| !t.is_empty())
            .unwrap_or(DEFAULT_TRUST)
            .to_string(),
    );
    caller.scope = known.and_then(|a| a.get("cwd")?.as_str()).and_then(|cwd| scope_of(Path::new(cwd), projects));
}

/// Write the caller's trust and scope into a call's arguments, for services that predate the
/// `caller` frame. Any value the caller sent is dropped first.
pub fn stamp(args: &mut Value, caller: &Caller) {
    let Some(map) = args.as_object_mut() else { return };
    map.remove(TRUST_KEY);
    map.remove(SCOPE_KEY);
    map.remove(SESSION_KEY);
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
    fn a_session_gets_its_level_and_its_scope() {
        let (_tmp, projects) = projects();
        let cwd = projects.join("awm/redux-x/core/src");
        let answer = json!({"ok": true, "known": true, "session_id": "cc0f1e", "trust": "cooperative", "cwd": cwd});
        let mut c = Caller::default();
        fill(&mut c, Some(&answer), &projects);
        assert_eq!((c.session.as_deref(), c.trust.as_deref(), c.scope.as_deref()), (Some("cc0f1e"), Some("cooperative"), Some("awm/redux-x")));
    }

    #[test]
    fn no_answer_or_no_session_reads_as_trusted() {
        let (_tmp, projects) = projects();
        let mut c = Caller::default();
        fill(&mut c, Some(&json!({"ok": true, "known": false, "mode": null})), &projects);
        assert_eq!((c.session.as_deref(), c.trust.as_deref(), c.scope.as_deref()), (None, Some("trusted"), None));
        fill(&mut c, None, &projects);
        assert_eq!((c.session.as_deref(), c.trust.as_deref()), (None, Some("trusted")));
        fill(&mut c, Some(&json!({"known": true, "session_id": "cc1", "cwd": "/"})), &projects);
        assert_eq!((c.trust.as_deref(), c.scope.as_deref()), (Some("trusted"), None));
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
        let mut args = json!({"text": "hi", "_caller_trust": "trusted", "_caller_scope": "a/b"});
        stamp(&mut args, &caller);
        assert_eq!(args, json!({"text": "hi", "_caller_trust": "untrusted", "_caller_scope": "awm/x"}));
        let mut args = json!({"_caller_trust": "trusted"});
        stamp(&mut args, &Caller::default());
        assert_eq!(args, json!({}));
    }
}
