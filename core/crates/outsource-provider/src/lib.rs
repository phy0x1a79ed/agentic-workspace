//! outsource provider runtime: bids, the declared-identity cache, job kinds and the local backend.

pub mod backend;
mod config;
mod host;
mod identity;
mod import;
mod index;
mod jobs;
mod local;
mod provider;
mod serve;

pub use backend::{Backend, Done, DoneTx, JobSpec};
pub use config::ProviderConfig;
pub use host::Host;
pub use identity::material_id;
pub use jobs::{disk_size, find_cargo, q};
pub use local::LocalBackend;
pub use provider::{DoneRx, Provider};
pub use serve::serve;

#[cfg(test)]
mod tests;
