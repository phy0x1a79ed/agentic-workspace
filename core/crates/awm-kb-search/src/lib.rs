//! search, kb's federation layer. It holds no content: it parses the query once, embeds its
//! text once when a named source takes vectors, builds the caller's veil from the private marks,
//! and asks each named source in parallel, each within its own timeout. kb's own sources answer
//! in-process through [`awm_kb_source::Source`]. A source named `<kind>@<node>` is asked of that
//! node's search, and a bare kind this node does not serve goes to the first peer that does.
//!
//! Its database keeps only the judgements and the usage log, in place at
//! `<AWM_DIR>/services/search/search.db`.

pub mod federation;
pub mod hits;
pub mod request;
pub mod search;
pub mod store;
pub mod verbs;

pub use federation::{HubPeers, Peers};
pub use search::{Gateway, HubGateway, Search};
pub use verbs::register;

use std::future::Future;
use std::pin::Pin;

pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// A failed call to another plugin or node, as the Python named it: the exception's class and
/// its text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallError {
    pub class: String,
    pub message: String,
}

impl CallError {
    pub fn new(class: &str, message: impl Into<String>) -> Self {
        CallError { class: class.into(), message: message.into() }
    }

    /// `describe_error`: one line of at most 300 characters, for a source's status.
    pub fn describe(&self) -> String {
        let line = if self.message.is_empty() { self.class.clone() } else { format!("{}: {}", self.class, self.message) };
        line.chars().take(300).collect()
    }
}

impl std::fmt::Display for CallError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl From<awm_plugin_sdk::HubError> for CallError {
    fn from(e: awm_plugin_sdk::HubError) -> Self {
        match &e {
            awm_plugin_sdk::HubError::Verb { error, error_class, .. } => {
                CallError::new(error_class.as_deref().unwrap_or("RuntimeError"), error.clone())
            }
            awm_plugin_sdk::HubError::Transport { .. } => CallError::new("ConnectionError", e.to_string()),
            _ => CallError::new("ServiceCallError", e.to_string()),
        }
    }
}

/// A verb's failure: the Python exception's class and its text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fail {
    pub class: String,
    pub message: String,
}

impl Fail {
    pub fn new(class: &str, message: impl Into<String>) -> Self {
        Fail { class: class.into(), message: message.into() }
    }

    pub fn value(message: impl Into<String>) -> Self {
        Fail::new("ValueError", message)
    }
}

impl std::fmt::Display for Fail {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl From<CallError> for Fail {
    fn from(e: CallError) -> Self {
        Fail { class: e.class, message: e.message }
    }
}

impl From<awm_store::StoreError> for Fail {
    fn from(e: awm_store::StoreError) -> Self {
        Fail::new("OperationalError", e.to_string())
    }
}

impl From<awm_index::QueryError> for Fail {
    fn from(e: awm_index::QueryError) -> Self {
        Fail::new("QueryError", e.0)
    }
}

impl From<Fail> for awm_plugin_sdk::VerbError {
    fn from(f: Fail) -> Self {
        awm_plugin_sdk::VerbError::new(f.class, f.message)
    }
}
