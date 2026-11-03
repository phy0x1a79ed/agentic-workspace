//! The awm gateway (install.md R112): loads plugins, runs their services, and routes every call.

pub mod caller;
pub mod config;
pub mod gateway;
pub mod hub;
pub mod loader;
pub mod peer;
pub mod server;
pub mod supervisor;
pub mod usage;

pub use gateway::Gateway;
