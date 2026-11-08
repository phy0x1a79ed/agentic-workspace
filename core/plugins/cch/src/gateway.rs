//! The gateway calls cch makes as a plain caller. Each caller degrades when a call fails, so cch
//! declares no needs.

use std::future::Future;
use std::time::Duration;

use awm_plugin_sdk::{CallOpts, Hub};
use serde_json::Value;

/// One call through the gateway: `<tool> <verb>` with `args`, on `peer` when set.
#[derive(Debug, Clone, PartialEq)]
pub struct Call {
    pub tool: String,
    pub verb: String,
    pub args: Value,
    pub peer: Option<String>,
    pub timeout: Option<Duration>,
}

impl Call {
    /// A local call with the gateway's default timeout.
    pub fn new(tool: &str, verb: &str, args: Value) -> Self {
        Self { tool: tool.into(), verb: verb.into(), args, peer: None, timeout: None }
    }

    /// The same call with a timeout.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }

    /// The same call on `peer`.
    pub fn peer(mut self, peer: Option<String>) -> Self {
        self.peer = peer;
        self
    }
}

/// Makes a gateway call. The error is the text a caller puts in a warning or a refusal.
pub trait Gateway: Send + Sync {
    fn call(&self, call: Call) -> impl Future<Output = Result<Value, String>> + Send;
}

impl Gateway for Hub {
    async fn call(&self, call: Call) -> Result<Value, String> {
        let mut opts = CallOpts { peer: call.peer, ..CallOpts::default() };
        if let Some(timeout) = call.timeout {
            opts.timeout = timeout;
        }
        self.call_with(&call.tool, &call.verb, call.args, &opts).await.map_err(|e| e.to_string())
    }
}

/// Test fakes.
pub mod testing {
    use std::collections::VecDeque;
    use std::sync::Mutex;

    use super::*;

    type Queue = VecDeque<Result<Value, String>>;

    /// Records each call and answers from a queue per `tool verb`, else `{ok: true}`.
    #[derive(Default)]
    pub struct FakeGateway {
        pub calls: Mutex<Vec<Call>>,
        answers: Mutex<Vec<(String, Queue)>>,
    }

    impl FakeGateway {
        /// Queue `answer` for the next `tool verb` call.
        pub fn answer(&self, tool: &str, verb: &str, answer: Result<Value, String>) {
            let key = format!("{tool} {verb}");
            let mut all = self.answers.lock().unwrap();
            match all.iter_mut().find(|(k, _)| *k == key) {
                Some((_, q)) => q.push_back(answer),
                None => all.push((key, VecDeque::from([answer]))),
            }
        }

        /// The calls so far, as `tool verb`.
        pub fn names(&self) -> Vec<String> {
            self.calls.lock().unwrap().iter().map(|c| format!("{} {}", c.tool, c.verb)).collect()
        }
    }

    impl Gateway for FakeGateway {
        async fn call(&self, call: Call) -> Result<Value, String> {
            let key = format!("{} {}", call.tool, call.verb);
            self.calls.lock().unwrap().push(call);
            let mut all = self.answers.lock().unwrap();
            all.iter_mut()
                .find(|(k, _)| *k == key)
                .and_then(|(_, q)| q.pop_front())
                .unwrap_or_else(|| Ok(serde_json::json!({"ok": true})))
        }
    }
}
