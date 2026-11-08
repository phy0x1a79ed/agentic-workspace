use std::sync::Arc;

use awm_plugin_scopes::outbound::Hub;
use awm_plugin_scopes::{db, App, Paths};
use awm_plugin_sdk::Service;
use awm_statecheck::cli::StateCmd;
use awm_statecheck::decl::Decl;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "scopes", about = "The scope lifecycle on git, claims, tags, the private marks and the project index.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the scopes service (the default).
    Serve,
    /// The pre-redux state check: scopes.db and the private marks.
    State {
        #[command(subcommand)]
        cmd: StateCmd,
    },
}

fn decl() -> anyhow::Result<Decl> {
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    Decl::parse(include_str!("../state.toml"), &awm_dir.join("services/scopes"), &awm_dir, |n| std::env::var(n).ok())
}

fn main() -> anyhow::Result<()> {
    if let Some(Cmd::State { cmd }) = Cli::parse().cmd {
        std::process::exit(awm_statecheck::cli::run(&cmd, "scopes", &decl()?)?);
    }
    let out = awm_statecheck::snapshot_once("scopes", &decl()?)?;
    tokio::runtime::Builder::new_multi_thread().enable_all().build()?.block_on(run(out))
}

async fn run(snapshot: awm_statecheck::snapshot::Outcome) -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    for p in &snapshot.taken {
        tracing::info!("scopes: took the pre-redux snapshot {}", p.display());
    }
    let svc = Service::from_env()?;
    let ctx = svc.context();
    let state = ctx.env().state_dir();
    let awm_dir = ctx.env().awm_dir.clone();
    let path = state.join("scopes.db");
    let store = tokio::task::spawn_blocking(move || db::open(&path)).await?.map_err(|e| anyhow::anyhow!("{}: {e}", e.class))?;
    let rt = tokio::runtime::Handle::current();
    let out = Arc::new(Hub { ctx: ctx.clone(), rt: rt.clone() });
    let paths = Paths::new(awm_plugin_sdk::config::workspace_root());
    let app = Arc::new(App::new(paths, store, out, awm_dir.join("bin").join("dvc"), rt));
    awm_plugin_scopes::service(svc, app).run().await
}
