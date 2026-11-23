//! outsource provider runtime: bids, the declared-identity cache, job kinds, the local backend and the host that entries attach to.

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
pub use identity::{identify, Probed};
pub use jobs::{find_cargo, is_local_route, q};
pub use local::LocalBackend;
pub use provider::{DoneRx, Provider};
pub use serve::Hosted;

#[cfg(test)]
mod tests;
