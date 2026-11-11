//! Who holds a pushed channel, a social seat or a channel room, and how to tell they are gone
//! (`gatewayclient/holders.py`, channels.md R69).
//!
//! The model never names the holder. A session is known by its R9 id from `_caller_session_id`,
//! else by the `_caller_pid` record on this node, else, for a call from another node, by the
//! `_caller_session` claim that node read from its own record. A line reaches the holder through
//! `reflection message`, so a holder is a background session or an isolated one.

use std::collections::HashSet;
use std::time::Duration;

use serde::Serialize;
use serde_json::{json, Value};

use crate::hub::{CallOpts, Hub};
use crate::service::{Call, CLAIM_KEY, SESSION_KEY};

const DEAD_STATES: [&str; 2] = ["gone", "stopped"];
pub const ISOLATED: &str = "isolated";
/// Session kinds that run without a terminal and so can hold a lock.
const HOLDER_KINDS: [&str; 3] = ["bg", "web", ISOLATED];

/// The calling session cannot hold the thing. The message says why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HolderError(pub String);

impl std::fmt::Display for HolderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl From<HolderError> for crate::VerbError {
    fn from(e: HolderError) -> Self {
        crate::VerbError::new("HolderError", e.0)
    }
}

/// The calling session as a holder: `session` is its R9 id, which a push types into.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Holder {
    pub node: String,
    pub session: String,
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
}

struct Found {
    background: bool,
    session: Option<String>,
    name: String,
}

fn text(v: &Value, key: &str) -> String {
    match v.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

/// A claim's `kind` for a session's `hosting`. A holder check on another node reads it.
pub fn kind_of(hosting: Option<&str>) -> &'static str {
    if hosting == Some("background") { "bg" } else { "interactive" }
}

/// What a borrower forwards as `_caller_session`: the local session `pid`, or None.
pub async fn claim(hub: &Hub, pid: u32) -> Option<Value> {
    let rec = crate::sessions::session_of(hub, pid).await?;
    let kind = kind_of(rec.get("hosting").and_then(Value::as_str));
    Some(json!({"kind": kind, "name": text(&rec, "name"), "session_id": rec.get("session_id")}))
}

/// Every session on this node from `reflection list_other`, or None when it cannot tell.
pub async fn session_rows(hub: &Hub, include_stopped: bool) -> Option<Vec<Value>> {
    let args = if include_stopped { json!({"include_stopped": true}) } else { json!({}) };
    let opts = CallOpts { timeout: Duration::from_secs(30), ..CallOpts::default() };
    let out = match hub.call_with("reflection", "list_other", args, &opts).await {
        Ok(o) => o,
        Err(e) => {
            tracing::info!("reflection list_other failed: {e}");
            return None;
        }
    };
    let errors = out.get("errors").is_some_and(|e| !e.is_null() && e != &json!([]) && e != &json!({}));
    if !out.is_object() || out.get("ok") == Some(&json!(false)) || errors {
        return None;
    }
    let rows: Vec<Value> = out.get("sessions").and_then(Value::as_array).into_iter().flatten().filter(|r| r.is_object()).cloned().collect();
    let all_ids = rows.iter().all(|r| r.get("session_id").is_some_and(|s| s.as_str().is_some_and(|s| !s.is_empty())));
    (!rows.is_empty() && all_ids).then_some(rows)
}

/// Whether a `reflection list_other` row is a running session.
pub fn row_live(row: &Value) -> bool {
    let state = row.get("state").and_then(Value::as_str).unwrap_or("");
    !DEAD_STATES.contains(&state) && row.get("live") != Some(&json!(false)) && row.get("alive") != Some(&json!(false))
}

/// Whether `session` is a background (`bg`, or `web` for a DeepSeek one) or an isolated session.
async fn can_hold(hub: &Hub, session: &str) -> bool {
    let rows = session_rows(hub, true).await.unwrap_or_default();
    rows.iter().any(|row| text(row, "session_id").to_lowercase() == session && HOLDER_KINDS.contains(&text(row, "kind").as_str()))
}

fn from_claim(raw: Option<&Value>) -> Option<Found> {
    let obj = raw.filter(|r| r.is_object())?;
    let get = |k: &str| obj.get(k).cloned().unwrap_or(Value::Null);
    Some(Found {
        background: HOLDER_KINDS.contains(&text(obj, "kind").as_str()) || get("hosting") == "background",
        session: get("session_id").as_str().map(str::to_string),
        name: text(obj, "name"),
    })
}

/// The calling session as a [`Holder`], or why it cannot hold `what`.
pub async fn caller(call: &Call, what: &str) -> Result<Holder, HolderError> {
    let hub = call.hub();
    let pid = call.caller_pid();
    let sid = call.args.get(SESSION_KEY).and_then(Value::as_str).and_then(crate::ids::SessionId::checked);
    let here = call.ctx.env().node.clone();
    let (found, node) = if let Some(pid) = pid {
        let found = crate::sessions::session_of(hub, pid).await.map(|rec| Found {
            background: rec.get("hosting").and_then(Value::as_str) == Some("background"),
            session: rec.get("session_id").and_then(Value::as_str).map(str::to_string),
            name: text(&rec, "name"),
        });
        (found, Some(here))
    } else if let Some(sid) = &sid {
        let found = Found { background: can_hold(hub, sid).await, session: Some(sid.clone()), name: String::new() };
        (Some(found), Some(here))
    } else {
        (from_claim(call.args.get(CLAIM_KEY)), call.peer_node().map(str::to_string))
    };
    let (Some(node), Some(found)) = (node, found) else {
        return Err(HolderError(format!("no calling session reached {what}: it is held by a running agent session, so call this from one")));
    };
    if !found.background {
        return Err(HolderError(format!(
            "only a background session, or an isolated one, can hold {what}: each message is typed into the holder's prompt"
        )));
    }
    let Some(session) = sid.or(found.session) else {
        return Err(HolderError(format!("the calling session has no session id, so it cannot hold {what}")));
    };
    Ok(Holder { node, session, name: found.name, pid })
}

/// The R9 ids of the sessions running on this node, or None when it cannot tell.
pub async fn live_sessions(hub: &Hub) -> Option<HashSet<String>> {
    let rows = session_rows(hub, false).await?;
    Some(rows.iter().filter(|r| row_live(r)).map(|r| text(r, "session_id").to_lowercase()).collect())
}
