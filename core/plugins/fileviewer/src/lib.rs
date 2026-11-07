//! The fileviewer plugin: a file by its absolute path, served on the gateway origin at
//! `/s/fileviewer/<path>` with its `Content-Type`, so the browser renders it. A missing,
//! outside or masked path is one plain 404.

pub mod glob;
pub mod mask;
pub mod mime;
mod mime_table;
pub mod resolve;
pub mod server;
