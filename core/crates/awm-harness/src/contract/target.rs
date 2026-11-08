//! The session a harness@1 verb names: by R9 `session_id`, by `ref`, by `pid`, or none (the
//! caller's own).

use awm_plugin_sdk::ids;
use serde_json::{json, Value};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    /// An R9 session id, lowercased.
    Session(String),
    /// The plugin's own reference: a job id, a name or a raw id.
    Ref(String),
    Pid(u32),
    None,
}

impl Target {
    /// The target in `args`, taking `session_id`, then `ref`, then `pid`. A `pid` that is not a
    /// positive integer counts as absent.
    pub fn from_args(args: &Value) -> Self {
        if let Some(sid) = super::args::text(args.get("session_id")) {
            return Target::Session(sid.to_lowercase());
        }
        if let Some(r) = super::args::text(args.get("ref")) {
            return Target::Ref(r);
        }
        match super::args::int_lenient(args.get("pid"), 0) {
            p if p > 0 && p <= u32::MAX as i64 => Target::Pid(p as u32),
            _ => Target::None,
        }
    }

    /// The platform's raw session id behind a `Session` target, refusing an id of another
    /// platform. None for the other targets.
    pub fn raw_session(&self, platform: &str, platform_name: &str) -> Result<Option<String>, String> {
        let Target::Session(sid) = self else { return Ok(None) };
        if !sid.starts_with(platform) {
            return Err(format!("{sid:?} is not a {platform_name} session id (it does not start with {platform:?})"));
        }
        match ids::parse_r9(sid) {
            Ok((_, raw)) => Ok(Some(raw)),
            Err(e) => Err(e.to_string()),
        }
    }
}

/// The three target params every session verb of harness@1 takes. `ref_text` says what this
/// harness accepts as a reference.
pub fn target_params(ref_text: &str) -> Vec<Value> {
    vec![
        json!({"name": "session_id", "type": "string", "required": false, "description": "The session's R9 id."}),
        json!({"name": "ref", "type": "string", "required": false,
               "description": ref_text}),
        json!({"name": "pid", "type": "integer", "required": false,
               "description": "The caller's own pid. With no session_id or ref the verb acts on the session at this pid."}),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_beats_ref_beats_pid_and_a_foreign_session_is_refused() {
        let all = json!({"session_id": "CCabc", "ref": "j1", "pid": 5});
        assert_eq!(Target::from_args(&all), Target::Session("ccabc".into()));
        assert_eq!(Target::from_args(&json!({"ref": "j1", "pid": 5})), Target::Ref("j1".into()));
        assert_eq!(Target::from_args(&json!({"pid": "5"})), Target::Pid(5));
        assert_eq!(Target::from_args(&json!({"pid": "x", "ref": " "})), Target::None);
        assert_eq!(Target::from_args(&json!({"pid": -3})), Target::None);


        let t = Target::Session("ds0123abcd".into());
        let e = t.raw_session("cc", "Claude Code").unwrap_err();
        assert!(e.contains("not a Claude Code session id"), "{e}");
        let ok = Target::Session("cc0f1e2d3c4b5a69788796a5b4c3d2e1f0".into());
        assert_eq!(ok.raw_session("cc", "Claude Code").unwrap().unwrap(), "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0");
        assert_eq!(Target::Pid(3).raw_session("cc", "x").unwrap(), None);
    }
}
