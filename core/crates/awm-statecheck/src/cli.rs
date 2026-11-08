//! The `<plugin> state …` subcommands. A plugin binary embeds [`StateCmd`] in its own
//! command line; `awm-statecheck` runs the same commands for a plugin not yet ported.

use std::path::PathBuf;

use crate::decl::Decl;

#[derive(Debug, Clone, clap::Subcommand)]
pub enum StateCmd {
    /// Copy the state once into each state dir's .pre-rust/. An existing copy is kept.
    Snapshot,
    /// Print the live state's paths, modes, owners, sizes, tables, columns and row counts.
    Inventory,
    /// Compare the live state with .pre-rust/. Exits 1 when anything is lost.
    Check,
    /// Put named stores back from .pre-rust/, moving the live copies aside. Run it with the plugin stopped.
    Restore {
        /// State `check` proved these stores damaged.
        #[arg(long)]
        damage_proven: bool,
        /// Each store, as a path relative to its state dir or absolute.
        stores: Vec<PathBuf>,
    },
    /// Delete .pre-rust/ once Tony accepts the port.
    Prune {
        /// The date of Tony's OK.
        #[arg(long = "tony-ok", value_name = "YYYY-MM-DD")]
        tony_ok: String,
    },
}

fn print(v: &impl serde::Serialize) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string_pretty(v)?);
    Ok(())
}

/// Run `cmd` for `plugin`. Returns the exit code: 0 done, 1 the check found a loss, 2 refused.
pub fn run(cmd: &StateCmd, plugin: &str, decl: &Decl) -> anyhow::Result<i32> {
    match cmd {
        StateCmd::Snapshot => print(&crate::snapshot_once(plugin, decl)?)?,
        StateCmd::Inventory => print(&crate::inventory(plugin, decl)?)?,
        StateCmd::Check => {
            let r = crate::check(decl)?;
            print(&r)?;
            return Ok(if r.ok() { 0 } else { 1 });
        }
        StateCmd::Restore { damage_proven, stores } => match crate::restore(decl, stores, *damage_proven) {
            Ok(lines) => lines.iter().for_each(|l| println!("{l}")),
            Err(e) => {
                eprintln!("{e}");
                return Ok(2);
            }
        },
        StateCmd::Prune { tony_ok } => match crate::prune(decl, tony_ok, chrono::Local::now().date_naive()) {
            Ok(gone) => gone.iter().for_each(|p| println!("deleted {}", p.display())),
            Err(e) => {
                eprintln!("{e}");
                return Ok(2);
            }
        },
    }
    Ok(0)
}
