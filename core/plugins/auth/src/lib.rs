//! The auth plugin: the authority for device tokens and the edge session secret, and the
//! node's TLS front that enforces them (fleet.md R84, R92; ports/auth.md).
// An axum handler answers `Result<_, Response>`: the error is the reply, and boxing it buys nothing.
#![allow(clippy::result_large_err)]

pub mod gate;
pub mod store;
pub mod tokens;
pub mod verbs;

pub mod certs;
pub mod edge;
pub mod front;
pub mod service;
pub mod snapshot;
pub mod tls;

pub mod landing;
pub mod pages;
pub mod policy;
pub mod portal;

/// Seconds since the epoch.
pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}
