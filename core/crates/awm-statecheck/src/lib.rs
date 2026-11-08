//! The pre-redux state check that every Rust port runs (state-safety rules 2 to 6).
//!
//! A port names its state in `state.toml` ([`Decl`]). Before the first Rust start,
//! [`snapshot_once`] copies it to `<state dir>/.pre-rust/`. [`check`] then proves after
//! each land that no table, column, row key or file of that copy went missing. [`restore`]
//! acts only on proven damage, and [`prune`] only on Tony's dated OK.

pub mod cachetag;
pub mod check;
pub mod cli;
pub mod decl;
pub mod inventory;
pub mod restore;
pub mod snapshot;

pub use check::{check, Report};
pub use cli::StateCmd;
pub use decl::Decl;
pub use inventory::{inventory, Inventory, PRE_RUST};
pub use restore::{prune, restore, RestoreError, ROLLBACK_RULE};
pub use snapshot::{snapshot_once, Outcome, SnapError};

#[cfg(test)]
mod tests;
