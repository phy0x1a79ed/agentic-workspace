//! The Rust side of an awm plugin service, the twin of Python's `awm.gatewayclient`.
//!
//! A service builds a [`Service`] from the environment the gateway spawns it with, adds its
//! verbs, sessions and mount, and calls [`Service::run`]: it registers, sends `ready`, answers
//! calls until the gateway stands it down, and reconnects as `ServiceAdapter` does
//! (`docs/redux/drafts/plugin-contract.md` § Service wire protocol). [`Hub`] is the other
//! direction, a call from this plugin to another one through the gateway.
//!
//! ```no_run
//! use awm_plugin_sdk::{Call, Service, VerbError};
//! use serde_json::{json, Value};
//!
//! #[tokio::main]
//! async fn main() -> anyhow::Result<()> {
//!     awm_plugin_sdk::telemetry::init();
//!     Service::from_env()?
//!         .verb("hello", "echo", "Return the text.", json!([{"name": "text", "type": "string", "required": true}]),
//!             |call: Call| async move { Ok::<Value, VerbError>(call.args["text"].clone()) })
//!         .run()
//!         .await
//! }
//! ```

pub mod cancel;
pub mod config;
pub mod holders;
pub mod hub;
pub mod privacy;
pub mod proc;
pub mod procfs;
pub mod service;
pub mod sessions;
pub mod supervise;
pub mod telemetry;

pub use awm_proto::ids;

pub use awm_proto::{Caller, Lane, Surface, UsageEvent, WsMsg};
pub use awm_trust::Level;
pub use cancel::CancelToken;
pub use hub::{Bridge, CallOpts, Hub, HubError, Lease, LeaseOpts, RefCache, Subscription};
pub use service::{Call, Context, Env, Service, Session, TestService, VerbError};
pub use supervise::{spawn_supervised, SubscriptionOpts, Supervised, SupervisedSubscription};

/// Re-exported so a plugin builds its mount and its bridge code against the SDK's versions.
pub use axum;
pub use tokio_tungstenite;
