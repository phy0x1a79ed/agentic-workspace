use awm_embed::embed::{from_blob, to_blob};
use awm_embed::model::ModelFilesError;
use awm_embed::{Onnx, MINILM};

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
    assert!(matches!(MINILM.verify(dir.path()), Err(ModelFilesError::Missing(_))));
    std::fs::write(dir.path().join("model.onnx"), b"not a model").unwrap();
    assert!(matches!(MINILM.verify(dir.path()), Err(ModelFilesError::Mismatch { .. })));
    assert!(Onnx::load_dir(dir.path(), &MINILM).is_err());
}

