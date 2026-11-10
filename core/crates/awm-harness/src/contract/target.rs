//! How a harness@1 verb names its session: by canonical `session_id`, else the caller's `pid`.
//! A name resolves to an id through `find` first, never as a target.

use serde_json::{json, Value};

/// The two target params every session verb of harness@1 takes.
pub fn target_params() -> Vec<Value> {
    vec![
        json!({"name": "session_id", "type": "string", "required": false, "description": "The session's canonical id."}),
        json!({"name": "pid", "type": "integer", "required": false,
               "description": "The caller's own pid. With no session_id the verb acts on the session at this pid."}),
    ]
}

/// The `find` verb's params: the whole session name, matched case-blind.
pub fn find_params() -> Vec<Value> {
    vec![json!({"name": "name", "type": "string", "required": true, "description": "The whole session name, any case."})]
}
