//! baitan provider runtime: bids, the declared-identity cache, job kinds and the local backend.

pub mod backend;
mod config;
mod host;
mod import;
mod index;
mod jobs;
mod local;
mod provider;
mod serve;

pub use backend::{Backend, Done, DoneTx, JobSpec};
pub use config::ProviderConfig;
pub use host::Host;
pub use jobs::{find_cargo, q};
pub use local::LocalBackend;
pub use provider::{DoneRx, Provider};
pub use serve::serve;

#[cfg(test)]
mod tests;
