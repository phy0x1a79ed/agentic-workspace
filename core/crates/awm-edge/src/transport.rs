//! The gateway's [`PeerTransport`] over the edges: a node reaches only the peers
//! its edge list gives it an edge to, each over its warm ssh channel.

use awm_gateway::peer::{BoxFuture, PeerError, PeerTransport};
use awm_proto::{CallContext, Caller, Lane};
use awm_trust::edges::EdgeList;
use serde_json::Value;

use crate::client::{EdgeClient, EdgeError};

/// The frame name that asks the far side for its `/hub/table` instead of a tool.
pub const TOOLS_FRAME: &str = "_tools";
/// The frame name that tells the far side this node's table changed (R117 item 3).
pub const TABLE_CHANGED_FRAME: &str = "_table_changed";

pub struct EdgePeers {
    here: String,
    edges: EdgeList,
    client: EdgeClient,
}

impl EdgePeers {
    pub fn new(here: impl Into<String>, edges: EdgeList, client: EdgeClient) -> Self {
        Self { here: here.into(), edges, client }
    }

    fn reachable(&self, peer: &str) -> Result<(), PeerError> {
        if self.edges.reaches(&self.here, peer) {
            Ok(())
        } else {
            Err(PeerError::Unknown(format!("{peer} (no edge from {})", self.here)))
        }
    }

    fn map(peer: &str, e: EdgeError) -> PeerError {
        match e {
            EdgeError::Remote(body) => PeerError::Refused { peer: peer.into(), status: 502, body },
            other => PeerError::Unreachable { peer: peer.into(), reason: other.to_string() },
        }
    }
}

impl PeerTransport for EdgePeers {
    fn peers(&self) -> Vec<String> {
        self.edges.reachable_from(&self.here)
    }

    fn invoke<'a>(&'a self, peer: &'a str, name: &'a str, args: Value, ctx: &'a CallContext)
        -> BoxFuture<'a, Result<String, PeerError>> {
        Box::pin(async move {
            self.reachable(peer)?;
            let caller = Caller { node: self.here.clone(), lane: Lane::Edge, session: ctx.caller.session.clone(),
                                  trust: ctx.caller.trust.clone(), scope: ctx.caller.scope.clone() };
            let v = self.client.invoke(peer, name, args, caller).await.map_err(|e| Self::map(peer, e))?;
            Ok(match v {
                Value::String(s) => s,
                other => other.to_string(),
            })
        })
    }

    fn tools<'a>(&'a self, peer: &'a str) -> BoxFuture<'a, Result<Value, PeerError>> {
        Box::pin(async move {
            self.reachable(peer)?;
            let caller = Caller { node: self.here.clone(), lane: Lane::Edge, ..Caller::default() };
            self.client.invoke(peer, TOOLS_FRAME, Value::Null, caller).await.map_err(|e| Self::map(peer, e))
        })
    }

    fn table_changed<'a>(&'a self, peer: &'a str, node: &'a str) -> BoxFuture<'a, Result<(), PeerError>> {
        Box::pin(async move {
            self.reachable(peer)?;
            let caller = Caller { node: self.here.clone(), lane: Lane::Edge, ..Caller::default() };
            self.client
                .invoke(peer, TABLE_CHANGED_FRAME, serde_json::json!({"node": node}), caller)
                .await
                .map(|_| ())
                .map_err(|e| Self::map(peer, e))
        })
    }
}
