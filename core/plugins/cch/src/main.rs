//! The cch binary: serves tool `harness-cc` over this node's Claude Code sessions.

use std::sync::Arc;

use awm_plugin_sdk::Service;
use cch::app::Live;
use cch::paths::Paths;
use cch::verbs::{serve, App};

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("hook") {
        std::process::exit(cch::hooks::main(args.get(1).map(String::as_str)));
    }
    service()
}

#[tokio::main]
async fn service() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let svc = Service::from_env()?;
    let app = Arc::new(App::new(Live::new(Paths::from_env(), svc.context().hub().clone())));
    let boot = app.clone();
    // The SDK holds every verb until this init returns, so none is answered before the boot.
    serve(svc, app)
        .on_start(async move {
            cch::app::boot(boot).await;
            Ok(())
        })
        .run()
        .await
}
