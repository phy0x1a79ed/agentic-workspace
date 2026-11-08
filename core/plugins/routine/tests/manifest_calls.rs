//! Replay the routine plugin's real gateway calls (plugin-contract § Calls).
//!
//! A failed routine runs its failure chain through the real GatewayOutbound, against a fake
//! gateway. The chain makes only plain calls, so the manifest declares no needs, and every call
//! carries the service token.

mod common;

use std::sync::{Arc, Mutex};

use awm_plugin_routine::outbound::GatewayOutbound;
use awm_plugin_routine::runner::Runner;
use awm_plugin_routine::store::Store;
use awm_plugin_sdk::axum::{self, extract::State, http::HeaderMap, routing::post, Json, Router};
use common::{project_repo, write_routine};
use serde_json::{json, Value};

const TOKEN: &str = "svc-token-test";
const FAILING: &str = "[routine]\nname = \"nightly\"\ndescription = \"fails\"\n[[step]]\nname = \"boom\"\nrun = \"exit 3\"\n[fixer]\npeer = \"alcor\"\n";

/// One call: its tool, verb, peer and service token.
type Seen = (String, String, Option<String>, Option<String>);

#[derive(Default)]
struct Gateway {
    calls: Mutex<Vec<Seen>>,
    fail_start: Mutex<bool>,
}

async fn invoke(State(gw): State<Arc<Gateway>>, headers: HeaderMap, Json(body): Json<Value>) -> Json<Value> {
    let tool = body["name"].as_str().unwrap_or("").to_string();
    let verb = body["args"]["verb"].as_str().unwrap_or("").to_string();
    let peer = body["args"]["peer"].as_str().map(str::to_string);
    let token = headers.get("X-Awm-Service-Token").and_then(|v| v.to_str().ok()).map(str::to_string);
    gw.calls.lock().unwrap().push((tool.clone(), verb.clone(), peer, token));
    if (tool.as_str(), verb.as_str()) == ("reflection", "start_other") {
        if *gw.fail_start.lock().unwrap() {
            return Json(json!({"result": {"error": "peer unreachable"}}));
        }
        return Json(json!({"result": {"job": "j1"}}));
    }
    Json(json!({"result": {"ok": true}}))
}

#[tokio::test]
async fn the_failure_chain_makes_plain_calls_with_the_service_token() {
    let manifest: toml::Table = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/awm-plugin.toml")).unwrap().parse().unwrap();
    assert!(manifest["needs"].get("tool").is_none(), "routine declares no needs");
    assert_eq!(manifest["needs"]["network"].as_array().unwrap(), &[toml::Value::from("*.globus.org")]);

    let gw = Arc::new(Gateway::default());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let app = Router::new().route("/invoke", post(invoke)).with_state(gw.clone());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });

    let tmp = tempfile::tempdir().unwrap();
    let token = tmp.path().join("token");
    std::fs::write(&token, TOKEN).unwrap();
    let store = Arc::new(Store::open(&tmp.path().join("routine.db")).unwrap());
    let out = Arc::new(GatewayOutbound { hub: awm_plugin_sdk::Hub::new(&url, Some(token)) });
    let runner = Runner::new(store, out, None);
    let repo = project_repo(tmp.path());
    runner.install("proj", &write_routine(&repo, "nightly", FAILING).to_string_lossy()).unwrap();

    // A failed run starts its fixer. A fixer that cannot start falls back to the notice.
    let run = runner.trigger("proj", "nightly", "call", "", true).await.unwrap();
    assert_eq!(run["dispatched"][0]["via"], "agent");
    *gw.fail_start.lock().unwrap() = true;
    let run = runner.trigger("proj", "nightly", "call", "", true).await.unwrap();
    assert_eq!(run["dispatched"][0]["via"], "notice");
    assert!(run["dispatched"][0]["errors"][0].as_str().unwrap().contains("peer unreachable"));

    let calls = gw.calls.lock().unwrap();
    let seen: Vec<(&str, &str, Option<&str>)> = calls.iter().map(|(t, v, p, _)| (t.as_str(), v.as_str(), p.as_deref())).collect();
    assert_eq!(seen, [("reflection", "start_other", Some("alcor")), ("reflection", "start_other", Some("alcor")), ("social", "send", None)]);
    assert!(calls.iter().all(|c| c.3.as_deref() == Some(TOKEN)));
}
