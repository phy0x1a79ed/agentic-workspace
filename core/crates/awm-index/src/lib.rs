//! The search engine each source links, a port of search's `lance_store` and `lance_query`.
//!
//! A source keeps an entries table and a chunks table per section: one per calendar year of the
//! entry's own date, plus `undated`. A query runs on every section, newest first. Each write
//! counts the rows it leaves outside the indexes, and the count, not a clock, starts maintenance.

pub mod cells;
pub mod fields;
pub mod fts;
pub mod fuse;
pub mod grammar;
pub mod query;
pub mod sql;
pub mod store;

pub use fields::{Class, Field, Fields};
pub use grammar::QueryError;
pub use query::{Answer, Ask};
pub use sql::{Mark, Veil};
pub use store::{Index, Kind, Spec, Store, Written};

/// A query this source's store cannot answer, with the reason.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct QueryUnserved(pub String);

#[derive(Debug, thiserror::Error)]
pub enum IndexError {
    #[error(transparent)]
    Lance(#[from] lancedb::Error),
    #[error(transparent)]
    Arrow(#[from] lancedb::arrow::arrow_schema::ArrowError),
    #[error(transparent)]
    Unserved(#[from] QueryUnserved),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    /// A table on disk that this source's declarations do not describe.
    #[error("{0}")]
    Layout(String),
}

pub type Result<T, E = IndexError> = std::result::Result<T, E>;
