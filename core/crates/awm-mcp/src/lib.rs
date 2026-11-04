//! `awm mcp`: a stateless stdio MCP server. Each `tools/list` reads the gateway's live
//! catalog, and each `tools/call` posts to `/invoke`, so the gateway can restart under it.
//! Once the client has listed the tools, the proxy rereads the catalog every `POLL` and
//! sends `notifications/tools/list_changed` when it differs, so a hot reload (R144) reaches
//! the session without a restart.

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::Mutex;

const PROTOCOL: &str = "2025-06-18";
const POLL: Duration = Duration::from_secs(15);
const LIST_CHANGED: &str = "notifications/tools/list_changed";

pub struct Proxy {
    url: String,
    client: reqwest::Client,
    /// A digest of the tool list the client last got. `None` until it lists them.
    listed: std::sync::Mutex<Option<u64>>,
}

fn digest(tools: &Value) -> u64 {
    let mut h = DefaultHasher::new();
    tools.to_string().hash(&mut h);
    h.finish()
}

impl Proxy {
    pub fn new(url: impl Into<String>) -> Self {
        Self {
            url: url.into().trim_end_matches('/').to_string(),
            client: reqwest::Client::new(),
            listed: std::sync::Mutex::new(None),
        }
    }

    /// Whether the catalog differs from the list the client last got. A gateway that does not
    /// answer is no change: the client keeps its list until the gateway is back.
    pub async fn list_changed(&self) -> bool {
        let Some(seen) = *self.listed.lock().unwrap_or_else(|p| p.into_inner()) else { return false };
        match self.fetch().await {
            Ok(tools) => digest(&tools) != seen,
            Err(_) => false,
        }
    }

    /// Answer one JSON-RPC message. A notification gets no answer.
    pub async fn handle(&self, msg: &Value) -> Option<Value> {
        let id = msg.get("id").cloned();
        let method = msg.get("method").and_then(Value::as_str).unwrap_or("");
        let id = id?;
        let result = match method {
            "initialize" => Ok(json!({
                "protocolVersion": msg.pointer("/params/protocolVersion").and_then(Value::as_str).unwrap_or(PROTOCOL),
                "capabilities": {"tools": {"listChanged": true}},
                "serverInfo": {"name": "awm", "version": env!("CARGO_PKG_VERSION")},
            })),
            "ping" => Ok(json!({})),
            "tools/list" => self.list().await,
            "tools/call" => Ok(self.call(msg.get("params").unwrap_or(&Value::Null)).await),
            other => Err((-32601, format!("method {other:?} not found"))),
        };
        Some(match result {
            Ok(r) => json!({"jsonrpc": "2.0", "id": id, "result": r}),
            Err((code, message)) => json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}),
        })
    }

    async fn list(&self) -> Result<Value, (i64, String)> {
        let tools = self.fetch().await?;
        *self.listed.lock().unwrap_or_else(|p| p.into_inner()) = Some(digest(&tools));
        Ok(json!({"tools": tools}))
    }

    async fn fetch(&self) -> Result<Value, (i64, String)> {
        let resp = self
            .client
            .get(format!("{}/tools", self.url))
            .query(&[("view", "domains"), ("peers", "1")])
            .send()
            .await
            .map_err(|e| (-32000, format!("awm gateway unreachable at {}: {e}", self.url)))?;
        let body: Value = resp.json().await.map_err(|e| (-32000, e.to_string()))?;
        Ok(body.get("tools").cloned().unwrap_or(json!([])))
    }

    async fn call(&self, params: &Value) -> Value {
        let name = params.get("name").and_then(Value::as_str).unwrap_or("");
        let args = params.get("arguments").cloned().unwrap_or(json!({}));
        // SAFETY: getppid has no preconditions.
        let ppid = unsafe { libc::getppid() };
        let sent = self
            .client
            .post(format!("{}/invoke", self.url))
            .header("X-Awm-Surface", "mcp")
            .header("X-Awm-Session-Pid", ppid.to_string())
            .json(&json!({"name": name, "args": args}))
            .send()
            .await;
        let (text, is_error) = match sent {
            Err(e) => (format!("awm gateway unreachable at {}: {e}", self.url), true),
            Ok(resp) => {
                let ok = resp.status().is_success();
                let status = resp.status().as_u16();
                let body: Value = resp.json().await.unwrap_or(Value::Null);
                if ok {
                    (body.get("result").and_then(Value::as_str).unwrap_or_default().to_string(), false)
                } else {
                    let detail = body.get("detail").cloned().unwrap_or(body);
                    let text = match detail {
                        Value::String(s) => s,
                        other => other.to_string(),
                    };
                    (format!("Error {status}: {text}"), true)
                }
            }
        };
        json!({"content": [{"type": "text", "text": text}], "isError": is_error})
    }
}

/// Serve MCP over stdin and stdout, one JSON message per line.
pub async fn run_stdio(url: &str) -> anyhow::Result<()> {
    let proxy = Arc::new(Proxy::new(url));
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let out = Arc::new(Mutex::new(tokio::io::stdout()));
    let watcher = tokio::spawn({
        let (proxy, out) = (proxy.clone(), out.clone());
        async move {
            loop {
                tokio::time::sleep(POLL).await;
                if proxy.list_changed().await {
                    // The client lists again on the notification; until then it is not told twice.
                    *proxy.listed.lock().unwrap_or_else(|p| p.into_inner()) = None;
                    let note = json!({"jsonrpc": "2.0", "method": LIST_CHANGED});
                    let mut out = out.lock().await;
                    if out.write_all(format!("{note}\n").as_bytes()).await.is_err() || out.flush().await.is_err() {
                        return;
                    }
                }
            }
        }
    });
    while let Some(line) = lines.next_line().await? {
        if line.trim().is_empty() {
            continue;
        }
        let reply = match serde_json::from_str::<Value>(&line) {
            Ok(msg) => proxy.handle(&msg).await,
            Err(e) => Some(json!({"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": e.to_string()}})),
        };
        if let Some(reply) = reply {
            let mut out = out.lock().await;
            out.write_all(format!("{reply}\n").as_bytes()).await?;
            out.flush().await?;
        }
    }
    watcher.abort();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn no_change_before_the_client_lists() {
        // Nothing listens on port 9, and no list was handed out yet.
        let p = Proxy::new("http://127.0.0.1:9");
        assert!(!p.list_changed().await);
        *p.listed.lock().unwrap() = Some(digest(&json!([])));
        assert!(!p.list_changed().await, "an unreachable gateway is no change");
    }

    #[test]
    fn the_digest_follows_the_list() {
        assert_eq!(digest(&json!([{"name": "a"}])), digest(&json!([{"name": "a"}])));
        assert_ne!(digest(&json!([{"name": "a"}])), digest(&json!([{"name": "b"}])));
    }
}
