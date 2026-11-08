//! Run the state commands for a plugin that has no Rust binary yet, such as the node
//! inventory taken before its port lands.

use std::path::PathBuf;

use awm_statecheck::{Decl, StateCmd};
use clap::Parser;

#[derive(Parser)]
#[command(about = "Snapshot, inventory, check, restore or prune a plugin's pre-redux state")]
struct Args {
    /// The plugin, as its state.toml names it.
    #[arg(long)]
    plugin: String,
    /// The state.toml to read. Without it, the state is <awm-dir>/services/<plugin>, whole.
    #[arg(long)]
    decl: Option<PathBuf>,
    #[arg(long, env = "AWM_DIR")]
    awm_dir: PathBuf,
    #[command(subcommand)]
    cmd: StateCmd,
}

fn main() -> anyhow::Result<()> {
    let a = Args::parse();
    let default_dir = a.awm_dir.join("services").join(&a.plugin);
    let decl = match &a.decl {
        Some(p) => {
            let text = std::fs::read_to_string(p).map_err(|e| anyhow::anyhow!("{}: {e}", p.display()))?;
            Decl::parse(&text, &default_dir, &a.awm_dir, |k| std::env::var(k).ok())?
        }
        None => Decl::whole(&default_dir),
    };
    std::process::exit(awm_statecheck::cli::run(&a.cmd, &a.plugin, &decl)?);
}
