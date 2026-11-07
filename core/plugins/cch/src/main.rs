//! The cch binary: serves tool `harness-cc` over this node's Claude Code sessions.

use std::sync::Arc;

use awm_plugin_sdk::Service;
use cch::app::Live;
use cch::paths::Paths;
use cch::verbs::{serve, App};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let svc = Service::from_env()?;
    let app = Arc::new(App::new(Live::new(Paths::from_env(), svc.context().hub().clone())));
    let boot = app.clone();
    serve(svc, app)
        .on_start(async move {
            boot.backend.boot().await;
            Ok(())
        })
        .run()
        .await
}
