//! The service's shared state: the gateway, the node, the trust policy, the state files and the
//! provider table, plus the small JSON readers every verb uses.

use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};

use serde_json::{Map, Value};

use crate::gateway::{Gateway, Node};
use crate::providers::{HarnessError, Table};
use crate::store::Store;
use crate::trust::TrustLib;

/// Why a verb did not run: no harness could do it, or the caller's trust forbids it. The verb
/// answers `{ok: false, error}` for either.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum Failure {
    #[error(transparent)]
    Harness(#[from] HarnessError),
    #[error("{0}")]
    Refused(String),
}

/// reflection, as every verb and background task reaches it.
pub struct App<G: Gateway> {
    pub gw: G,
    pub node: Arc<dyn Node>,
    pub policy: Arc<dyn TrustLib>,
    pub store: Store,
    pub(crate) table: Table,
    pub(crate) sweeping: AtomicBool,
    /// Held while a report of the live sessions runs, so a stale list never lands after a newer one.
    pub(crate) reporting: tokio::sync::Mutex<()>,
    background: Mutex<Vec<tokio::task::JoinHandle<()>>>,
}

impl<G: Gateway> App<G> {
    pub fn new(gw: G, node: Arc<dyn Node>, policy: Arc<dyn TrustLib>, store: Store) -> Self {
        Self { gw, node, policy, store, table: Table::default(), sweeping: AtomicBool::new(false), reporting: tokio::sync::Mutex::default(), background: Mutex::default() }
    }

    /// Run `work` detached from the verb that started it, such as a retire's notices to peers.
    pub fn detach(&self, work: impl std::future::Future<Output = ()> + Send + 'static) {
        let mut all = self.background.lock().unwrap_or_else(|p| p.into_inner());
        all.retain(|h| !h.is_finished());
        all.push(tokio::spawn(work));
    }

    /// Wait until the detached work started so far has finished.
    pub async fn settle(&self) {
        loop {
            let next = self.background.lock().unwrap_or_else(|p| p.into_inner()).pop();
            match next {
                Some(h) => {
                    let _ = h.await;
                }
                None => return,
            }
        }
    }
}

/// Python's truthiness of an argument or a field.
pub fn truthy(v: Option<&Value>) -> bool {
    v.is_some_and(awm_pycompat::repr::truthy)
}

/// `out.get("ok")`: false unless the answer says ok.
pub fn is_ok(v: &Value) -> bool {
    truthy(v.get("ok"))
}

/// `out.get("ok", True)`: true unless the answer says otherwise.
pub fn ok_or_absent(v: &Value) -> bool {
    v.get("ok").is_none_or(|o| truthy(Some(o)))
}

/// `out.get(key) is False`.
pub fn is_false(v: &Value, key: &str) -> bool {
    v.get(key) == Some(&Value::Bool(false))
}

/// A string field that is present and not empty.
pub fn text<'a>(v: &'a Value, key: &str) -> Option<&'a str> {
    v.get(key).and_then(Value::as_str).filter(|s| !s.is_empty())
}

/// The object behind `v`, or an empty one.
pub fn object(v: &Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

/// `{**base, **extra}`.
pub fn merged(base: &Value, extra: &Value) -> Value {
    let mut out = object(base);
    if let Some(m) = extra.as_object() {
        for (k, v) in m {
            out.insert(k.clone(), v.clone());
        }
    }
    Value::Object(out)
}

/// The args without the gateway's stamps or any other `_` key.
pub fn declared(args: &Value) -> Map<String, Value> {
    object(args).into_iter().filter(|(k, _)| !k.starts_with('_')).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn truthiness_follows_python() {
        for v in [json!(null), json!(false), json!(0), json!(""), json!([]), json!({})] {
            assert!(!truthy(Some(&v)), "{v}");
        }
        for v in [json!(true), json!(1), json!("false"), json!([0]), json!({"a": 1})] {
            assert!(truthy(Some(&v)), "{v}");
        }
        assert!(ok_or_absent(&json!({})) && !ok_or_absent(&json!({"ok": false})) && !is_ok(&json!({})));
    }
}
