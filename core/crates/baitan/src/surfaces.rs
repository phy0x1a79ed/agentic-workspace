//! The caller surfaces generated from `CALLER_VERBS`: CLI subcommands, a stdio MCP server and `POST /invoke`.

use crate::client::Client;
use baitan_proto::{Msg, CALLER_VERBS};
use serde_json::{json, Value};
use std::sync::Arc;

/// Builds the typed message for a caller verb, refusing provider verbs and unknown args.
pub fn verb_msg(verb: &str, args: Value) -> Result<Msg, String> {
    if !CALLER_VERBS.iter().any(|v| v.name == verb) {
        return Err(format!("unknown verb {verb}"));
    }
    serde_json::from_value(json!({"selector": verb, "args": args})).map_err(|e| e.to_string())
}

/// The reply as the surfaces show it: `return` unwrapped, anything else as an error object.
pub fn shown(reply: Msg) -> Result<Value, Value> {
    match reply {
        Msg::Return(v) => Ok(v),
        other => Err(serde_json::to_value(other).expect("msg serializes")),
    }
}

pub async fn invoke(client: &Client, verb: &str, args: Value) -> Result<Value, Value> {
    let msg = verb_msg(verb, args).map_err(|e| json!({"selector": "raise", "args": {"name": "BadArgs", "message": e}}))?;
    shown(client.ask(msg).await.map_err(|e| json!({"selector": "raise", "args": {"name": "Unavailable", "message": e}}))?)
}

/// The verbs MCP and HTTP expose: every caller verb but the CLI-only ones.
pub fn remote_verbs() -> impl Iterator<Item = &'static baitan_proto::VerbSpec> {
    CALLER_VERBS.iter().filter(|v| !v.cli_only)
}

pub fn clap_verbs(cmd: clap::Command) -> clap::Command {
    CALLER_VERBS.iter().fold(cmd, |cmd, v| {
        let schema: Value = serde_json::from_str(v.schema).expect("catalog schema is JSON");
        let props = schema["properties"].as_object().cloned().unwrap_or_default();
        let required: Vec<&str> = schema["required"].as_array().map(|r| r.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
        let sub = props.keys().fold(clap::Command::new(v.name).about(v.summary), |sub, key| {
            let key: &'static str = Box::leak(key.clone().into_boxed_str());
            sub.arg(
                clap::Arg::new(key)
                    .long(key)
                    .required(required.contains(&key))
                    .num_args(1..)
                    .help("JSON or a plain string; @path reads a file"),
            )
        });
        cmd.subcommand(sub)
    })
}

/// The args object of a CLI verb: each flag parsed as JSON when it parses, else as a string.
pub fn clap_args(verb: &str, m: &clap::ArgMatches) -> anyhow::Result<Value> {
    let spec = CALLER_VERBS.iter().find(|v| v.name == verb).expect("clap built from catalog");
    let schema: Value = serde_json::from_str(spec.schema)?;
    let mut args = serde_json::Map::new();
    for (key, prop) in schema["properties"].as_object().into_iter().flatten() {
        let Some(vals) = m.get_many::<String>(key) else { continue };
        let vals: Vec<Value> = vals.map(|s| cli_value(s)).collect::<anyhow::Result<_>>()?;
        let value = if prop["type"] == "array" && !(vals.len() == 1 && vals[0].is_array()) { Value::Array(vals) } else { vals.into_iter().next().unwrap_or(Value::Null) };
        args.insert(key.clone(), value);
    }
    Ok(Value::Object(args))
}

fn cli_value(s: &str) -> anyhow::Result<Value> {
    let text = match s.strip_prefix('@') {
        Some(path) => std::fs::read_to_string(path)?,
        None => s.to_string(),
    };
    Ok(serde_json::from_str(&text).unwrap_or(Value::String(text)))
}

/// Serves MCP over stdio: one JSON-RPC message per line.
pub async fn mcp_stdio(client: Arc<Client>) -> anyhow::Result<()> {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let mut out = tokio::io::stdout();
    while let Some(line) = lines.next_line().await? {
        let Ok(req) = serde_json::from_str::<Value>(&line) else { continue };
        if let Some(resp) = mcp_handle(&client, &req).await {
            out.write_all(format!("{resp}\n").as_bytes()).await?;
            out.flush().await?;
        }
    }
    Ok(())
}

pub async fn mcp_handle(client: &Client, req: &Value) -> Option<Value> {
    let id = req.get("id")?.clone();
    let result = match req["method"].as_str().unwrap_or("") {
        "initialize" => json!({
            "protocolVersion": req["params"]["protocolVersion"].as_str().unwrap_or("2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "baitan", "version": env!("CARGO_PKG_VERSION")},
        }),
        "tools/list" => json!({"tools": mcp_tools()}),
        "tools/call" => {
            let name = req["params"]["name"].as_str().unwrap_or("");
            let args = req["params"].get("arguments").cloned().unwrap_or(json!({}));
            let out = if remote_verbs().any(|v| v.name == name) {
                invoke(client, name, args).await
            } else {
                Err(json!({"selector": "doesNotUnderstand", "args": {"selector": name}}))
            };
            let (text, is_error) = match out {
                Ok(v) => (v.to_string(), false),
                Err(e) => (e.to_string(), true),
            };
            json!({"content": [{"type": "text", "text": text}], "isError": is_error})
        }
        "ping" => json!({}),
        m => return Some(json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": format!("no method {m}")}})),
    };
    Some(json!({"jsonrpc": "2.0", "id": id, "result": result}))
}

pub fn mcp_tools() -> Vec<Value> {
    remote_verbs()
        .map(|v| json!({"name": v.name, "description": v.summary, "inputSchema": serde_json::from_str::<Value>(v.schema).unwrap()}))
        .collect()
}

/// `POST /invoke` with `{verb, args}`.
pub fn http_router(client: Arc<Client>) -> axum::Router {
    use axum::{http::StatusCode, routing::post, Json};
    axum::Router::new().route(
        "/invoke",
        post(move |Json(body): Json<Value>| {
            let client = client.clone();
            async move {
                let verb = body["verb"].as_str().unwrap_or("").to_string();
                if !remote_verbs().any(|v| v.name == verb) {
                    return (StatusCode::NOT_FOUND, Json(json!({"selector": "doesNotUnderstand", "args": {"selector": verb}})));
                }
                match invoke(&client, &verb, body.get("args").cloned().unwrap_or(json!({}))).await {
                    Ok(v) => (StatusCode::OK, Json(v)),
                    Err(e) => (StatusCode::UNPROCESSABLE_ENTITY, Json(e)),
                }
            }
        }),
    )
}
