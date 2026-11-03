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
pub struct Caller {
    pub node: String,
    pub lane: Lane,
    pub session: Option<String>,
    pub trust: Option<String>,
}

/// What the gateway knows about one inbound call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallContext {
    pub caller: Caller,
    /// The `X-Awm-As` principal, when the caller acts for someone.
    pub as_: Option<String>,
    pub surface: Surface,
    /// The calling plugin, from `X-Awm-Plugin`.
    pub plugin: Option<String>,
}

impl CallContext {
    pub fn local(node: impl Into<String>, surface: Surface) -> Self {
        Self {
            caller: Caller { node: node.into(), ..Caller::default() },
            as_: None,
            surface,
            plugin: None,
        }
    }
}

/// Headers a caller may not set: the gateway owns them.
pub const GATEWAY_OWNED_HEADERS: [&str; 2] = ["x-awm-trust", "x-awm-lane"];
