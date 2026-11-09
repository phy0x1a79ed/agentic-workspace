//! Cross-node calls over the ssh edge (fleet.md R92 item "cross-node call", R93).
//!
//! A cross-node call is an ordinary awm request carried inside plain ssh to the
//! other node's loopback gateway. The caller keeps one
//! `ssh -T <peer> AWM_EDGE_FROM=<node> awm edge-serve` per peer and sends every call down
//! its stdio as one JSON line. Replies come
//! back by id, so calls overlap. A dropped link fails the calls in flight and the
//! next call reopens it. A sleeping node fails like any unreachable peer.

pub mod client;
pub mod frame;
pub mod server;
pub mod transport;

pub use client::{EdgeClient, EdgeError, SshCommand};
pub use frame::{Request, Response};
pub use server::{login, serve, EdgeLevel, Login, ServiceGate};
pub use transport::{EdgePeers, TABLE_CHANGED_FRAME, TOOLS_FRAME};
