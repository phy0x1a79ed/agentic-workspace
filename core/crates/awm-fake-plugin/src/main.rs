//! A test service that speaks the contract's wire protocol, as a non-Python plugin would.
//!
//! It publishes, under the tool `FAKE_TOOL` (default: the service name):
//! `echo(text)`, `add(a, b)`, `whoami()` (returns the `caller` frame the gateway stamped),
//! `run_job(job)` (reports a background run), and `call_needed(verb, args)` (calls the first
//! tool in `AWM_RESOLVED_TOOLS` through `/invoke`).

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite::Message;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let hub = std::env::var("AWM_HUB_URL")?.trim_end_matches('/').to_string();
    let name = std::env::var("AWM_SERVICE_NAME")?;
    let tool = std::env::var("FAKE_TOOL").unwrap_or_else(|_| name.clone());
    let plugin = std::env::var("AWM_PLUGIN").unwrap_or_else(|_| name.clone());
    let http = reqwest::Client::new();

    let reg: Value = http
        .post(format!("{hub}/hub/service/register"))
        .json(&json!({"name": name, "pid": std::process::id(), "start": ["bash", "run.sh"], "cwd": "."}))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let id = reg["service_id"].as_str().unwrap_or_default().to_string();
    let ws_url = format!("{}/hub/service/control/{id}", hub.replacen("http", "ws", 1));
    let (ws, _) = tokio_tungstenite::connect_async(&ws_url).await?;
    let (mut sink, mut stream) = ws.split();

    let f = |verb: &str, desc: &str, params: Value| json!({"name": verb, "tool": format!("{tool}_{verb}"), "description": desc, "params": params});
    let manifest = json!({"functions": [
        f("echo", "Return the text.", json!([{"name": "text", "type": "string", "required": true}])),
        f("add", "Add two numbers.", json!([{"name": "a", "type": "number", "required": true}, {"name": "b", "type": "number", "required": true}])),
        f("whoami", "The caller the gateway stamped.", json!([])),
        f("run_job", "Report one background run.", json!([{"name": "job", "type": "string", "required": true}])),
        f("call_needed", "Call the first needed tool.", json!([{"name": "verb", "required": true}, {"name": "args", "type": "object"}])),
    ]});
    sink.send(Message::Text(json!({"kind": "ready", "api": manifest}).to_string().into())).await?;

    while let Some(msg) = stream.next().await {
        let Message::Text(text) = msg? else { continue };
        let frame: Value = serde_json::from_str(&text)?;
        match frame["kind"].as_str() {
            Some("shutdown") => break,
            Some("call") => {}
            _ => continue,
        }
        let args = &frame["args"];
        let result: Result<Value, String> = match frame["fn"].as_str().unwrap_or("") {
            "echo" => Ok(args["text"].clone()),
            "add" => match (args["a"].as_f64(), args["b"].as_f64()) {
                (Some(a), Some(b)) => Ok(json!(a + b)),
                _ => Err("add needs numbers a and b".into()),
            },
            "whoami" => Ok(json!({"caller": frame["caller"], "as": frame["as"]})),
            "run_job" => {
                let usage = json!({"kind": "usage", "surface": "background", "name": args["job"], "ok": true, "ms": 5});
                sink.send(Message::Text(usage.to_string().into())).await?;
                Ok(json!("ran"))
            }
            "call_needed" => call_needed(&http, &hub, &plugin, args).await,
            other => Err(format!("unknown function {other:?}")),
        };
        let reply = match result {
            Ok(r) => json!({"kind": "reply", "id": frame["id"], "ok": true, "result": r}),
            Err(e) => json!({"kind": "reply", "id": frame["id"], "ok": false, "error": e, "error_class": "ValueError"}),
        };
        sink.send(Message::Text(reply.to_string().into())).await?;
    }
    Ok(())
}

async fn call_needed(http: &reqwest::Client, hub: &str, plugin: &str, args: &Value) -> Result<Value, String> {
    let path = std::env::var("AWM_RESOLVED_TOOLS").map_err(|e| e.to_string())?;
    let resolved: Value = serde_json::from_str(&std::fs::read_to_string(&path).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    let tool = resolved["tools"]
        .as_object()
        .and_then(|m| m.values().next())
        .and_then(|t| t["name"].as_str())
        .ok_or("no needed tool resolved")?
        .to_string();
    let body = json!({"name": tool, "args": {"verb": args["verb"], "args": args.get("args").cloned().unwrap_or(json!({}))}});
    let resp = http
        .post(format!("{hub}/invoke"))
        .header("X-Awm-Plugin", plugin)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let v: Value = resp.json().await.map_err(|e| e.to_string())?;
    Ok(v)
}
