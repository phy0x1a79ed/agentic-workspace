//! The awm gateway (install.md R112): loads plugins, runs their services, and routes every call.

pub mod caller;
pub mod config;
pub mod door;
pub mod egress;
pub mod files;
pub mod hooks;
pub mod gateway;
pub mod hub;
pub mod install;
pub mod isolated;
pub mod loader;
pub mod modes;
pub mod mount;
pub mod native;
pub mod peer;
pub mod privacy;
pub mod server;
pub mod streams;
pub mod supervisor;
pub mod survival;
pub mod usage;

pub use gateway::Gateway;
