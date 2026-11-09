//! The rlm-browser realm service: a Chrome session pool that web-game effectors drive.
//!
//! The lifecycle, tab and perceive verbs are fixed. The act surface is one verb, `cdp`, which
//! relays any DevTools method. `commands` returns the running Chrome's own catalog.

mod engine;
mod manifest;
mod store;

use std::sync::Arc;

use awm_plugin_sdk::{Call, Level, Service, VerbError};
use serde_json::{json, Value};

use crate::engine::{norm_mode, Config, EngineError, Manager};

const TOOL: &str = "rlm-browser";
/// The peer that owns the attached Chrome, on a node that borrows it (FEDERATION.md § Singletons).
const ATTACH_PEER_ENV: &str = "AWM_RLM_BROWSER_ATTACH_PEER";

fn answer(r: Result<Value, EngineError>) -> Result<Value, VerbError> {
    r.map_err(|e| match e {
        EngineError::Value(m) => VerbError::value(m),
        EngineError::Runtime(m) => VerbError::runtime(m),
    })
}

/// The level an `acquire` runs at. A forwarding node passes its caller's level in
/// `opts.level`, which can lower the stamped level and never raise it.
fn acquire_level(call: &Call) -> Level {
    let stamped = call.level();
    match call.args.get("opts").and_then(|o| o.get("level")).and_then(Value::as_str).map(|l| l.parse::<Level>()) {
        Some(Ok(l)) => stamped.min(l),
        Some(Err(_)) => Level::Untrusted,
        None => stamped,
    }
}

async fn forward(call: &Call, peer: &str, verb: &str, args: Value) -> Result<Value, VerbError> {
    call.hub().call(TOOL, verb, args, Some(peer)).await.map_err(|e| VerbError::runtime(e.to_string()))
}

/// The peer a session lives on, when this node forwarded it there.
fn routed(m: &Manager, call: &Call) -> Option<String> {
    call.str_arg("session_id").and_then(|id| m.forwarded_peer(id))
}

async fn handle(m: Arc<Manager>, call: Call) -> Result<Value, VerbError> {
    let id = || call.req::<String>("session_id");
    if call.verb != "acquire" {
        if let Some(peer) = routed(&m, &call) {
            let out = forward(&call, &peer, &call.verb, call.declared_args()).await?;
            if call.verb == "release" {
                m.forget_forwarded(&id()?);
            }
            return Ok(out);
        }
    }
    let out = match call.verb.as_str() {
        "acquire" => {
            let mode = norm_mode(call.str_arg("mode"));
            let level = acquire_level(&call);
            if mode == "attached" {
                if let Some(peer) = awm_plugin_sdk::config::peer_env(ATTACH_PEER_ENV) {
                    let mut args = call.declared_args();
                    let mut opts = args.get("opts").cloned().filter(Value::is_object).unwrap_or(json!({}));
                    opts["level"] = json!(level.as_str());
                    args["opts"] = opts;
                    let out = forward(&call, &peer, "acquire", args).await?;
                    if let Some(sid) = out["session_id"].as_str() {
                        m.note_forwarded(sid, &peer);
                    }
                    return Ok(out);
                }
            }
            let game: String = call.req("game")?;
            let opts = call.args.get("opts").cloned().unwrap_or(json!({}));
            m.acquire(&game, call.str_arg("mode"), &opts, level).await
        }
        "release" => m.release(&id()?).await,
        "reset" => m.reset(&id()?).await,
        "status" => m.status(call.str_arg("session_id").filter(|s| !s.is_empty())).await,
        "tab_open" => m.tab_open(&id()?, call.str_arg("url")).await,
        "tab_close" => m.tab_close(&id()?, &call.req::<String>("tab_id")?).await,
        "tab_list" => m.tab_list(&id()?).await,
        "tab_activate" => m.tab_activate(&id()?, &call.req::<String>("tab_id")?).await,
        "observe" => m.observe(&id()?, call.str_arg("tab_id")).await,
        "screenshot" => m.screenshot(&id()?, call.str_arg("tab_id"), call.args.get("full_page").and_then(Value::as_bool).unwrap_or(false)).await,
        "cdp" => m.cdp(&id()?, &call.req::<String>("method")?, call.str_arg("tab_id"), call.args.get("params").cloned().unwrap_or(Value::Null)).await,
        "commands" => m.commands(call.str_arg("session_id").filter(|s| !s.is_empty())).await,
        other => Err(EngineError::Value(format!("unknown verb {other}"))),
    };
    answer(out)
}

fn service(svc: Service, manager: Arc<Manager>) -> Service {
    let mut svc = svc.emitter(manifest::EMIT_TOPIC, Some(manifest::EMIT_DESCRIPTION)).api("sessions", json!([]));
    for v in manifest::VERBS {
        let m = manager.clone();
        let params: Value = serde_json::from_str(v.params).expect("the verb table's params are JSON");
        svc = svc.verb(TOOL, v.name, v.description, params, move |call: Call| handle(m.clone(), call));
    }
    svc
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    awm_statecheck::cli::state_subcommand(TOOL, include_str!("../state.toml"), &awm_plugin_sdk::config::service_dir(TOOL), &awm_dir)?;
    let svc = Service::from_env()?;
    let dir = awm_plugin_sdk::config::service_dir(TOOL);
    let store = store::Store::open(&dir.join("rlm-browser.db"))?;
    let manager = Arc::new(Manager::new(Config::from_env(dir.join("profiles")), store, Some(svc.context())));
    service(svc, manager).run().await
}

#[cfg(test)]
mod tests;
