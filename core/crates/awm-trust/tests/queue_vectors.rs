//! The grant vectors operations signs (awm/plugins/operations/grantlib/test_vectors.json) pass here too.

use awm_trust::grant::{key_id, load_public_key, verify_grant, Expect};
use serde_json::Value;

#[test]
fn queue_vectors() {
    let vec: Value = serde_json::from_str(include_str!("../../../../awm/plugins/operations/grantlib/test_vectors.json")).unwrap();
    let pk = load_public_key(vec["public_key_b64u"].as_str().unwrap()).unwrap();
    assert_eq!(key_id(&pk), vec["key_id"].as_str().unwrap());
    for case in vec["cases"].as_array().unwrap().iter().filter(|c| c["check"] == "grant") {
        let expect = Expect { kind: case["kind"].as_str(), node: case["node"].as_str(), ..Default::default() };
        let got = verify_grant(&case["envelope"], &pk, &expect, case["now"].as_i64().unwrap());
        assert_eq!(got.is_ok(), case["expect"] == "ok", "{}: {:?}", case["name"], got.err());
    }
}
