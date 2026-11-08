//! The reflection tool's verbs as MCP, the CLI and HTTP show them: `surface.json`, the Python
//! verb table with the port's listed changes, and the projection onto the SDK service.

use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::{Call, Service, VerbError};
use serde_json::{json, Value};

use crate::app::App;
use crate::caller::Frame;
use crate::gateway::Gateway;

pub const TOOL: &str = "reflection";
const TABLE: &str = include_str!("surface.json");

/// One published verb.
#[derive(Debug, Clone, PartialEq)]
pub struct Spec {
    pub verb: String,
    pub description: String,
    pub params: Value,
    /// Seconds the gateway waits for the reply, when not its default.
    pub timeout: Option<u64>,
    /// The surfaces the verb shows on, when not all of them.
    pub surfaces: Option<Vec<String>>,
}

fn table() -> Value {
    serde_json::from_str(TABLE).expect("surface.json is valid JSON")
}

/// The tool's description in the manifest.
pub fn description() -> String {
    table()["description"].as_str().unwrap_or_default().to_string()
}

/// Every verb, in the table's order.
pub fn specs() -> Vec<Spec> {
    let t = table();
    t["functions"]
        .as_array()
        .map(|fs| {
            fs.iter()
                .map(|f| Spec {
                    verb: f["name"].as_str().unwrap_or_default().to_string(),
                    description: f["description"].as_str().unwrap_or_default().to_string(),
                    params: f.get("params").cloned().unwrap_or_else(|| json!([])),
                    timeout: f.get("timeout").and_then(Value::as_u64),
                    surfaces: f.get("surfaces").and_then(Value::as_array).map(|s| s.iter().filter_map(Value::as_str).map(str::to_string).collect()),
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Publish every verb of `app` on `svc`.
pub fn serve<G: Gateway>(svc: Service, app: Arc<App<G>>) -> Service {
    let svc = svc.api("description", json!(description()));
    specs().into_iter().fold(svc, |svc, spec| {
        let (verb, app) = (spec.verb.clone(), app.clone());
        let svc = svc.verb(TOOL, &spec.verb, &spec.description, spec.params, move |c: Call| {
            let (app, verb) = (app.clone(), verb.clone());
            async move { Ok::<Value, VerbError>(app.handle(&verb, &Frame::from_call(&c), &c.args).await) }
        });
        let svc = match spec.timeout {
            Some(t) => svc.timeout(Duration::from_secs(t)),
            None => svc,
        };
        match spec.surfaces {
            Some(s) => svc.surfaces(s),
            None => svc,
        }
    })
}
