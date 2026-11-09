//! kb: awm's knowledge base. One process serves the `journal`, `search` and `transcripts` tools.
//! Each search source keeps its own LanceDB database under `services/kb/lance/<source>/`, and
//! search federates them in-process and asks peers over the hub. The model loads once here.

use std::path::Path;
use std::sync::Arc;

use awm_embed::Embedder;
use awm_kb_journals::{Journals, Opts};
use awm_kb_source::Source;
use awm_plugin_sdk::Service;
use awm_statecheck::cli::StateCmd;
use awm_statecheck::decl::Decl;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "kb", about = "awm's knowledge base: journal, search and transcripts.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the kb service (the default).
    Serve,
    /// The pre-redux state check over journal's, search's and transcripts' state dirs.
    State {
        #[command(subcommand)]
        cmd: StateCmd,
    },
    /// journal's rollback: copy every post written since the cutover back into the frozen
    /// tables of journal.db, so the Python journal reads them.
    ExportBack,
}

fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    Decl::parse(include_str!("../state.toml"), &awm_dir.join("services/kb"), awm_dir, |n| std::env::var(n).ok())
}

/// The model, or `None` when it is absent or turned off: sources then store their writes and
/// embed them when it loads.
fn embedder(awm_dir: &Path) -> Option<Arc<dyn Embedder>> {
    match awm_embed::MiniLm::load(awm_dir) {
        Ok(m) => Some(Arc::new(m)),
        Err(e) => {
            tracing::warn!("kb: no embedder, answering on keywords: {e}");
            None
        }
    }
}

fn main() -> anyhow::Result<()> {
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    let cli = Cli::parse();
    match cli.cmd {
        Some(Cmd::State { cmd }) => std::process::exit(awm_statecheck::cli::run(&cmd, "kb", &decl(&awm_dir)?)?),
        Some(Cmd::ExportBack) => {
            let out = runtime()?.block_on(awm_kb_journals::export_back(&awm_dir, &awm_plugin_sdk::config::node_name()))?;
            println!("{}", serde_json::to_string_pretty(&out)?);
            return Ok(());
        }
        Some(Cmd::Serve) | None => {}
    }
    runtime()?.block_on(run(&awm_dir))
}

fn runtime() -> std::io::Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_multi_thread().enable_all().build()
}

async fn run(awm_dir: &Path) -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let svc = Service::from_env()?;
    let ctx = svc.context();
    let hub = ctx.hub().clone();
    let model = embedder(awm_dir);
    let services = awm_dir.join("services");

    let journals = Journals::open(awm_dir, model.clone(), Opts::for_node(awm_dir, &awm_plugin_sdk::config::node_name())).await?;
    let workspace = awm_plugin_sdk::config::workspace_root();
    let guide = awm_kb_guide::Guide::open(awm_dir, &workspace, model.clone(), Arc::new(awm_kb_guide::HubGuide(hub.clone()))).await?;
    let transcripts = awm_kb_transcripts::open(awm_dir, &services.join("transcripts"), ctx.clone())?;
    let sources: Vec<Arc<dyn Source>> = vec![journals.clone(), guide.clone(), Arc::new(awm_kb_transcripts::source::Transcripts::new(transcripts.clone()))];
    let search = awm_kb_search::Search::with_hub(&services.join("search"), sources, model, hub.clone())?;

    let svc = awm_kb_journals::register(svc, journals.clone());
    let svc = awm_kb_search::register(svc, Arc::new(search));
    let svc = awm_kb_transcripts::register(svc, transcripts.clone());
    let start_ctx = ctx.clone();
    svc.on_start(async move {
        awm_kb_transcripts::start(&transcripts).await?;
        awm_kb_transcripts::ready(&start_ctx, transcripts);
        Ok(())
    })
    .on_ready(async move {
        journals.start(ctx);
        guide.follow(hub);
    })
        .run()
        .await
}
