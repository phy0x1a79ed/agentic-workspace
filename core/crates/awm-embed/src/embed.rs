//! The embedders: MiniLM on tract-onnx, and a deterministic stub for tests.
//!
//! MiniLM reproduces sentence-transformers' `encode(normalize_embeddings=True)`: truncation at
//! 256 tokens, padding to the batch's longest, mean pooling over the attention mask, then L2.

use crate::model::{self, DIM, MAX_SEQ_LENGTH};
use awm_pycompat::re::words;
use sha1::{Digest, Sha1};
use std::path::Path;
use tokenizers::{PaddingParams, PaddingStrategy, Tokenizer, TruncationParams};
use tract_linalg::multithread::{multithread_tract_scope, Executor};
use tract_onnx::prelude::*;

/// sentence-transformers' `batch_size`.
pub const BATCH: usize = 32;

/// Why a text could not be embedded.
#[derive(Debug, thiserror::Error)]
pub enum EmbedError {
    /// This node cannot embed: `AWM_SEARCH=0`, or the model files are missing or wrong.
    #[error("embeddings unavailable: {0}")]
    Unavailable(String),
    #[error("embedding failed: {0}")]
    Failed(String),
}

/// Turns texts into unit vectors, and counts their tokens for the chunker.
pub trait Embedder: Send + Sync {
    /// The model name stored with each vector.
    fn name(&self) -> &str;
    fn dim(&self) -> usize;
    /// Tokens per text, uncapped and without special tokens.
    fn count_tokens(&self, texts: &[&str]) -> Vec<usize>;
    fn encode(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError>;
}

/// True when `AWM_SEARCH=0` turns embedding off, whatever is installed.
pub fn disabled() -> bool {
    std::env::var("AWM_SEARCH").is_ok_and(|v| v == "0")
}

type Plan = std::sync::Arc<TypedRunnableModel>;

/// all-MiniLM-L6-v2 on tract-onnx.
pub struct MiniLm {
    encoder: Tokenizer,
    counter: Tokenizer,
    plan: Plan,
    executor: Executor,
}

/// `AWM_EMBED_THREADS`, default 4, capped at the CPUs available.
fn threads() -> usize {
    let cpus = std::thread::available_parallelism().map_or(1, |n| n.get());
    let want = std::env::var("AWM_EMBED_THREADS").ok().and_then(|v| v.parse().ok()).unwrap_or(4usize);
    want.clamp(1, cpus)
}

impl MiniLm {
    /// Load from `<awm_dir>/models/`, after checking every file's sha256.
    pub fn load(awm_dir: &Path) -> Result<Self, EmbedError> {
        if disabled() {
            return Err(EmbedError::Unavailable("semantic search is turned off by AWM_SEARCH=0".into()));
        }
        Self::load_dir(&model::model_dir(awm_dir))
    }

    /// Load from `dir`, after checking every file's sha256.
    pub fn load_dir(dir: &Path) -> Result<Self, EmbedError> {
        model::verify(dir).map_err(|e| EmbedError::Unavailable(e.to_string()))?;
        let failed = |e: &dyn std::fmt::Display| EmbedError::Failed(e.to_string());
        let counter = Tokenizer::from_file(dir.join("tokenizer.json")).map_err(|e| failed(&e))?;
        let mut encoder = counter.clone();
        let mut counter = counter;
        counter.with_truncation(None).map_err(|e| failed(&e))?;
        counter.with_padding(None);
        encoder
            .with_truncation(Some(TruncationParams { max_length: MAX_SEQ_LENGTH, ..Default::default() }))
            .map_err(|e| failed(&e))?;
        encoder.with_padding(Some(PaddingParams { strategy: PaddingStrategy::BatchLongest, ..Default::default() }));
        let plan = tract_onnx::onnx()
            .model_for_path(dir.join("model.onnx"))
            .and_then(|m| m.into_optimized())
            .and_then(|m| m.into_runnable())
            .map_err(|e| failed(&e))?;
        let executor = match threads() {
            1 => Executor::SingleThread,
            n => Executor::multithread_with_name(n, "awm-embed"),
        };
        Ok(Self { encoder, counter, plan, executor })
    }

    fn encode_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        let failed = |e: &dyn std::fmt::Display| EmbedError::Failed(e.to_string());
        let enc = self.encoder.encode_batch(texts.to_vec(), true).map_err(|e| failed(&e))?;
        let rows = enc.len();
        let len = enc.iter().map(|e| e.len()).max().unwrap_or(0);
        let column = |f: &dyn Fn(&tokenizers::Encoding) -> &[u32]| -> Result<TValue, EmbedError> {
            let v: Vec<i64> = enc.iter().flat_map(|e| f(e).iter().map(|&x| x as i64)).collect();
            let a = tract_ndarray::Array2::from_shape_vec((rows, len), v).map_err(|e| failed(&e))?;
            Ok(a.into_tensor().into())
        };
        let ids = column(&|e| e.get_ids())?;
        let mask = column(&|e| e.get_attention_mask())?;
        let types = column(&|e| e.get_type_ids())?;
        let out = multithread_tract_scope(self.executor.clone(), || self.plan.run(tvec!(ids, mask, types)))
            .map_err(|e| failed(&e))?;
        let hidden = out[0].to_plain_array_view::<f32>().map_err(|e| failed(&e))?;
        if hidden.shape() != [rows, len, DIM] {
            return Err(EmbedError::Failed(format!("model output shape {:?}", hidden.shape())));
        }
        let hidden = hidden.as_slice().ok_or_else(|| EmbedError::Failed("model output is not contiguous".into()))?;
        Ok(enc.iter().enumerate().map(|(r, e)| pool(e.get_attention_mask(), &hidden[r * len * DIM..(r + 1) * len * DIM])).collect())
    }
}

/// Mean pooling over the attention mask, then L2 normalisation with torch's eps of 1e-12.
fn pool(mask: &[u32], hidden: &[f32]) -> Vec<f32> {
    let mut acc = vec![0f32; DIM];
    let mut n = 0f32;
    for (t, &m) in mask.iter().enumerate() {
        if m == 0 {
            continue;
        }
        n += 1.0;
        acc.iter_mut().zip(&hidden[t * DIM..(t + 1) * DIM]).for_each(|(a, x)| *a += x);
    }
    let n = n.max(1e-9);
    acc.iter_mut().for_each(|a| *a /= n);
    let norm = acc.iter().map(|x| x * x).sum::<f32>().sqrt().max(1e-12);
    acc.iter_mut().for_each(|a| *a /= norm);
    acc
}

impl Embedder for MiniLm {
    fn name(&self) -> &str {
        model::MODEL_NAME
    }

    fn dim(&self) -> usize {
        DIM
    }

    fn count_tokens(&self, texts: &[&str]) -> Vec<usize> {
        match self.counter.encode_batch(texts.to_vec(), false) {
            Ok(enc) => enc.iter().map(|e| e.len()).collect(),
            Err(_) => crate::chunk::approx_tokens(texts),
        }
    }

    /// Batches by descending length, as sentence-transformers does, so a batch pads little.
    fn encode(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        let mut order: Vec<usize> = (0..texts.len()).collect();
        order.sort_by_key(|&i| std::cmp::Reverse(texts[i].chars().count()));
        let mut out = vec![Vec::new(); texts.len()];
        for batch in order.chunks(BATCH) {
            let sorted: Vec<&str> = batch.iter().map(|&i| texts[i]).collect();
            for (&i, v) in batch.iter().zip(self.encode_batch(&sorted)?) {
                out[i] = v;
            }
        }
        Ok(out)
    }
}

/// Bag of words hashed into `dim` dimensions: texts that share words score high.
pub struct Stub {
    pub name: String,
    pub dim: usize,
    pub encoded: std::sync::atomic::AtomicUsize,
}

impl Stub {
    pub fn new(name: &str, dim: usize) -> Self {
        Self { name: name.into(), dim, encoded: 0.into() }
    }
}

impl Default for Stub {
    fn default() -> Self {
        Self::new("stub-a", DIM)
    }
}

impl Embedder for Stub {
    fn name(&self) -> &str {
        &self.name
    }

    fn dim(&self) -> usize {
        self.dim
    }

    fn count_tokens(&self, texts: &[&str]) -> Vec<usize> {
        crate::chunk::approx_tokens(texts)
    }

    fn encode(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        self.encoded.fetch_add(texts.len(), std::sync::atomic::Ordering::Relaxed);
        Ok(texts
            .iter()
            .map(|t| {
                let mut v = vec![0f32; self.dim];
                for w in words(&awm_pycompat::pystr::lower(t)) {
                    let h = Sha1::digest(w.as_bytes());
                    let k = u64::from_be_bytes(h[..8].try_into().unwrap()) % self.dim as u64;
                    v[k as usize] += 1.0;
                }
                let norm = v.iter().map(|x| x * x).sum::<f32>().sqrt().max(1e-9);
                v.iter_mut().for_each(|x| *x /= norm);
                v
            })
            .collect())
    }
}

/// A vector as the stored blob: f32 little-endian.
pub fn to_blob(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

/// A stored blob as a vector, or `None` when its length is not a multiple of 4.
pub fn from_blob(b: &[u8]) -> Option<Vec<f32>> {
    (b.len() % 4 == 0).then(|| b.chunks_exact(4).map(|c| f32::from_le_bytes(c.try_into().unwrap())).collect())
}
