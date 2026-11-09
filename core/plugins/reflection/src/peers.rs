//! The peers: the nodes this node holds an edge to, read from the edge list. A retire is told to
//! each, a wait asks each which of its sessions retired there, and an unknown job asks each
//! whether the job runs there.

use std::time::Duration;

use futures_util::future::join_all;
use serde_json::{json, Value};

use awm_pycompat::repr::str_repr;
use crate::app::{is_false, text, truthy, App};
use crate::gateway::{Call, Gateway};

pub const LIST_TIMEOUT: Duration = Duration::from_secs(5);
pub const RETIRED_TIMEOUT: Duration = Duration::from_secs(15);
pub const AMONG_TIMEOUT: Duration = Duration::from_secs(10);

/// Whether a session row of a peer answers to `reference`.
pub fn names(row: &Value, reference: &str) -> bool {
    let field = |k: &str| match row.get(k) {
        Some(v) if truthy(Some(v)) => v.as_str().map_or_else(|| v.to_string(), str::to_string),
        _ => String::new(),
    };
    if reference == field("job") || reference == field("session_id") {
        return true;
    }
    text(row, "name").unwrap_or_default().to_lowercase() == reference.to_lowercase()
}

/// The clause the unknown-job error gains, or `""`.
pub fn hint_text(found: &[String]) -> String {
    match found {
        [] => String::new(),
        [one] => format!(". It runs on {one}: call again with peer={}", str_repr(one)),
        many => format!(". Sessions on {} answer to it: call again with peer=<one of them> and the job id", many.join(", ")),
    }
}

impl<G: Gateway> App<G> {
    /// The peers, or none when the edge list cannot be read.
    pub fn peer_list(&self) -> Vec<String> {
        self.node.peers().unwrap_or_else(|e| {
            tracing::info!("reflection: cannot read the peer book or edge list: {e}");
            vec![]
        })
    }

    async fn ask_peer(&self, peer: &str, verb: &str, args: Value, timeout: Duration) -> Result<Value, String> {
        self.gw.call(Call::new("reflection", verb, args, timeout).peer(Some(peer))).await.map_err(|e| e.to_string())
    }

    /// The peers whose session list names `reference`, sorted. A peer that does not answer is
    /// skipped: this only sharpens an error.
    pub async fn holders(&self, reference: &str) -> Vec<String> {
        let peers = self.peer_list();
        let asks = peers.iter().map(|peer| async move {
            match self.ask_peer(peer, "list_other", json!({}), LIST_TIMEOUT).await {
                Ok(reply) => {
                    let rows = reply.get("sessions").and_then(Value::as_array).cloned().unwrap_or_default();
                    rows.iter().filter(|r| r.is_object()).any(|r| names(r, reference)).then(|| peer.clone())
                }
                Err(e) => {
                    tracing::info!("reflection: cannot list sessions on {peer}: {e}");
                    None
                }
            }
        });
        let mut found: Vec<String> = join_all(asks).await.into_iter().flatten().collect();
        found.sort();
        found
    }

    /// Tell every peer that `session_id` retired, so a wait there wakes. A peer that misses it
    /// checks again at its start.
    pub async fn tell_peers(&self, session_id: &str) {
        let peers = self.peer_list();
        join_all(peers.iter().map(|peer| async move {
            if let Err(e) = self.ask_peer(peer, "retired", json!({"session_id": session_id}), RETIRED_TIMEOUT).await {
                tracing::warn!("reflection: could not tell {peer} that {session_id} retired: {e}");
            }
        }))
        .await;
    }

    /// Each peer's `retired_among` answer, with `peer` and whether it `answered`.
    pub async fn ask_peers(&self, session_ids: &[String]) -> Vec<Value> {
        let peers = self.peer_list();
        let asks = peers.iter().map(|peer| async move {
            match self.ask_peer(peer, "retired_among", json!({"session_ids": session_ids}), AMONG_TIMEOUT).await {
                Ok(Value::Object(mut m)) => {
                    m.insert("peer".into(), json!(peer));
                    m.insert("answered".into(), json!(true));
                    Some(Value::Object(m))
                }
                Ok(_) => None,
                Err(e) => {
                    tracing::info!("reflection: {peer} did not answer retired_among: {e}");
                    Some(json!({"peer": peer, "answered": false}))
                }
            }
        });
        join_all(asks).await.into_iter().flatten().collect()
    }

    /// The ids among `session_ids` that retired on some peer, sorted.
    pub async fn retired_elsewhere(&self, session_ids: Vec<String>) -> Vec<String> {
        if session_ids.is_empty() {
            return vec![];
        }
        let mut out: Vec<String> = self
            .ask_peers(&session_ids)
            .await
            .iter()
            .flat_map(|r| r.get("retired").and_then(Value::as_array).cloned().unwrap_or_default())
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect();
        out.sort();
        out.dedup();
        out
    }

    /// `live` or `stopped` when this node's harness knows the session, `gone` when it says it
    /// does not, else None.
    pub async fn session_state(&self, session_id: &str) -> Option<String> {
        let p = self.for_platform(crate::providers::platform_of(session_id)).await.ok()?;
        let out = self.harness_call(&p, "status", json!({"session_id": session_id}), None).await.ok()?;
        if is_false(&out, "ok") {
            return truthy(out.get("unknown_ref")).then(|| "gone".into());
        }
        Some(if truthy(out.get("live")) { "live" } else { "stopped" }.into())
    }

    /// `live` or `stopped` when some node knows the session, `gone` when every node said no.
    pub async fn state_anywhere(&self, session_id: &str) -> Option<String> {
        let here = self.session_state(session_id).await;
        if matches!(here.as_deref(), Some("live" | "stopped")) {
            return here;
        }
        let replies = self.ask_peers(&[session_id.to_string()]).await;
        let known = |r: &Value| r.get("known").and_then(Value::as_array).is_some_and(|k| k.iter().any(|s| s.as_str() == Some(session_id)));
        if replies.iter().any(known) {
            return Some("stopped".into());
        }
        if here.is_none() || !replies.iter().all(|r| truthy(r.get("answered"))) {
            return None;
        }
        Some("gone".into())
    }
}
