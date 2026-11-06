//! One JSON line each way.
//!
//! A call is one request and one response. A stream is a request named [`WS_OPEN`] with
//! `{path}`; the far side answers with responses marked `more` (each `result` a `WsMsg`)
//! and a last one without. The caller sends into the stream with [`WS_SEND`] under the
//! stream's id, and ends it with [`WS_CLOSE`].

use awm_proto::Caller;
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const WS_OPEN: &str = "_ws";
pub const WS_SEND: &str = "_ws_send";
pub const WS_CLOSE: &str = "_ws_close";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Request {
    pub id: u64,
    pub name: String,
    #[serde(default)]
    pub args: Value,
    /// The calling session's own view. The serving node keeps only `session`
    /// and `trust`, and takes the node from the ssh key.
    #[serde(default)]
    pub caller: Caller,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Response {
    pub id: u64,
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// A stream message: more follow under this id.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub more: bool,
}
