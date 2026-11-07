//! The tether: the prebuilt `tether-operator` and `tether-relay` binaries at the release the
//! plugin pins, fetched, adopted in their own units, and, on a member node, read through the
//! operator daemon's event stream.

pub mod adopted;
pub mod control;
pub mod fetch;
pub mod paths;
pub mod remote_os;
pub mod stream;
