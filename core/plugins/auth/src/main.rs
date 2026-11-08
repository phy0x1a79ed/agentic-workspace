//! The auth service and the state check.

use awm_plugin_auth::{service, snapshot};
use awm_statecheck::cli::StateCmd;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "auth", about = "Device tokens, the edge session secret, and the node's browser edge.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the auth service (the default).
    Serve,
    /// The pre-redux state check.
    State {
        #[command(subcommand)]
        cmd: StateCmd,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    match Cli::parse().cmd {
        Some(Cmd::State { cmd }) => {
            let decl = snapshot::decl(&awm_plugin_sdk::config::awm_dir())?;
            std::process::exit(awm_statecheck::cli::run(&cmd, "auth", &decl)?)
        }
        Some(Cmd::Serve) | None => service::serve().await,
    }
}
