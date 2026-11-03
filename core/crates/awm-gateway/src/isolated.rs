//! The door for isolated sessions (fleet.md R109): MCP over HTTP for a container session.
//!
//! A container has no host pid, so it names itself by the caller token cch minted for it
//! (`X-Awm-Caller-Token`). The token's holder comes from the harness plugin, and the holder
//! carries the session's whitelist. The door admits a verb only on that whitelist, only on
//! this node and only inside the session's own scope.

use std::collections::{BTreeMap, HashMap};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use awm_proto::{Caller, Lane};
use serde_json::{json, Value};

use crate::caller::{SCOPE_KEY, SESSION_KEY, TRUST_KEY};

pub const TOKEN_HEADER: &str = "x-awm-caller-token";
pub const PROVIDER_TOOL: &str = "harness-cc";
pub const PROVIDER_VERB: &str = "caller_of_token";
const CACHE_FOR: Duration = Duration::from_secs(10);

/// The reflection verbs that act on the caller's own session. Every other reflection verb
/// reaches another session, which an isolated session may never do (R109 item 2).
pub const OWN_SESSION_VERBS: [&str; 5] = ["whoami", "compact", "message_self", "pending", "mode"];

/// The levels a token may carry. An isolated session is never trusted.
const LEVELS: [&str; 2] = ["untrusted", "cooperative"];

/// The session that holds a caller token, as `harness-cc caller_of_token` answers it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Holder {
    pub session_id: String,
    pub name: Option<String>,
    pub project: String,
    pub scope: String,
    pub trust: String,
    pub tools: BTreeMap<String, Vec<String>>,
}

impl Holder {
    /// Read a provider answer. An unknown token, or one with no session or scope, is None.
    pub fn from_answer(v: &Value) -> Option<Self> {
        if v.get("known").and_then(Value::as_bool) != Some(true) {
            return None;
        }
        let text = |k: &str| v.get(k).and_then(Value::as_str).map(str::trim).filter(|s| !s.is_empty()).map(str::to_string);
        let trust = text("trust").filter(|t| LEVELS.contains(&t.as_str())).unwrap_or_else(|| LEVELS[0].to_string());
        let tools = v
            .get("tools")
            .and_then(Value::as_object)
            .map(|m| {
                m.iter()
                    .map(|(t, verbs)| {
                        let verbs = verbs.as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_string).collect();
                        (t.clone(), verbs)
                    })
                    .collect()
            })
            .unwrap_or_default();
        Some(Self { session_id: text("session_id")?, name: text("name"), project: text("project")?, scope: text("scope")?, trust, tools })
    }

    pub fn own_scope(&self) -> String {
        format!("{}/{}", self.project, self.scope)
    }

    pub fn may_use(&self, tool: &str) -> bool {
        within_ceiling(tool, None) && self.tools.contains_key(tool)
    }

    pub fn may(&self, tool: &str, verb: &str) -> bool {
        within_ceiling(tool, Some(verb)) && self.tools.get(tool).is_some_and(|v| v.iter().any(|x| x == verb))
    }

    pub fn caller(&self, node: &str) -> Caller {
        Caller {
            node: node.to_string(),
            lane: Lane::Local,
            session: Some(self.session_id.clone()),
            trust: Some(self.trust.clone()),
            scope: Some(self.own_scope()),
        }
    }
}

/// What the gateway refuses an isolated session whatever its token says: ssh (R109 item 1),
/// the harness plugins, and every reflection verb that reaches another session (item 2).
fn within_ceiling(tool: &str, verb: Option<&str>) -> bool {
    if tool == "ssh" || tool.starts_with("harness-") {
        return false;
    }
    match (tool, verb) {
        ("reflection", Some(v)) => OWN_SESSION_VERBS.contains(&v),
        _ => true,
    }
}

/// Pin a call to the holder's own scope, then stamp who is calling.
///
/// `project`, `scope` and `to_scope` must name the session's own when sent. A verb that
/// declares `project` or `scope` gets the session's value when the caller sent none.
pub fn pin(holder: &Holder, spec: &Value, args: &mut Value) -> Result<(), String> {
    let Some(map) = args.as_object_mut() else { return Err("'args' must be an object".into()) };
    map.retain(|k, _| !k.starts_with("_caller"));
    let own = holder.own_scope();
    let checks: [(&str, Vec<&str>); 3] = [
        ("project", vec![holder.project.as_str()]),
        ("scope", vec![holder.scope.as_str(), own.as_str()]),
        ("to_scope", vec![holder.scope.as_str(), own.as_str()]),
    ];
    for (key, allowed) in &checks {
        match map.get(*key) {
            None | Some(Value::Null) => {}
            Some(Value::String(s)) if allowed.contains(&s.trim()) => {}
            Some(other) => {
                return Err(format!("an isolated session acts only in its own scope {own}; {key}={other} is outside it"));
            }
        }
    }
    let declared: Vec<&str> =
        spec.get("params").and_then(Value::as_array).into_iter().flatten().filter_map(|p| p.get("name").and_then(Value::as_str)).collect();
    for (key, value) in [("project", &holder.project), ("scope", &holder.scope)] {
        if declared.contains(&key) && matches!(map.get(key), None | Some(Value::Null)) {
            map.insert(key.into(), Value::String(value.clone()));
        }
    }
    map.insert(TRUST_KEY.into(), Value::String(holder.trust.clone()));
    map.insert(SCOPE_KEY.into(), Value::String(own));
    map.insert(SESSION_KEY.into(), Value::String(holder.session_id.clone()));
    Ok(())
}

/// Holders the provider named recently. A revoked token stops working within `CACHE_FOR`.
#[derive(Default)]
pub struct TokenCache {
    seen: Mutex<HashMap<String, (Instant, Holder)>>,
}

impl TokenCache {
    pub fn get(&self, token: &str) -> Option<Holder> {
        let mut seen = self.seen.lock().expect("token cache");
        match seen.get(token) {
            Some((at, h)) if at.elapsed() < CACHE_FOR => Some(h.clone()),
            Some(_) => {
                seen.remove(token);
                None
            }
            None => None,
        }
    }

    pub fn put(&self, token: &str, holder: Holder) {
        let mut seen = self.seen.lock().expect("token cache");
        seen.retain(|_, (at, _)| at.elapsed() < CACHE_FOR);
        seen.insert(token.to_string(), (Instant::now(), holder));
    }
}

/// A JSON-RPC error reply.
pub fn rpc_error(id: &Value, code: i64, message: impl Into<String>) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message.into()}})
}

#[cfg(test)]
mod tests {
    use super::*;

    fn holder() -> Holder {
        Holder::from_answer(&json!({
            "ok": true, "known": true, "session_id": "cc0123", "claude_session_id": "0123", "name": "iso",
            "project": "awm", "scope": "redux-x", "trust": "untrusted",
            "tools": {"journal": ["fetch", "post"], "reflection": ["whoami", "start_other"], "ssh": ["run"], "harness-cc": ["send"]},
        }))
        .unwrap()
    }

    #[test]
    fn an_unknown_or_scopeless_answer_is_no_holder() {
        assert_eq!(Holder::from_answer(&json!({"ok": true, "known": false})), None);
        assert_eq!(Holder::from_answer(&json!({"known": true, "session_id": "cc1", "project": "awm"})), None);
    }

    #[test]
    fn a_token_is_never_trusted() {
        let h = Holder::from_answer(&json!({"known": true, "session_id": "cc1", "project": "a", "scope": "b", "trust": "trusted"})).unwrap();
        assert_eq!(h.trust, "untrusted");
        let h = Holder::from_answer(&json!({"known": true, "session_id": "cc1", "project": "a", "scope": "b", "trust": "cooperative"})).unwrap();
        assert_eq!(h.trust, "cooperative");
    }

    #[test]
    fn the_ceiling_holds_whatever_the_whitelist_says() {
        let h = holder();
        assert!(h.may("journal", "post") && !h.may("journal", "subscribe"));
        assert!(h.may("reflection", "whoami") && !h.may("reflection", "start_other"));
        assert!(!h.may_use("ssh") && !h.may("ssh", "run"));
        assert!(!h.may_use("harness-cc") && !h.may("harness-cc", "send"));
        assert!(!h.may_use("search"));
    }

    #[test]
    fn pin_fills_the_own_scope_and_refuses_another() {
        let h = holder();
        let spec = json!({"params": [{"name": "project"}, {"name": "scope"}, {"name": "body"}, {"name": "to_scope"}]});
        let mut args = json!({"body": "hi", "_caller_trust": "trusted", "_caller_pid": 1, "_caller_session_id": "cc9"});
        pin(&h, &spec, &mut args).unwrap();
        assert_eq!(
            args,
            json!({"body": "hi", "project": "awm", "scope": "redux-x", "_caller_trust": "untrusted",
                   "_caller_scope": "awm/redux-x", "_caller_session_id": "cc0123"})
        );
        for bad in [json!({"project": "cloud"}), json!({"scope": "redux-y"}), json!({"to_scope": "awm/redux-y"}), json!({"scope": 3})] {
            let mut a = bad.clone();
            assert!(pin(&h, &spec, &mut a).unwrap_err().contains("own scope awm/redux-x"), "{bad}");
        }
        let mut ok = json!({"scope": "awm/redux-x", "to_scope": "redux-x"});
        pin(&h, &json!({}), &mut ok).unwrap();
        assert_eq!(ok["scope"], "awm/redux-x");
        assert!(ok.get("project").is_none(), "an undeclared project is not filled: {ok}");
    }

    #[test]
    fn the_cache_forgets_after_its_window() {
        let cache = TokenCache::default();
        cache.put("t", holder());
        assert_eq!(cache.get("t").map(|h| h.session_id), Some("cc0123".into()));
        cache.seen.lock().unwrap().get_mut("t").unwrap().0 -= CACHE_FOR;
        assert_eq!(cache.get("t"), None);
    }
}
