//! The calls scopes makes to other plugins. Each one degrades by design, so none is a need:
//! journal writes the brief and answers `search`, reflection reports waiting sessions and
//! types a queue notice, and operations takes the hygiene report.

use std::collections::HashSet;
use std::time::Duration;

use awm_plugin_sdk::{CallOpts, Context};
use serde_json::{json, Value};

/// One call through the gateway, blocking. Swapped for a recorder in tests.
pub trait Outbound: Send + Sync {
    fn call(&self, tool: &str, verb: &str, args: Value, timeout: Duration) -> Result<Value, String>;
}

/// The gateway, called from a blocking thread.
pub struct Hub {
    pub ctx: Context,
    pub rt: tokio::runtime::Handle,
}

impl Outbound for Hub {
    fn call(&self, tool: &str, verb: &str, args: Value, timeout: Duration) -> Result<Value, String> {
        let opts = CallOpts { timeout, ..CallOpts::default() };
        self.rt.block_on(self.ctx.hub().call_with(tool, verb, args, &opts)).map_err(|e| e.to_string())
    }
}

/// Sessions the harness lists as waiting on other sessions (sessions.md R51). No answer
/// means nobody waits.
pub fn waiting_sessions(out: &dyn Outbound) -> HashSet<String> {
    let Ok(v) = out.call("reflection", "waits", json!({}), Duration::from_secs(10)) else {
        return HashSet::new();
    };
    v.get("waits")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|r| r.get("session_id").filter(|s| crate::args::truthy(s)).map(crate::args::py_str))
        .collect()
}

/// Type `text` into the waiting session, named by its canonical id. A notice that fails leaves the waiter queued.
pub fn notify(out: &dyn Outbound, session: &str, text: &str) -> bool {
    match out.call("reflection", "message", json!({"to": {"id": session}, "text": text}), Duration::from_secs(60)) {
        Ok(v) => v.get("ok") != Some(&Value::Bool(false)),
        Err(e) => {
            tracing::warn!("scopes: could not tell {session} its scope is free: {e}");
            false
        }
    }
}
