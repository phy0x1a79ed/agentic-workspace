//! The reflection binary: serves tool `reflection`, and checks its state as `reflection state`.

use std::sync::Arc;

use awm_plugin_sdk::{config, Service};
use awm_statecheck::cli::StateCmd;
use clap::Parser;
use reflection::app::App;
use reflection::gateway::LiveNode;
use reflection::store::Store;
use reflection::trust::Policy;

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("state") {
        let cli = StateCli::parse_from(std::iter::once("reflection state".to_string()).chain(args.into_iter().skip(1)));
        let decl = reflection::snapshot::decl(&config::awm_dir())?;
        std::process::exit(awm_statecheck::cli::run(&cli.cmd, "reflection", &decl)?);
    }
    service()
}

/// `reflection state <cmd>`: the pre-redux state check.
#[derive(Parser)]
struct StateCli {
    #[command(subcommand)]
    cmd: StateCmd,
}

#[tokio::main]
async fn service() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let awm_dir = config::awm_dir();
    let svc = Service::from_env()?;
    let store = Store::new(reflection::snapshot::service_dir(&awm_dir));
    let app = Arc::new(App::new(svc.context().hub().clone(), Arc::new(LiveNode(svc.context())), Arc::new(Policy), store));
    let boot = app.clone();
    let grant_key = awm_trust::grant::grant_key_env(std::env::var(awm_trust::grant::GRANT_KEY_ENV).ok())
        .or_else(|_| awm_trust::grant::grant_key_file(&awm_dir))
        .ok();
    let started = async move {
        boot.clone().boot().await;
        match grant_key {
            Some(key) => drop(tokio::spawn(boot.order_loop(key))),
            None => tracing::warn!("reflection: no AWM_APPROVER_GRANT_PUBKEY: agent orders stay off on this node"),
        }
    };
    reflection::surface::serve(svc, app).on_ready(started).run().await
}
