//! The fleet's edge list, trust per session, grant checks and the service door.
//!
//! The Python twin is `awm.config.edges` plus `awm.trustpolicy`. Both read the
//! same `edges.yml` and the same grant envelope, so they must agree byte for byte.

pub mod edges;
pub mod grant;
pub mod port;
pub mod requests;
pub mod trust;

pub use edges::{EdgeList, Network};
pub use grant::{sign_envelope, verify_grant, verify_order, Grant, GrantError};
pub use trust::{call_refusal, check_effort, cross_edge, journal_bound, level_for_start, may_steer, stamped_level, Call, JournalBound, Level, Raise, TrustError, FIRST_LOCAL_MESSAGE_HINT};
