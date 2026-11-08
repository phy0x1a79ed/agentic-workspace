//! The grant vectors grantlib signs (awm/service_components/grantlib/test_vectors.json) pass
//! here too, with the same error text, so the Python and Rust checks agree byte for byte.

use awm_trust::grant::{key_id, load_public_key, verify_grant, verify_order, Expect};
use serde_json::Value;

#[test]
fn grant_vectors() {
    let vec: Value = serde_json::from_str(include_str!("../../../../awm/service_components/grantlib/test_vectors.json")).unwrap();
    let pk = load_public_key(vec["public_key_b64u"].as_str().unwrap()).unwrap();
    assert_eq!(key_id(&pk), vec["key_id"].as_str().unwrap());
    for case in vec["cases"].as_array().unwrap() {
        let now = case["now"].as_i64().unwrap();
        let got = if case["check"] == "grant" {
            let expect = Expect {
                kind: case["kind"].as_str(),
                node: case["node"].as_str(),
                subject: case["subject"].as_str(),
                payload: case.get("payload"),
            };
            verify_grant(&case["envelope"], &pk, &expect, now)
        } else {
            verify_order(&case["envelope"], &pk, case["node"].as_str().unwrap(), now)
        };
        let want = case["expect"].as_str().unwrap();
        match got {
            Ok(_) => assert_eq!(want, "ok", "{}", case["name"]),
            Err(e) => assert!(want != "ok" && e.to_string().contains(want), "{}: {e}", case["name"]),
        }
    }
}
