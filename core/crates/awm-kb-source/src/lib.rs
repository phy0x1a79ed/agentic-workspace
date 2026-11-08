//! The source interface inside kb. search parses the query once, embeds its text once when a
//! named source takes vectors, builds the caller's veil from the private marks, and asks each
//! named source in parallel. Each source answers from its own LanceDB database and applies the
//! veil itself, so no caller sees a row a mark covers.

use awm_index::fields::Fields;
use awm_index::grammar::{SourceFilter, Text};
use awm_index::sql::Veil;
use futures_util::future::BoxFuture;
use serde_json::{Map, Value};

/// What a source is asked for one query.
#[derive(Debug, Clone)]
pub struct Ask {
    /// The parsed text, or `None` for a query by fields alone.
    pub text: Option<Text>,
    /// The source's filter and sort, validated against its fields.
    pub source: SourceFilter,
    pub max: usize,
    /// The query text's vector, when the source takes vectors and the model is loaded.
    pub vector: Option<Vec<f32>>,
    pub veil: Veil,
    /// The calling session, for read records. `None` for a peer or a plain shell.
    pub session: Option<String>,
}

/// A source's refusal or failure. search shows it in that source's section only.
#[derive(Debug, Clone, PartialEq)]
pub struct SourceError(pub String);

impl std::fmt::Display for SourceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for SourceError {}

impl SourceError {
    pub fn of(e: impl std::fmt::Display) -> Self {
        SourceError(e.to_string())
    }
}

/// One of kb's databases as search sees it.
pub trait Source: Send + Sync {
    /// The name a query uses, such as `journals`.
    fn name(&self) -> &str;
    /// Whether a query that names no source asks this one.
    fn default(&self) -> bool;
    /// The declared fields, and the fields each hit carries.
    fn fields(&self) -> (&Fields, &[String]);
    /// Whether the source ranks by vectors, so search embeds the text for it.
    fn takes_vectors(&self) -> bool;
    /// `{exact, related?}` for one ask.
    fn query(&self, ask: Ask) -> BoxFuture<'_, Result<Value, SourceError>>;
    /// One document whole, or `None` when it is absent or the veil covers it.
    fn get<'a>(&'a self, id: &'a str, veil: &'a Veil, session: Option<&'a str>) -> BoxFuture<'a, Result<Option<Map<String, Value>>, SourceError>>;
    /// The source's own counts and state, for `search status`.
    fn status(&self) -> BoxFuture<'_, Value>;
}
