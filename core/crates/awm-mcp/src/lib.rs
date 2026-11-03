//! `awm mcp`: a stateless stdio MCP server. Each `tools/list` reads the gateway's live
//! catalog, and each `tools/call` posts to `/invoke`, so the gateway can restart under it.

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

const PROTOCOL: &str = "2025-06-18";

pub struct Proxy {
    url: String,
    client: reqwest::Client,
}

impl Proxy {
    pub fn new(url: impl Into<String>) -> Self {
        Self { url: url.into().trim_end_matches('/').to_string(), client: reqwest::Client::new() }
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
        let resp = self
            .client
            .get(format!("{}/tools", self.url))
            .query(&[("view", "domains"), ("peers", "1")])
            .send()
            .await
            .map_err(|e| (-32000, format!("awm gateway unreachable at {}: {e}", self.url)))?;
        let body: Value = resp.json().await.map_err(|e| (-32000, e.to_string()))?;
        Ok(json!({"tools": body.get("tools").cloned().unwrap_or(json!([]))}))
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
    let proxy = Proxy::new(url);
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let mut out = tokio::io::stdout();
    while let Some(line) = lines.next_line().await? {
        if line.trim().is_empty() {
            continue;
        }
        let reply = match serde_json::from_str::<Value>(&line) {
            Ok(msg) => proxy.handle(&msg).await,
            Err(e) => Some(json!({"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": e.to_string()}})),
        };
        if let Some(reply) = reply {
            out.write_all(format!("{reply}\n").as_bytes()).await?;
            out.flush().await?;
        }
    }
    Ok(())
}
