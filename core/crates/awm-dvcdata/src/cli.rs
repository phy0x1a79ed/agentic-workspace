//! The subcommands git and an operator call: the hooks, the merge driver, status and
//! checkout. The dvc plugin's binary carries them, so they run without the gateway.

use std::path::PathBuf;

use crate::hooks::{self, HookKind};

#[derive(Debug, clap::Subcommand)]
pub enum DataCmd {
    /// Run a data git hook (post-merge, post-commit or post-checkout) in the current worktree.
    Hook {
        name: String,
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    /// Merge two `.dvc` pins as git's merge driver: --ancestor %O --our %A --their %B.
    MergeDriver {
        #[arg(long)]
        ancestor: PathBuf,
        #[arg(long)]
        our: PathBuf,
        #[arg(long)]
        their: PathBuf,
    },
    /// Print, as JSON, the outputs that differ from their pins. Exits 1 when any does.
    Status {
        #[arg(long, default_value = ".")]
        repo: PathBuf,
        targets: Vec<String>,
    },
    /// Check out pinned outputs, or every output. Exits 1 when one fails.
    Checkout {
        #[arg(long, default_value = ".")]
        repo: PathBuf,
        targets: Vec<String>,
    },
}

fn absolute(p: PathBuf) -> PathBuf {
    std::fs::canonicalize(&p).unwrap_or(p)
}

/// Run one subcommand and return its exit code.
pub fn run(cmd: DataCmd) -> i32 {
    match cmd {
        DataCmd::Hook { name, args } => match HookKind::from_name(&name) {
            Some(kind) => hooks::run(kind, &absolute(PathBuf::from(".")), &args),
            None => {
                eprintln!("unknown hook {name}");
                2
            }
        },
        DataCmd::MergeDriver { ancestor, our, their } => match crate::merge::merge_driver(&absolute(PathBuf::from(".")), &ancestor, &our, &their) {
            Ok(()) => 0,
            Err(e) => {
                eprintln!("ERROR: {e}");
                1
            }
        },
        DataCmd::Status { repo, targets } => {
            let names = (!targets.is_empty()).then_some(targets.as_slice());
            match crate::status::changed_outs(&absolute(repo), names) {
                Ok(changed) => {
                    println!("{}", serde_json::to_string_pretty(&changed).unwrap_or_default());
                    i32::from(!changed.is_empty())
                }
                Err(e) => {
                    eprintln!("ERROR: {e}");
                    2
                }
            }
        }
        DataCmd::Checkout { repo, targets } => {
            let names = (!targets.is_empty()).then_some(targets.as_slice());
            match crate::checkout::checkout(&absolute(repo), names) {
                Ok(r) => {
                    println!("{}", serde_json::to_string_pretty(&r).unwrap_or_default());
                    i32::from(!r.ok())
                }
                Err(e) => {
                    eprintln!("ERROR: {e}");
                    2
                }
            }
        }
    }
}
