//! Asking another node's search for the sources named on it, each answer apart.
//!
//! A station runs no search and is never asked, nor is a node with no edge from this one, nor
//! one that an open tag clash stands between. A node that fails or refuses shows as an error on
//! its sources and never fails the search.

use crate::hits;
use crate::request::{label, Kinds};
use crate::{BoxFut, CallError};
use awm_plugin_sdk::config::EdgeList;
use serde_json::{json, Map, Value};
use std::time::Duration;

/// Budget for one peer, connect to reply.
pub const PEER_TIMEOUT: Duration = Duration::from_secs(10);
/// A reply larger than this is discarded unread.
pub const MAX_REPLY_BYTES: usize = 2 * 1024 * 1024;
pub const CLASH_REFUSAL: &str = "an open tag clash stands between this node and it; searches wait until an agent renames the tag";

/// The nodes this node holds an edge to, and the call to one. Tests stand in for both.
pub trait Peers: Send + Sync {
    fn edges(&self) -> EdgeList;
    /// `tool`'s `verb` on `peer`, through this node's gateway.
    fn invoke<'a>(&'a self, peer: &'a str, tool: &'a str, verb: &'a str, args: Value, timeout: Duration) -> BoxFut<'a, Result<Value, CallError>>;
}

/// The edge list on disk and the gateway's peer calls.
pub struct HubPeers(pub awm_plugin_sdk::Hub);

impl Peers for HubPeers {
    fn edges(&self) -> EdgeList {
        awm_plugin_sdk::config::edges().unwrap_or_else(|e| {
            tracing::warn!("search: edge list unreadable: {e:#}");
            EdgeList::default()
        })
    }

    fn invoke<'a>(&'a self, peer: &'a str, tool: &'a str, verb: &'a str, args: Value, timeout: Duration) -> BoxFut<'a, Result<Value, CallError>> {
        Box::pin(async move {
            let opts = awm_plugin_sdk::CallOpts { peer: Some(peer.to_string()), timeout, ..Default::default() };
            self.0.call_with(tool, verb, args, &opts).await.map_err(CallError::from)
        })
    }
}

/// A node's answer: `found` maps each source asked, by its bare kind, to its part.
#[derive(Debug, Clone)]
pub struct PeerResult {
    pub name: String,
    /// `ok`, `error`, `refused` or `station`.
    pub status: String,
    pub error: Option<String>,
    pub found: Map<String, Value>,
}

impl PeerResult {
    fn failed(name: &str, status: &str, error: String) -> Self {
        PeerResult { name: name.into(), status: status.into(), error: Some(error), found: Map::new() }
    }
}

/// The node in `edges` that `name` labels, among those `here` holds an edge to.
fn record(edges: &EdgeList, here: &str, name: &str) -> Option<String> {
    edges.reachable_from(&label(here)).into_iter().find(|r| label(r) == label(name))
}

/// The peer's answer: one object per source asked.
fn reply(raw: Value, names: &[String]) -> Result<Map<String, Value>, CallError> {
    let mut raw = raw;
    if let Value::Object(m) = &raw {
        if m.contains_key("result") && !names.iter().any(|n| m.contains_key(n)) {
            raw = m["result"].clone();
        }
    }
    let size = match &raw {
        Value::String(s) => s.chars().count(),
        other => awm_pycompat::json::dumps(other).chars().count(),
    };
    if size > MAX_REPLY_BYTES {
        return Err(CallError::new("ValueError", format!("reply of {size} bytes exceeds the {MAX_REPLY_BYTES} byte cap")));
    }
    if let Value::String(s) = &raw {
        raw = serde_json::from_str(s).map_err(|e| CallError::new("JSONDecodeError", e.to_string()))?;
    }
    match raw {
        Value::Object(m) if names.iter().any(|n| m.get(n).is_some_and(Value::is_object)) => Ok(m),
        other => {
            let detail = other.get("error").filter(|e| crate::live::truthy(e)).map(hits::py_str);
            Err(CallError::new("RuntimeError", detail.unwrap_or_else(|| "reply names none of the sources asked".into())))
        }
    }
}

/// One source's part of a peer's answer, cleaned to the kind's hit fields.
fn answer(raw: Option<&Value>, limit: usize, fields: &[String]) -> Value {
    let Some(Value::Object(m)) = raw else {
        return json!({"exact": [], "error": "the node did not answer for this source"});
    };
    let mut out = Map::new();
    out.insert("exact".into(), Value::Array(hits::section(m.get("exact"), limit, fields)));
    if m.contains_key("related") {
        out.insert("related".into(), Value::Array(hits::section(m.get("related"), limit, fields)));
    }
    if let Some(e) = m.get("error").filter(|e| crate::live::truthy(e)) {
        out.insert("error".into(), json!(hits::py_str(e).chars().take(300).collect::<String>()));
    }
    Value::Object(out)
}

/// The answers for the sources `args` names, asked of the node that holds them.
pub async fn ask_home(peers: &dyn Peers, here: &str, home: &str, args: Value, limit: usize, kinds: &Kinds, clashed: bool) -> PeerResult {
    let edges = peers.edges();
    let Some(name) = record(&edges, here, home) else {
        return PeerResult::failed(home, "error", format!("no edge from {} to {home}", label(here)));
    };
    if clashed {
        return PeerResult::failed(home, "refused", CLASH_REFUSAL.into());
    }
    if edges.nodes.get(&name).is_some_and(|n| n.role == "station") {
        return PeerResult::failed(&name, "station", "a station runs no search".into());
    }
    let names: Vec<String> = args["query"].as_object().map(|q| q.keys().filter(|k| *k != "text").cloned().collect()).unwrap_or_default();
    let call = peers.invoke(&name, "search", "query", args, PEER_TIMEOUT);
    let raw = match tokio::time::timeout(PEER_TIMEOUT + Duration::from_secs(1), call).await {
        Err(_) => return PeerResult::failed(&name, "error", format!("timeout after {}s", PEER_TIMEOUT.as_secs())),
        Ok(Err(e)) => return PeerResult::failed(&name, "error", e.describe()),
        Ok(Ok(raw)) => raw,
    };
    let reply = match reply(raw, &names) {
        Ok(r) => r,
        Err(e) => return PeerResult::failed(&name, "error", e.describe()),
    };
    let found = names.iter().map(|n| (n.clone(), answer(reply.get(n), limit, &kinds.hit_fields(n)))).collect();
    PeerResult { name, status: "ok".into(), error: None, found }
}

/// One document from a peer's search, stamped with the peer as its origin.
pub async fn fetch(peers: &dyn Peers, here: &str, name: &str, source: &str, id: &str) -> Result<Value, CallError> {
    let edges = peers.edges();
    let Some(node) = record(&edges, here, name) else {
        return Err(CallError::new("ValueError", format!("no edge from {} to {name}", label(here))));
    };
    let call = peers.invoke(&node, "search", "get", json!({"source": source, "id": id}), PEER_TIMEOUT);
    let mut raw = match tokio::time::timeout(PEER_TIMEOUT + Duration::from_secs(1), call).await {
        Err(_) => return Err(CallError::new("TimeoutError", String::new())),
        Ok(r) => r?,
    };
    if let Value::Object(m) = &raw {
        if !m.contains_key("document") && m.contains_key("result") {
            raw = m["result"].clone();
        }
    }
    if let Value::String(s) = &raw {
        raw = serde_json::from_str(s).map_err(|e| CallError::new("JSONDecodeError", e.to_string()))?;
    }
    let Some(Value::Object(doc)) = raw.get("document") else {
        let detail = raw.get("error").filter(|e| crate::live::truthy(e)).map(hits::py_str);
        return Err(CallError::new("RuntimeError", detail.unwrap_or_else(|| "reply has no document".into())));
    };
    let mut out = doc.clone();
    out.insert("source".into(), json!(source));
    out.insert("id".into(), json!(id));
    out.insert("origin".into(), json!({"node": node}));
    Ok(json!({"document": out}))
}
