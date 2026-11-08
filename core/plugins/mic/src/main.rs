use std::sync::Arc;
use std::time::Duration;

use awm_plugin_mic::audio::{self, Audio};
use awm_plugin_mic::bridge::Mic;
use awm_plugin_sdk::Service;
use awm_statecheck::cli::StateCmd;
use awm_statecheck::decl::Decl;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "mic", about = "Pipe a phone's browser audio into the PulseAudio virtmic sink.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the mic service (the default).
    Serve,
    /// The pre-redux state check: the three sound-server files mic appends to.
    State {
        #[command(subcommand)]
        cmd: StateCmd,
    },
}

fn decl() -> anyhow::Result<Decl> {
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    Decl::parse(include_str!("../state.toml"), &awm_dir.join("services/mic"), &awm_dir, |n| std::env::var(n).ok())
}

fn main() -> anyhow::Result<()> {
    if let Some(Cmd::State { cmd }) = Cli::parse().cmd {
        std::process::exit(awm_statecheck::cli::run(&cmd, "mic", &decl()?)?);
    }
    audio::ensure_runtime_env();
    let out = awm_statecheck::snapshot_once("mic", &decl()?)?;
    tokio::runtime::Builder::new_multi_thread().enable_all().build()?.block_on(run(out))
}

async fn run(snapshot: awm_statecheck::snapshot::Outcome) -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    for p in &snapshot.taken {
        tracing::info!("mic: took the pre-redux snapshot {}", p.display());
    }
    let var = |k: &str| std::env::var(k).ok().filter(|v| !v.trim().is_empty());
    let sink = var("MIC_SINK").unwrap_or_else(|| audio::DEFAULT_SINK.into());
    let interval = var("MIC_HEALTH_INTERVAL_S").and_then(|v| v.trim().parse::<f64>().ok()).filter(|s| *s > 0.0).unwrap_or(20.0);
    let mic = Arc::new(Mic::new(Arc::new(Audio::system()), &sink));
    let svc = Service::from_env()?;
    let ctx = svc.context();
    let loop_mic = mic.clone();
    awm_plugin_mic::service(svc, mic)
        .on_ready(async move {
            let c = ctx.clone();
            ctx.spawn("health", async move {
                awm_plugin_mic::health(c, loop_mic, Duration::from_secs_f64(interval)).await;
                Ok(())
            });
        })
        .run()
        .await
}
