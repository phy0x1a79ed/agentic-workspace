//! The DVC data layer, done natively. A commit records code and the data it was built
//! against: the `.dvc` pin is tracked beside the code, and the bytes live once in a
//! content-addressed cache that every scope on the machine shares.
//!
//! Everything the Python layer asked the `dvc` CLI for runs here instead: checkout,
//! status, the merge driver and the three git hooks. `dvc` itself stays a hand tool for
//! `dvc add`. The cache format is DVC 3's `files/md5`, and a legacy pin without
//! `hash: md5` is refused, never hashed.

pub mod cachetag {
    //! The marker that opts a directory out of the mirror. awm-statecheck owns it.
    pub use awm_statecheck::cachetag::*;
}
pub mod checkout;
pub mod cli;
pub mod dvcignore;
pub mod gc;
pub mod git;
pub mod hooks;
pub mod merge;
pub mod odb;
pub mod pin;
pub mod provision;
pub mod pyjson;
pub mod scope;
pub mod status;
pub mod testkit;
pub mod wiring;

pub use cli::DataCmd;
pub use provision::Layout;

#[cfg(test)]
mod tests;
