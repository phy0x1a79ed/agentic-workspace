//! Where the MiniLM files live, and the sha256 table they load against.
//!
//! The files come from a pinned deploy artifact under `<AWM_DIR>/models/`, never from an HF cache.

use sha2::{Digest, Sha256};
use std::io::Read;
use std::path::{Path, PathBuf};

/// The model name stored with every vector.
pub const MODEL_NAME: &str = "all-MiniLM-L6-v2";
/// The `sentence-transformers/all-MiniLM-L6-v2` revision the files come from.
pub const REVISION: &str = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41";
/// The vector dimension.
pub const DIM: usize = 384;
/// sentence-transformers' `max_seq_length`, which overrides tokenizer.json's 128.
pub const MAX_SEQ_LENGTH: usize = 256;

/// Each file the embedder loads, with its sha256.
pub const FILES: &[(&str, &str)] = &[
    ("model.onnx", "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"),
    ("tokenizer.json", "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037"),
];

/// `<awm_dir>/models/all-MiniLM-L6-v2/<revision>`.
pub fn model_dir(awm_dir: &Path) -> PathBuf {
    awm_dir.join("models").join(MODEL_NAME).join(REVISION)
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

/// Check every file of [`FILES`] in `dir` against its sha256.
pub fn verify(dir: &Path) -> Result<(), ModelFilesError> {
    for (name, expected) in FILES {
        let path = dir.join(name);
        let found = sha256_file(&path)?;
        if found != *expected {
            return Err(ModelFilesError::Mismatch { path, found, expected: expected.to_string() });
        }
    }
    Ok(())
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
