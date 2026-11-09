//! The routine service, its land queue and its state check.

use std::sync::Arc;

use awm_plugin_routine::land::{self, LandArgs};
use awm_plugin_routine::events::Events;
use awm_plugin_routine::outbound::GatewayOutbound;
use awm_plugin_routine::runner::Runner;
use awm_plugin_routine::service::{self, RoutineService};
use awm_plugin_routine::store::Store;
use awm_plugin_routine::{snapshot, timers};
use awm_plugin_sdk::Service;
use awm_statecheck::cli::StateCmd;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "routine", about = "Registered routines, their timers and runs, and the land queue.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the routine service (the default).
    Serve,
    /// The land queue: submit, wait, batch, hold, bounce, status. See the skills ci/land and ci/merge.
    Land(LandArgs),
    /// The pre-redux state check.
    State {
        #[command(subcommand)]
        cmd: StateCmd,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    match Cli::parse().cmd {
        Some(Cmd::State { cmd }) => std::process::exit(awm_statecheck::cli::run(&cmd, "routine", &snapshot::decl(&awm_dir)?)?),
        Some(Cmd::Land(args)) => std::process::exit(tokio::task::spawn_blocking(move || land::main(args)).await?),
        Some(Cmd::Serve) | None => serve().await,
    }
}

async fn serve() -> anyhow::Result<()> {
    let svc = Service::from_env()?;
    let env = svc.context().env().clone();
    let store = Arc::new(Store::open(&env.state_dir().join("routine.db"))?);
    let orphans = store.orphan_live()?;
    if orphans > 0 {
        tracing::warn!("closed {orphans} runs left live by a previous process");
    }
    let ctx = svc.context();
    let usage = Arc::new(move |job: &str, ok: bool, ms: u64| ctx.usage(job, ok, ms));
    let out = Arc::new(GatewayOutbound { hub: svc.context().hub().clone() });
    let runner = Runner::new(store, out, Some(usage));
    let events = Events::new(svc.context().hub().clone(), runner.clone());
    events.reconcile();
    let routine = Arc::new(RoutineService { runner: runner.clone(), unit_dir: timers::unit_dir(), systemctl: Box::new(timers::real_systemctl), events: Some(events) });
    service::mount(svc, routine)
        .on_shutdown(async move {
            runner.shutdown().await;
        })
        .run()
        .await
}
