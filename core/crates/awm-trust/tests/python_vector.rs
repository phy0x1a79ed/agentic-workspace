//! A grant signed by the Python twin (`awm.trustpolicy`) verifies here, so the
//! two canonical JSON writers agree byte for byte.

use awm_trust::grant::{verify_grant, Expect};
use base64::Engine;
use serde_json::Value;

#[test]
fn python_signed_grant_verifies() {
    let vec: Value = serde_json::from_str(include_str!("python_vector.json")).unwrap();
    let pk: [u8; 32] = base64::engine::general_purpose::STANDARD
        .decode(vec["public_key_b64"].as_str().unwrap())
        .unwrap()
        .try_into()
        .unwrap();
    let expect = Expect { kind: "trust_raise", node: Some("home"), subject: Some("sé1"), payload: Some(&vec["payload"]) };
    assert_eq!(verify_grant(&vec["envelope"], &pk, &expect, 150).unwrap().grant_id, "g1");
}
