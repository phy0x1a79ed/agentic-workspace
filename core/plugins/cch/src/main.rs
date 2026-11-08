//! The cch binary: serves tool `harness-cc` over this node's Claude Code sessions.

use std::sync::Arc;

use awm_plugin_sdk::Service;
use awm_statecheck::cli::StateCmd;
use clap::Parser;
use cch::app::Live;
use cch::paths::Paths;
use cch::verbs::{serve, App};

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("hook") {
        std::process::exit(cch::hooks::main(args.get(1).map(String::as_str)));
    }
    if args.first().map(String::as_str) == Some("state") {
        let cli = StateCli::parse_from(std::iter::once("cch state".to_string()).chain(args.into_iter().skip(1)));
        let decl = cch::snapshot::decl(&awm_plugin_sdk::config::awm_dir())?;
        std::process::exit(awm_statecheck::cli::run(&cli.cmd, "cch", &decl)?);
    }
    service()
}

/// `cch state <cmd>`: the pre-redux state check.
#[derive(Parser)]
struct StateCli {
    #[command(subcommand)]
    cmd: StateCmd,
}

#[tokio::main]
async fn service() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    cch::snapshot::take(&awm_plugin_sdk::config::awm_dir())?;
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
