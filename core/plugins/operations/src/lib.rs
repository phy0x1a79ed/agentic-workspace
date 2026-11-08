//! The operations plugin: the task store and its board feed, the relay with its outbox, the
//! session verbs, the approver with its vault API, and alcor's singleton door.

pub mod api;
pub mod approver;
pub mod arbutus;
pub mod door;
pub mod front;
pub mod home;
pub mod hook;
pub mod ids;
pub mod keys;
pub mod passkeys;
pub mod relay;
pub mod server;
pub mod service;
pub mod settings;
pub mod snapshot;
pub mod store;

/// Seconds since the epoch, as Python's `time.time()`.
pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}
