//! The fleet's edge list, trust per session, grant checks and the service door.
//!
//! The Python twin is `awm.config.edges` plus `awm.trustpolicy`. Both read the
//! same `edges.yml` and the same grant envelope, so they must agree byte for byte.

pub mod edges;
pub mod grant;
pub mod port;
pub mod trust;

pub use edges::{EdgeList, Network};
pub use grant::{verify_grant, Grant, GrantError};
pub use trust::{call_refusal, cross_edge, journal_bound, level_for_start, may_steer, stamped_level, Call, JournalBound, Level, TrustError};
