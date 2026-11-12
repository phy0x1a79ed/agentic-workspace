//! The gateway's [`PeerTransport`] over the edges: a node reaches only the peers
//! its edge list gives it an edge to, each over its warm ssh channel. A retiring
//! node and a node that runs no awm are never dialed.

use awm_gateway::peer::{stream_path_allowed, BoxFuture, PeerError, PeerTransport, WsPipe};
use awm_proto::{CallContext, Caller, Lane, Surface};
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
        if self.edges.retiring(peer) {
            return Err(PeerError::Refused {
                peer: peer.into(),
                status: 403,
                body: format!("{peer} is retiring: no call dials a retiring node"),
            });
        }
        if self.edges.undialable(peer) {
            return Err(PeerError::Refused { peer: peer.into(), status: 400, body: format!("{peer} runs no awm") });
        }
        if self.edges.reaches(&self.here, peer) {
            Ok(())
        } else {
            Err(PeerError::Unknown(format!("{peer} (no edge from {})", self.here)))
        }
    }

    /// This node as the caller, carrying the node a relayed call started from.
    fn caller(&self, ctx: &CallContext) -> Caller {
        Caller { node: self.here.clone(), lane: Lane::Edge, session: ctx.caller.session.clone(), trust: ctx.caller.trust.clone(),
                 scope: ctx.caller.scope.clone(), origin: ctx.caller.relayed_origin(&self.here),
                 name: ctx.caller.name.clone(), kind: ctx.caller.kind.clone() }
    }

    /// Whether this call may wake a sleeping peer: only a session's or the CLI's own call that
    /// names the peer. A service's fan-out, a page and table upkeep never wake one.
    fn wakes(ctx: &CallContext) -> bool {
        matches!(ctx.surface, Surface::Mcp | Surface::Cli)
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
        self.edges.dialable_from(&self.here)
    }

    fn invoke<'a>(&'a self, peer: &'a str, name: &'a str, args: Value, ctx: &'a CallContext)
        -> BoxFuture<'a, Result<String, PeerError>> {
        Box::pin(async move {
            self.reachable(peer)?;
            let caller = self.caller(ctx);
            let v = self.client.invoke(peer, name, args, caller, Self::wakes(ctx)).await.map_err(|e| Self::map(peer, e))?;
            Ok(match v {
                Value::String(s) => s,
                other => other.to_string(),
            })
        })
    }

    fn open_ws<'a>(&'a self, peer: &'a str, path: &'a str, ctx: &'a CallContext) -> BoxFuture<'a, Result<WsPipe, PeerError>> {
        Box::pin(async move {
            self.reachable(peer)?;
            if !stream_path_allowed(path) {
                return Err(PeerError::Refused { peer: peer.into(), status: 403, body: format!("{path} is not a stream path") });
            }
            let caller = self.caller(ctx);
            self.client.open_ws(peer, path, caller, Self::wakes(ctx)).await.map_err(|e| Self::map(peer, e))
        })
    }

    fn tools<'a>(&'a self, peer: &'a str) -> BoxFuture<'a, Result<Value, PeerError>> {
        Box::pin(async move {
            self.reachable(peer)?;
            let caller = Caller { node: self.here.clone(), lane: Lane::Edge, ..Caller::default() };
            self.client.invoke(peer, TOOLS_FRAME, Value::Null, caller, false).await.map_err(|e| Self::map(peer, e))
        })
    }

    fn table_changed<'a>(&'a self, peer: &'a str, node: &'a str) -> BoxFuture<'a, Result<(), PeerError>> {
        Box::pin(async move {
            self.reachable(peer)?;
            let caller = Caller { node: self.here.clone(), lane: Lane::Edge, ..Caller::default() };
            self.client
                .invoke(peer, TABLE_CHANGED_FRAME, serde_json::json!({"node": node}), caller, false)
                .await
                .map(|_| ())
                .map_err(|e| Self::map(peer, e))
        })
    }
}
