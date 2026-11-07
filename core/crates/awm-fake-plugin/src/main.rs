//! A test service that speaks the contract's wire protocol through `awm-plugin-sdk`, as a
//! Rust core plugin does.
//!
//! It publishes, under the tool `FAKE_TOOL` (default: the service name):
//! `echo(text)`, `add(a, b)`, `whoami()` (returns the `caller` frame the gateway stamped),
//! `run_job(job)` (reports a background run), and `call_needed(verb, args)` (calls the first
//! `[[needs.tool]]` of its own `awm-plugin.toml` by name through `/invoke`, with its service token). As a fake harness plugin it also answers
//! `caller_of_token(token)` from the JSON map in `FAKE_TOKENS`, and `scoped(project?, scope?)`
//! returns the args it got. `env(name)` returns one variable of its environment.
//! As a fake reflection it answers `session_of(pid)` from the JSON map in the file at
//! `FAKE_SESSIONS_FILE`, read on each call. As a fake approver, `approval_request` writes its
//! request to the file at `FAKE_APPROVAL_FILE` and `approval_get` answers that file.
//! `emit(topic, payload)` sends one emit frame. It serves the direct session kind `echo`:
//! its bridge sends `{"init": <init>}` first, then echoes each message. With `FAKE_MOUNT` set
//! it registers an HTTP mount that answers `mount <method> <path>`.

use awm_plugin_sdk::axum::{http::Method, http::Uri, Router};
use awm_plugin_sdk::{Call, CallOpts, Service, Session, VerbError, WsMsg};
use serde_json::{json, Value};

type Reply = Result<Value, VerbError>;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let svc = Service::from_env()?;
    let tool = std::env::var("FAKE_TOOL").unwrap_or_else(|_| svc.context().env().service.clone());
    let tokens: Value = std::env::var("FAKE_TOKENS").ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(json!({}));
    let t = tool.as_str();
    let mut svc = svc
        .verb(t, "echo", "Return the text.", json!([{"name": "text", "type": "string", "required": true}]), |c: Call| async move {
            Reply::Ok(c.args["text"].clone())
        })
        .verb(t, "add", "Add two numbers.", json!([{"name": "a", "type": "number", "required": true}, {"name": "b", "type": "number", "required": true}]),
            |c: Call| async move {
                match (c.args["a"].as_f64(), c.args["b"].as_f64()) {
                    (Some(a), Some(b)) => Ok(json!(a + b)),
                    _ => Err(VerbError::value("add needs numbers a and b")),
                }
            })
        .verb(t, "whoami", "The caller the gateway stamped.", json!([]), |c: Call| async move {
            let a = &c.args;
            Reply::Ok(json!({"caller": c.caller, "as": c.as_, "trust": a["_caller_trust"], "scope": a["_caller_scope"],
                "session_id": a["_caller_session_id"], "pid": a["_caller_pid"], "claim": a["_caller_session"]}))
        })
        // A hook-only verb: off the MCP surface, on the CLI and HTTP.
        .verb(t, "run_job", "Report one background run.", json!([{"name": "job", "type": "string", "required": true}]), |c: Call| async move {
            c.ctx.usage(c.str_arg("job").unwrap_or_default(), true, 5);
            Reply::Ok(json!("ran"))
        })
        .hidden()
        .verb(t, "call_needed", "Call the first needed tool.", json!([{"name": "verb", "required": true}, {"name": "args", "type": "object"}]), call_needed)
        .verb(t, "caller_of_token", "The isolated session that holds a caller token.", json!([{"name": "token", "required": true}]), move |c: Call| {
            let rec = c.str_arg("token").and_then(|t| tokens.get(t)).and_then(Value::as_object).cloned();
            async move {
                Reply::Ok(match rec {
                    Some(mut answer) => {
                        answer.insert("ok".into(), json!(true));
                        answer.insert("known".into(), json!(true));
                        Value::Object(answer)
                    }
                    None => json!({"ok": true, "known": false}),
                })
            }
        })
        .verb(t, "scoped", "Return the args as received.", json!([{"name": "project"}, {"name": "scope"}]), |c: Call| async move { Reply::Ok(c.args) })
        .verb(t, "env", "One variable of the service's environment, or null.", json!([{"name": "name", "required": true}]), |c: Call| async move {
            Reply::Ok(c.str_arg("name").and_then(|n| std::env::var(n).ok()).map(Value::from).unwrap_or(Value::Null))
        })
        .verb(t, "session_of", "The session that owns a pid.", json!([{"name": "pid", "type": "integer", "required": true}]), |c: Call| async move {
            Reply::Ok(
                std::env::var("FAKE_SESSIONS_FILE")
                    .ok()
                    .and_then(|p| std::fs::read_to_string(p).ok())
                    .and_then(|t| serde_json::from_str::<Value>(&t).ok())
                    .and_then(|m| m.get(c.args["pid"].to_string()).cloned())
                    .unwrap_or_else(|| json!({"ok": true, "known": false, "mode": null})),
            )
        })
        .verb(t, "approval_request", "Record an approval request.",
            json!([{"name": "kind", "required": true}, {"name": "node"}, {"name": "reason"}, {"name": "payload", "type": "object"}]),
            |c: Call| async move {
                let a = &c.args;
                let asked = json!({"request_id": "qfake", "status": "pending", "kind": a["kind"], "node": a["node"], "payload": a["payload"]});
                let file = std::env::var("FAKE_APPROVAL_FILE").map_err(|e| VerbError::value(e.to_string()))?;
                std::fs::write(file, asked.to_string()).map_err(|e| VerbError::value(e.to_string()))?;
                Reply::Ok(json!({"request_id": "qfake", "task_id": "t1"}))
            })
        .verb(t, "approval_get", "The recorded approval.", json!([{"name": "request_id", "required": true}]), |_c: Call| async move {
            let file = std::env::var("FAKE_APPROVAL_FILE").map_err(|e| VerbError::value(e.to_string()))?;
            let text = std::fs::read_to_string(file).map_err(|e| VerbError::value(e.to_string()))?;
            serde_json::from_str::<Value>(&text).map_err(|e| VerbError::value(e.to_string()))
        })
        .verb(t, "emit", "Emit one payload on a topic.", json!([{"name": "topic", "required": true}, {"name": "payload"}]), |c: Call| async move {
            c.ctx.emit(c.str_arg("topic").unwrap_or_default(), &c.args["payload"]);
            Reply::Ok(json!("emitted"))
        })
        .session("echo", echo_session);
    if std::env::var_os("FAKE_MOUNT").is_some() {
        svc = svc.mount(Router::new().fallback(|method: Method, uri: Uri| async move {
            format!("mount {method} {}", uri.path_and_query().map(|p| p.as_str()).unwrap_or("?"))
        }));
    }
    svc.run().await
}

async fn echo_session(session: Session) -> anyhow::Result<()> {
    let mut bridge = session.open_bridge().await?;
    bridge.send_json(&json!({"init": session.init})).await?;
    while let Some(msg) = bridge.recv().await {
        if matches!(msg, WsMsg::Close(_)) || bridge.send(msg).await.is_err() {
            break;
        }
    }
    Ok(())
}

/// Calls the first `[[needs.tool]]` and answers the gateway's reply body as it came, error or not.
async fn call_needed(c: Call) -> Reply {
    let text = std::fs::read_to_string("awm-plugin.toml").map_err(|e| VerbError::value(format!("awm-plugin.toml: {e}")))?;
    let manifest: toml::Value = toml::from_str(&text).map_err(|e| VerbError::value(e.to_string()))?;
    let tool = manifest
        .get("needs")
        .and_then(|n| n.get("tool"))
        .and_then(|t| t.get(0))
        .and_then(|t| t.get("name"))
        .and_then(|n| n.as_str())
        .ok_or_else(|| VerbError::value("the manifest needs no tool"))?
        .to_string();
    let body = json!({"name": tool, "args": {"verb": c.args["verb"], "args": c.args.get("args").cloned().unwrap_or(json!({}))}});
    let (_, reply) = c.hub().invoke_raw(&body, &CallOpts::default()).await.map_err(|e| VerbError::value(e.to_string()))?;
    Ok(reply)
}
