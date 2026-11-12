//! The embedding models, where their files live, and the sha256 table they load against.
//!
//! The files come from a pinned deploy artifact under `<AWM_DIR>/models/`, never from an HF cache.

use sha2::{Digest, Sha256};
use std::io::Read;
use std::path::{Path, PathBuf};

/// How a model turns token states into one vector.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pooling {
    /// Mean over the attention mask (sentence-transformers' default).
    Mean,
    /// The first token's state (BGE).
    Cls,
}

/// One embedding model: its pinned files and how to run them.
#[derive(Debug)]
pub struct Model {
    /// The model name stored with every vector.
    pub name: &'static str,
    /// The upstream revision the files come from.
    pub revision: &'static str,
    pub dim: usize,
    /// sentence-transformers' `max_seq_length`, which overrides tokenizer.json.
    pub max_seq_length: usize,
    pub pooling: Pooling,
    /// Prepended to a query, never to a passage.
    pub query_prefix: &'static str,
    /// Each file the embedder loads, with its sha256.
    pub files: &'static [(&'static str, &'static str)],
}

/// all-MiniLM-L6-v2, which journals, guide and transcripts rank by.
pub const MINILM: Model = Model {
    name: "all-MiniLM-L6-v2",
    revision: "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    dim: 384,
    max_seq_length: 256,
    pooling: Pooling::Mean,
    query_prefix: "",
    files: &[
        ("model.onnx", "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"),
        ("tokenizer.json", "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037"),
    ],
};

/// BAAI/bge-large-en-v1.5 from Qdrant's ONNX export, which papers rank by.
pub const BGE_LARGE: Model = Model {
    name: "bge-large-en-v1.5",
    revision: "e93b9305e013fdf771b471426516285da0cca188",
    dim: 1024,
    max_seq_length: 512,
    pooling: Pooling::Cls,
    query_prefix: "Represent this sentence for searching relevant passages: ",
    files: &[
        ("model.onnx", "523c08e39a645aa3760380dee5d4b432b2f3ce7ba77ff2cd9c02c5a896b6a7c5"),
        ("tokenizer.json", "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"),
    ],
};

impl Model {
    /// `<awm_dir>/models/<name>/<revision>`.
    pub fn dir(&self, awm_dir: &Path) -> PathBuf {
        awm_dir.join("models").join(self.name).join(self.revision)
    }

    /// Check every file in `dir` against its sha256.
    pub fn verify(&self, dir: &Path) -> Result<(), ModelFilesError> {
        for (name, expected) in self.files {
            let path = dir.join(name);
            let found = sha256_file(&path)?;
            if found != *expected {
                return Err(ModelFilesError::Mismatch { path, found, expected: expected.to_string() });
            }
        }
        Ok(())
    }
}

/// Why the model files cannot be used.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ModelFilesError {
    #[error("{0} is missing")]
    Missing(PathBuf),
    #[error("{path} has sha256 {found}, expected {expected}")]
    Mismatch { path: PathBuf, found: String, expected: String },
    #[error("{path}: {err}")]
    Io { path: PathBuf, err: String },
}

fn sha256_file(path: &Path) -> Result<String, ModelFilesError> {
    let io = |e: std::io::Error| ModelFilesError::Io { path: path.to_path_buf(), err: e.to_string() };
    let mut f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(ModelFilesError::Missing(path.to_path_buf())),
        Err(e) => return Err(io(e)),
    };
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf).map_err(io)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher.finalize().iter().map(|b| format!("{b:02x}")).collect())
}
