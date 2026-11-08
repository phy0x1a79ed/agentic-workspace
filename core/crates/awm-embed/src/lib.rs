//! The chunker and embedder of `persistence.embeddings`, for the plugins that keep a search
//! index: journal, guide and search (which embeds only the query).

pub mod chunk;
pub mod embed;
pub mod model;

pub use chunk::Document;
pub use embed::{disabled, EmbedError, Embedder, MiniLm, Stub};
