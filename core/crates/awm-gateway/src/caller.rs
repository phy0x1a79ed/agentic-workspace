//! Who a local call comes from: the calling session's R9 id, trust level, mode and own scope.
//!
//! A caller names a pid: `awm mcp` its session's (`X-Awm-Session-Pid`), the CLI its own
//! (`X-Awm-Caller-Pid`). The gateway walks that pid's ancestors and asks reflection
//! `session_of` for each, which asks the harness plugins (install.md R135 item 6). The first
//! session found is the caller. A chain no harness claims is a user's shell, which passes as
//! trusted. A lookup that fails is untrusted with mode `unknown` (fleet.md R103 item 2), and so
//! is every lookup on a node whose plugin set holds reflection but did not load it.

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
/// A session claim from another node. A borrower's `{kind, job, name}` survives an edge call,
/// and a door call carries `{session_id, node, job?, name?}`: the calling session as the calling
/// gateway stamped it, on the node the door verified.
pub const CLAIM_KEY: &str = "_caller_session";
/// The calling plugin, as a Python service's adapter sets it from the call frame. The gateway
/// drops any copy in the args, since only the frame's `plugin` names the caller.
pub const PLUGIN_KEY: &str = "_caller_plugin";
/// Where `resolve_caller` records which pid of the walk the session answered for.
pub const FOUND_PID: &str = "_found_pid";
pub const FAILED_LOOKUP_TRUST: &str = "untrusted";
/// A caller that is no session: a user's shell on this node, or a plugin's service.
pub const NO_SESSION_TRUST: &str = "trusted";
pub const TRUSTED: &str = "trusted";
/// The tool and verb the gateway asks who owns a pid.
pub const LOOKUP_TOOL: &str = "reflection";
pub const LOOKUP_VERB: &str = "session_of";
/// The plugin that serves `LOOKUP_TOOL`.
pub const LOOKUP_PLUGIN: &str = "reflection";
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
    /// reflection answered nonsense.
    Failed,
    /// reflection is not registered, or did not answer within the lookup wait.
    Unreachable,
}

/// Whether a `session_of` answer is worth keeping: it names a session with a readable mode, or
/// says a pid is nobody's. An `unknown` or missing mode may be a harness mid-restart.
pub fn is_definite(answer: &Value) -> bool {
    answer.is_object() && answer.get("mode").is_some_and(|m| m.as_str() != Some(crate::modes::UNKNOWN))
}

/// Whether the node's plugin set holds the lookup plugin: the install config names it, or the
/// loader found it in a root and refused it. Only a node without it runs no harness, so only
/// there a pid nobody serves the lookup for is a user's shell.
pub fn lookup_expected(load: &crate::loader::LoadResult, cfg: &crate::config::Config) -> bool {
    let named = cfg.wants(LOOKUP_PLUGIN) && cfg.plugins.iter().any(|p| p == LOOKUP_PLUGIN);
    named || load.refused.iter().any(|r| r.plugin == LOOKUP_PLUGIN)
}

/// The parent of `pid`, from `/proc/<pid>/stat` field 4. The comm field may hold spaces
/// and parens, so the fields are counted from the last `)`.
pub fn ppid_of(pid: u32) -> Option<u32> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    stat[stat.rfind(')')? + 2..].split_whitespace().nth(1)?.parse().ok()
}

/// `<project>/<scope>` of a directory under `projects`, as `awm.config.scope_of` reads it:
/// the deepest enclosing git worktree, the directory that holds a `.git`, else the path below
/// the project.
pub fn scope_of(cwd: &Path, projects: &Path) -> Option<String> {
    let root = projects.canonicalize().ok()?;
    let cwd = cwd.canonicalize().ok()?;
    let parts: Vec<String> =
        cwd.strip_prefix(&root).ok()?.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
    if parts.len() < 2 {
        return None;
    }
    let depth = (2..=parts.len()).rev().find(|&d| root.join(parts[..d].join("/")).join(".git").exists()).unwrap_or(parts.len());
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
            caller.job = text(a, "job");
            caller.name = text(a, "name");
            // reflection says `null` for a session no restricted mode started, and leaves the
            // key out only when it could not tell.
            match a.get("mode") {
                None => Some(crate::modes::UNKNOWN.to_string()),
                Some(m) => m.as_str().map(str::to_string),
            }
        }
        Lookup::NoOwner { mode } => {
            caller.session = None;
            caller.job = None;
            caller.name = None;
            caller.trust = Some(NO_SESSION_TRUST.to_string());
            caller.scope = None;
            mode.clone()
        }
        Lookup::Failed | Lookup::Unreachable => {
            caller.session = None;
            caller.job = None;
            caller.name = None;
            caller.trust = Some(FAILED_LOOKUP_TRUST.to_string());
            caller.scope = None;
            Some(if matches!(lookup, Lookup::Unreachable) { crate::modes::UNREACHABLE } else { crate::modes::UNKNOWN }.to_string())
        }
    }
}

/// Write the caller's trust and scope into a call's arguments, for services that predate the
/// `caller` frame. Any value the caller sent is dropped first. A door call, which comes on the
/// service lane from another node, also gets the calling session as its claim.
pub fn stamp(args: &mut Value, caller: &Caller, pid: Option<u32>, here: &str) {
    let Some(map) = args.as_object_mut() else { return };
    map.remove(TRUST_KEY);
    map.remove(SCOPE_KEY);
    map.remove(SESSION_KEY);
    map.remove(PID_KEY);
    map.remove(PLUGIN_KEY);
    if caller.lane != awm_proto::Lane::Edge {
        map.remove(CLAIM_KEY);
    }
    let door = caller.lane == awm_proto::Lane::Service && caller.node != here;
    if let Some(session) = caller.session.as_ref().filter(|_| door) {
        let node = caller.origin.as_deref().unwrap_or(&caller.node);
        let mut claim = serde_json::json!({"session_id": session, "node": node});
        if let Some(job) = &caller.job {
            claim["job"] = Value::String(job.clone());
        }
        if let Some(name) = &caller.name {
            claim["name"] = Value::String(name.clone());
        }
        map.insert(CLAIM_KEY.into(), claim);
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
        std::fs::create_dir_all(projects.join("awm/redux-x/core/src")).unwrap();
        std::fs::write(projects.join("awm/redux-x/.git"), "gitdir: ../.bare/worktrees/redux-x\n").unwrap();
        std::fs::create_dir_all(projects.join("loose/a/b")).unwrap();
        (tmp, projects)
    }

    #[test]
    fn a_lookup_answer_fills_the_caller_and_names_its_mode() {
        let (_tmp, projects) = projects();
        let cwd = projects.join("awm/redux-x/core/src");
        let answer = json!({"ok": true, "known": true, "session_id": "cc0f1e", "trust": "cooperative", "mode": null, "cwd": cwd,
                            "job": "0f1e", "name": "redux/x"});
        let unread = json!({"known": true, "session_id": "cc0f1e", "trust": "cooperative", "cwd": cwd});
        let mut c = Caller::default();
        let mode = fill(&mut c, &Lookup::Session(answer), &projects);
        assert_eq!((c.session.as_deref(), c.trust.as_deref(), c.scope.as_deref()), (Some("cc0f1e"), Some("cooperative"), Some("awm/redux-x")));
        assert_eq!(mode, None, "a session no restricted mode started is ungated");
        assert_eq!((c.job.as_deref(), c.name.as_deref()), (Some("0f1e"), Some("redux/x")));
        assert_eq!(fill(&mut c, &Lookup::Session(unread), &projects).as_deref(), Some(crate::modes::UNKNOWN), "an unreadable mode is gated hardest");

        let mut c = Caller { job: Some("stale".into()), ..Caller::default() };
        assert_eq!(fill(&mut c, &Lookup::NoOwner { mode: None }, &projects), None, "a shell no harness claims is ungated");
        assert_eq!(c.job, None);
        assert_eq!((c.session.as_deref(), c.trust.as_deref(), c.scope.as_deref()), (None, Some("trusted"), None));

        let mut c = Caller { trust: Some("trusted".into()), ..Caller::default() };
        assert_eq!(fill(&mut c, &Lookup::Failed, &projects).as_deref(), Some("unknown"));
        assert_eq!((c.session.as_deref(), c.trust.as_deref()), (None, Some("untrusted")));

        let mut c = Caller::default();
        fill(&mut c, &Lookup::Session(json!({"known": true, "session_id": "cc1", "cwd": "/", "mode": "worker"})), &projects);
        assert_eq!((c.trust.as_deref(), c.scope.as_deref()), (Some("untrusted"), None), "a known session without a level");

        let mut c = Caller::default();
        assert_eq!(fill(&mut c, &Lookup::Unreachable, &projects).as_deref(), Some(crate::modes::UNREACHABLE));

        assert!(is_definite(&json!({"known": true, "mode": "worker"})));
        assert!(is_definite(&json!({"known": true, "mode": null})));
        assert!(!is_definite(&json!({"known": true, "mode": "unknown"})));
        assert!(!is_definite(&json!({"known": true})));
        assert!(!is_definite(&json!("nonsense")));
    }

    fn config(plugins: &[&str]) -> crate::config::Config {
        let text = format!("plugins = {:?}", plugins);
        toml::from_str(&text).unwrap()
    }

    #[test]
    fn a_lookup_is_expected_unless_reflection_is_simply_absent() {
        let load = crate::loader::LoadResult::default();
        assert!(!lookup_expected(&load, &config(&["*"])), "no reflection folder in the roots");
        assert!(!lookup_expected(&load, &config(&["journal", "scopes"])));
        let mut refused = crate::loader::LoadResult::default();
        refused.refused.push(crate::loader::Refusal {
            plugin: LOOKUP_PLUGIN.into(),
            dir: "/x/reflection".into(),
            reason: "needs harness@1".into(),
        });
        assert!(lookup_expected(&refused, &config(&["*"])), "refused at load");
        assert!(lookup_expected(&load, &config(&["journal", "reflection"])), "named but in no root");
    }

    fn git(dir: &Path, args: &[&str]) -> String {
        let out = std::process::Command::new("git").current_dir(dir).args(args).output().expect("git runs");
        assert!(out.status.success(), "git {args:?}: {}", String::from_utf8_lossy(&out.stderr));
        String::from_utf8(out.stdout).unwrap().trim().to_string()
    }

    /// The workspace layout: a bare repo per project, a worktree per scope, a scope nested
    /// inside another, and a session whose cwd is a subfolder.
    #[test]
    fn a_scope_is_the_deepest_enclosing_worktree_of_a_bare_repo() {
        let tmp = tempfile::tempdir().unwrap();
        let projects = tmp.path().join("projects");
        let p = projects.join("proj");
        std::fs::create_dir_all(&p).unwrap();
        git(&p, &["init", "-q", "--bare", ".bare"]);
        let bare = p.join(".bare");
        let tree = git(&bare, &["mktree"]);
        let commit = git(&bare, &["-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "-m", "root", &tree]);
        git(&bare, &["branch", "dev", &commit]);
        git(&bare, &["branch", "feat/build", &commit]);
        git(&bare, &["branch", "feat/build-x", &commit]);
        git(&bare, &["worktree", "add", "-q", "../dev", "dev"]);
        git(&bare, &["worktree", "add", "-q", "../redux-build", "feat/build"]);
        git(&bare, &["worktree", "add", "-q", "../redux-build/x", "feat/build-x"]);
        std::fs::create_dir_all(p.join("redux-build/x/core/src")).unwrap();
        std::fs::create_dir_all(p.join("redux-build/docs")).unwrap();
        std::fs::create_dir_all(p.join("dev/.awm")).unwrap();
        std::fs::create_dir_all(p.join("loose/a")).unwrap();
        let at = |rel: &str| scope_of(&p.join(rel), &projects);
        assert_eq!(at("dev").as_deref(), Some("proj/dev"));
        assert_eq!(at("dev/.awm").as_deref(), Some("proj/dev"), "a leftover .awm/ marks nothing");
        assert_eq!(at("redux-build").as_deref(), Some("proj/redux-build"));
        assert_eq!(at("redux-build/docs").as_deref(), Some("proj/redux-build"));
        assert_eq!(at("redux-build/x").as_deref(), Some("proj/redux-build/x"), "the nested worktree wins");
        assert_eq!(at("redux-build/x/core/src").as_deref(), Some("proj/redux-build/x"));
        assert_eq!(at("loose/a").as_deref(), Some("proj/loose/a"), "under no worktree: its own path");
        assert_eq!(at("").as_deref(), None);
        assert_eq!(scope_of(tmp.path(), &projects), None);
    }

    #[test]
    fn stamp_replaces_what_the_caller_sent() {
        let caller = Caller { trust: Some("untrusted".into()), scope: Some("awm/x".into()), ..Caller::default() };
        let mut args = json!({"text": "hi", "_caller_trust": "trusted", "_caller_scope": "a/b", "_caller_pid": 1, "_caller_session": {"job": "x"}});
        stamp(&mut args, &caller, Some(42), "");
        assert_eq!(args, json!({"text": "hi", "_caller_trust": "untrusted", "_caller_scope": "awm/x", "_caller_pid": 42}));
        let mut args = json!({"_caller_trust": "trusted", "_caller_pid": 7});
        stamp(&mut args, &Caller::default(), None, "");
        assert_eq!(args, json!({}));
        let edge = Caller { lane: awm_proto::Lane::Edge, ..Caller::default() };
        let mut args = json!({"_caller_session": {"job": "x"}});
        stamp(&mut args, &edge, Some(42), "");
        assert_eq!(args, json!({"_caller_session": {"job": "x"}}), "a claim survives on an edge call, a pid never crosses");
    }

    #[test]
    fn a_door_call_carries_the_calling_session_and_its_level() {
        let door = Caller {
            node: "alcor".into(),
            lane: awm_proto::Lane::Service,
            session: Some("cc12".into()),
            trust: Some("cooperative".into()),
            ..Caller::default()
        };
        let mut args = json!({"_caller_session": {"session_id": "cc99", "job": "forged"}, "_caller_trust": "trusted"});
        stamp(&mut args, &door, None, "deneb");
        assert_eq!(args, json!({"_caller_session": {"session_id": "cc12", "node": "alcor"}, "_caller_trust": "cooperative"}));
        // A relayed call names the node it started from, and the session's job and name ride along.
        let relayed = Caller { origin: Some("mizar".into()), job: Some("0dcd".into()), name: Some("redux/x".into()), ..door.clone() };
        let mut args = json!({"_caller_session": {"session_id": "cc99", "node": "lyra"}});
        stamp(&mut args, &relayed, None, "deneb");
        assert_eq!(args["_caller_session"], json!({"session_id": "cc12", "node": "mizar", "job": "0dcd", "name": "redux/x"}));
        // A service of this node on its own account claims no session.
        let own = Caller { node: "deneb".into(), session: Some("cc12".into()), ..door.clone() };
        let mut args = json!({});
        stamp(&mut args, &own, None, "deneb");
        assert_eq!(args, json!({"_caller_trust": "cooperative"}));
    }
}
