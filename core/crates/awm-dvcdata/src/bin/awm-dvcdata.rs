//! The data layer's subcommands on their own, for tests and for a node without the dvc plugin.

use clap::Parser;

#[derive(Parser)]
#[command(name = "awm-dvcdata", about = "The DVC data layer's git hooks, merge driver, status and checkout")]
struct Cli {
    #[command(subcommand)]
    cmd: awm_dvcdata::DataCmd,
}

fn main() {
    std::process::exit(awm_dvcdata::cli::run(Cli::parse().cmd));
}
