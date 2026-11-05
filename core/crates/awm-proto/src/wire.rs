//! The caller context the gateway stamps on each call (contract § Service wire protocol).

use serde::{Deserialize, Serialize};

use crate::usage::Surface;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Lane {
    #[default]
    Local,
    Edge,
    Service,
}

/// The `caller` object of a `call` frame. Only the gateway fills it (R103).
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(default)]
pub struct Caller {
    pub node: String,
    pub lane: Lane,
    pub session: Option<String>,
    pub trust: Option<String>,
    /// The calling session's own `<project>/<scope>`.
    pub scope: Option<String>,
    /// The node a relayed call started from, when it is not `node` (fleet.md R98 item 3).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub origin: Option<String>,
}

impl Caller {
    /// The nodes behind this call other than `here`: the node it came from, and the one it
    /// started from when another node relays it.
    pub fn nodes_behind<'a>(&'a self, here: &'a str) -> impl Iterator<Item = &'a str> + 'a {
        std::iter::once(self.node.as_str()).chain(self.origin.as_deref()).filter(move |n| *n != here)
    }

    /// The origin a call carries on when this node relays it: its own, or the node it came from.
    pub fn relayed_origin(&self, here: &str) -> Option<String> {
        self.origin.clone().or_else(|| (self.node != here).then(|| self.node.clone()))
    }
}

/// What the gateway knows about one inbound call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallContext {
    pub caller: Caller,
    /// The `X-Awm-As` principal, when the caller acts for someone.
    pub as_: Option<String>,
    pub surface: Surface,
    /// The calling plugin, known by its service token or by the path of the page that sent
    /// the call. Its manifest's needs bound what it may call (R116 item 3).
    pub plugin: Option<String>,
    /// The calling session's mode, when a restricted mode may gate it (`config/modes.py`).
    pub mode: Option<String>,
    /// The pid of the calling session on this node, which a service reads as `_caller_pid`.
    pub pid: Option<u32>,
}

impl CallContext {
    pub fn local(node: impl Into<String>, surface: Surface) -> Self {
        Self {
            caller: Caller { node: node.into(), ..Caller::default() },
            as_: None,
            surface,
            plugin: None,
            mode: None,
            pid: None,
        }
    }
}

/// Headers a caller may not set: the gateway owns them.
pub const GATEWAY_OWNED_HEADERS: [&str; 2] = ["x-awm-trust", "x-awm-lane"];

/// One WebSocket message, as an emit stream or a session carries it across the edge.
/// It travels as `{"text": ...}`, `{"b64": ...}` for binary, or `{"close": reason}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WsMsg {
    Text(String),
    Binary(Vec<u8>),
    Close(Option<String>),
}

impl Serialize for WsMsg {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        use base64::Engine;
        use serde::ser::SerializeMap;
        let mut m = s.serialize_map(Some(1))?;
        match self {
            WsMsg::Text(t) => m.serialize_entry("text", t)?,
            WsMsg::Binary(b) => m.serialize_entry("b64", &base64::engine::general_purpose::STANDARD.encode(b))?,
            WsMsg::Close(r) => m.serialize_entry("close", r)?,
        }
        m.end()
    }
}

impl<'de> Deserialize<'de> for WsMsg {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        use base64::Engine;
        use serde::de::Error;
        let v = serde_json::Value::deserialize(d)?;
        if let Some(t) = v.get("text").and_then(|t| t.as_str()) {
            return Ok(WsMsg::Text(t.to_string()));
        }
        if let Some(b) = v.get("b64").and_then(|b| b.as_str()) {
            return base64::engine::general_purpose::STANDARD.decode(b).map(WsMsg::Binary).map_err(D::Error::custom);
        }
        if let Some(r) = v.get("close") {
            return Ok(WsMsg::Close(r.as_str().map(str::to_string)));
        }
        Err(D::Error::custom("a WebSocket message is {text}, {b64} or {close}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ws_messages_round_trip() {
        for m in [WsMsg::Text("hi".into()), WsMsg::Binary(vec![0, 255, 7]), WsMsg::Close(Some("done".into())), WsMsg::Close(None)] {
            let line = serde_json::to_string(&m).unwrap();
            assert_eq!(serde_json::from_str::<WsMsg>(&line).unwrap(), m, "{line}");
        }
    }
}
