//! One JSON line each way.

use awm_proto::Caller;
use serde::{Deserialize, Serialize};
use serde_json::Value;

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
}
