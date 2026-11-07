//! Python 3.12 behaviour that the plugin ports store or match byte for byte.
//!
//! The character tables in `tables.rs` are generated from Python itself by `tests/capture.py`,
//! because Rust's standard library follows a newer Unicode version than Python 3.12 (Unicode 15).

mod tables;

pub mod chars;
pub mod difflib;
pub mod json;
pub mod re;
pub mod repr;
pub mod pystr;
pub mod time;

pub use tables::UNICODE_VERSION;
