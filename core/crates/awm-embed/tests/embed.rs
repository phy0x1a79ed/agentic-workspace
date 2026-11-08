use awm_embed::embed::{from_blob, to_blob};
use awm_embed::model::{self, ModelFilesError};
use awm_embed::{Embedder, MiniLm, Stub};

fn cos(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

#[test]
fn stub_scores_shared_words_high() {
    let s = Stub::default();
    let v = s.encode(&["deploy the release", "Release deploy notes", "banana bread"]).unwrap();
    assert_eq!(v[0].len(), 384);
    assert!(cos(&v[0], &v[1]) > cos(&v[0], &v[2]));
    assert!((cos(&v[0], &v[0]) - 1.0).abs() < 1e-6);
    assert_eq!(s.encoded.load(std::sync::atomic::Ordering::Relaxed), 3);
    assert_eq!(s.count_tokens(&["a b, c"]), vec![4]);
}

#[test]
fn blob_round_trip() {
    let v = vec![0.5f32, -1.25, 3.0e-7];
    let b = to_blob(&v);
    assert_eq!(b.len(), 12);
    assert_eq!(&b[..4], &0.5f32.to_le_bytes());
    assert_eq!(from_blob(&b).unwrap(), v);
    assert!(from_blob(&b[..5]).is_none());
}

#[test]
fn verify_refuses_missing_and_wrong_files() {
    let dir = tempfile::tempdir().unwrap();
    assert!(matches!(model::verify(dir.path()), Err(ModelFilesError::Missing(_))));
    std::fs::write(dir.path().join("model.onnx"), b"not a model").unwrap();
    assert!(matches!(model::verify(dir.path()), Err(ModelFilesError::Mismatch { .. })));
    assert!(MiniLm::load_dir(dir.path()).is_err());
}

#[test]
fn model_dir_is_pinned_by_revision() {
    let d = model::model_dir("/x/.awm".as_ref());
    assert_eq!(d, std::path::PathBuf::from(format!("/x/.awm/models/all-MiniLM-L6-v2/{}", model::REVISION)));
}

/// Runs only where `AWM_EMBED_MODEL_DIR` points at the pinned model files.
#[test]
fn minilm_matches_sentence_transformers_shape() {
    let Ok(dir) = std::env::var("AWM_EMBED_MODEL_DIR") else { return };
    let m = MiniLm::load_dir(dir.as_ref()).unwrap();
    let long = "word ".repeat(400);
    let texts = ["deploy the release", "Release deploy notes", "banana bread", long.as_str()];
    let v = m.encode(&texts).unwrap();
    for x in &v {
        assert_eq!(x.len(), 384);
        assert!((cos(x, x) - 1.0).abs() < 1e-5);
    }
    assert!(cos(&v[0], &v[1]) > cos(&v[0], &v[2]));
    let one = m.encode(&texts[..1]).unwrap();
    assert!(cos(&one[0], &v[0]) > 0.99999);
    assert_eq!(m.count_tokens(&[long.as_str()]), vec![400]);
}
