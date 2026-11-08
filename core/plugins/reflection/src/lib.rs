//! reflection: the one agent-facing session tool (install.md R135). It holds what holds for
//! every harness (who is calling, trust, the ledger of owed follow-ups, waits) and does each
//! verb's session work through the harness@1 provider of the session it acts on.

pub mod app;
pub mod gateway;
pub mod store;

pub mod caller;
pub mod providers;
pub mod trust;

pub mod calls;
pub mod ledger;
pub mod orders;
pub mod peers;
pub mod waits;

pub mod verbs;
pub mod surface;
pub mod snapshot;

#[cfg(test)]
mod tests;
