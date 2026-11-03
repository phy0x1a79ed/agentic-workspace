//! Who a local call comes from: the calling session's R9 id, trust level and own scope.
//!
//! `awm mcp` names its session by pid (`X-Awm-Session-Pid`). The session record gives the
//! session id and cwd. Reflection's ledger, `<state>/services/reflection/trust.json`, gives
//! the level, and a session with no entry is trusted, as the ledger reads it.

use std::path::{Path, PathBuf};

use awm_proto::Caller;
use serde_json::Value;

pub const TRUST_KEY: &str = "_caller_trust";
pub const SCOPE_KEY: &str = "_caller_scope";
pub const DEFAULT_TRUST: &str = "trusted";

/// Where Claude Code keeps one record per live session, as `awm.claudedaemon.sessionmode` reads it.
pub fn sessions_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("AWM_CX_SESSIONS").filter(|v| !v.is_empty()) {
        return dir.into();
    }
    let home = std::env::var_os("CLAUDE_CONFIG_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".claude"));
    home.join("sessions")
}

pub fn ledger_path(state: &Path) -> PathBuf {
    state.join("services").join("reflection").join("trust.json")
}

fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

/// `cc` and the session UUID without dashes, the R9 id of a Claude Code record.
fn r9(record: &Value) -> Option<String> {
    let digits: String = record.get("sessionId")?.as_str()?.trim().to_lowercase().replace('-', "");
    (digits.len() == 32 && digits.bytes().all(|b| b.is_ascii_hexdigit())).then(|| format!("cc{digits}"))
}

fn level(ledger: &Path, session: Option<&str>) -> String {
    session
        .and_then(|s| read_json(ledger)?.get(s)?.get("level")?.as_str().map(str::to_string))
        .filter(|l| !l.is_empty())
        .unwrap_or_else(|| DEFAULT_TRUST.to_string())
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

/// Fill `session`, `trust` and `scope` of a local caller from the session that owns `pid`.
pub fn fill(caller: &mut Caller, pid: u32, sessions: &Path, ledger: &Path, projects: &Path) {
    let record = read_json(&sessions.join(format!("{pid}.json"))).unwrap_or(Value::Null);
    caller.session = r9(&record);
    caller.trust = Some(level(ledger, caller.session.as_deref()));
    caller.scope = record.get("cwd").and_then(Value::as_str).and_then(|cwd| scope_of(Path::new(cwd), projects));
}

/// Write the caller's trust and scope into a call's arguments, for services that predate the
/// `caller` frame. Any value the caller sent is dropped first.
pub fn stamp(args: &mut Value, caller: &Caller) {
    let Some(map) = args.as_object_mut() else { return };
    map.remove(TRUST_KEY);
    map.remove(SCOPE_KEY);
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

    const UUID: &str = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0";

    fn node() -> (tempfile::TempDir, PathBuf, PathBuf, PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let (sessions, projects) = (tmp.path().join("sessions"), tmp.path().join("projects"));
        let worktree = projects.join("awm/redux-x");
        std::fs::create_dir_all(worktree.join(".awm")).unwrap();
        std::fs::create_dir_all(worktree.join("core/src")).unwrap();
        std::fs::create_dir_all(projects.join("loose/a/b")).unwrap();
        std::fs::create_dir_all(&sessions).unwrap();
        let rec = json!({"sessionId": UUID, "cwd": worktree.join("core/src")});
        std::fs::write(sessions.join("4242.json"), rec.to_string()).unwrap();
        std::fs::write(sessions.join("5151.json"), json!({"sessionId": "f".repeat(32), "cwd": "/"}).to_string()).unwrap();
        let ledger = tmp.path().join("trust.json");
        let key = format!("cc{}", UUID.replace('-', ""));
        std::fs::write(&ledger, json!({key: {"level": "cooperative", "origin": "seat:ana"}}).to_string()).unwrap();
        (tmp, sessions, ledger, projects)
    }

    #[test]
    fn a_session_gets_its_ledger_level_and_its_scope() {
        let (_tmp, sessions, ledger, projects) = node();
        let mut c = Caller::default();
        fill(&mut c, 4242, &sessions, &ledger, &projects);
        assert_eq!(c.session.as_deref(), Some(format!("cc{}", UUID.replace('-', ""))).as_deref());
        assert_eq!(c.trust.as_deref(), Some("cooperative"));
        assert_eq!(c.scope.as_deref(), Some("awm/redux-x"));
    }

    #[test]
    fn no_entry_no_record_or_no_ledger_reads_as_trusted() {
        let (_tmp, sessions, ledger, projects) = node();
        let mut c = Caller::default();
        fill(&mut c, 5151, &sessions, &ledger, &projects);
        assert_eq!((c.trust.as_deref(), c.scope.as_deref()), (Some("trusted"), None));
        fill(&mut c, 77, &sessions, &ledger, &projects);
        assert_eq!((c.session.as_deref(), c.trust.as_deref()), (None, Some("trusted")));
        std::fs::remove_file(&ledger).unwrap();
        fill(&mut c, 4242, &sessions, &ledger, &projects);
        assert_eq!(c.trust.as_deref(), Some("trusted"));
    }

    #[test]
    fn a_path_under_no_scope_falls_back_to_its_own_path() {
        let (_tmp, _, _, projects) = node();
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
