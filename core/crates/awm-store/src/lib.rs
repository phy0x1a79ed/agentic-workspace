//! SQLite for the Rust plugins, ported from `service_components/persistence`.
//!
//! Every path comes from the caller: journal's state dir is `/var/lib/awm/journal` on singleton
//! nodes, so nothing here derives `AWM_DIR/services/<service>/`.

mod init;
mod rows;
mod store;

pub mod settings;

pub use init::{InitOutcome, Migration};
pub use rows::{row_json, rows_json};
pub use rusqlite;
pub use store::{Store, StoreError};

/// Python's `persistence.timestamps` and `isoformat`, from awm-pycompat.
pub use awm_pycompat::time;

/// A fresh uuid4 as 32 hex digits, the row key every Python service mints.
pub fn new_uuid() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}
