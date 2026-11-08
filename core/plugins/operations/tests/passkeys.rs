//! Passkeys as py_webauthn checks them, against the soft authenticator and py_webauthn's own vectors.
//!
//! `passkey_vectors.json` is synthetic. py_webauthn 3.0.1 wrote it, with the soft passkey in
//! `awm/plugins/operations/tests/softauthn.py`, from fixed challenges and user id: the option JSON
//! for both ceremonies, and a registration and an assertion that Python verified and stored.

use awm_plugin_operations::passkeys::soft::{SoftAuthenticator, UP, UV};
use awm_plugin_operations::passkeys::{
    assertion_options, parse_cose, registration_options, verify_assertion, verify_registration, PasskeyError, ALG_EDDSA, ALG_ES256,
};
use awm_trust::grant::{b64u, unb64u};
use ciborium::Value as Cbor;
use serde_json::{json, Value};

const RP: &str = "approver.example";
const ORIGIN: &str = "https://approver.example";
const REG_CHALLENGE: &[u8] = b"registration-challenge-0123456789";
const ASR_CHALLENGE: &[u8] = b"assertion-challenge-0123456789ab";

fn vectors() -> Value {
    serde_json::from_str(include_str!("passkey_vectors.json")).unwrap()
}

fn bytes(v: &Value) -> Vec<u8> {
    unb64u(v.as_str().unwrap()).unwrap()
}

fn reg_options() -> Value {
    registration_options(RP, b"user-1", "tony", REG_CHALLENGE)
}

/// A registered authenticator, with what the approver stores: `(cred id, COSE key, sign count)`.
fn enrolled(mut auth: SoftAuthenticator) -> (SoftAuthenticator, String, String, u32) {
    let cred = auth.register(&reg_options());
    let (id, cose, count) = verify_registration(&cred, REG_CHALLENGE, ORIGIN, RP).unwrap();
    (auth, id, cose, count)
}

fn asr_options(id: &str) -> Value {
    assertion_options(RP, ASR_CHALLENGE, &[id.to_string()])
}

fn reg_err(cred: &Value) -> String {
    verify_registration(cred, REG_CHALLENGE, ORIGIN, RP).unwrap_err().to_string()
}

fn asr_err(cred: &Value, cose: &str, count: u32) -> String {
    verify_assertion(cred, ASR_CHALLENGE, ORIGIN, RP, cose, count).unwrap_err().to_string()
}

#[test]
fn options_match_py_webauthn_byte_for_byte() {
    let v = vectors();
    let reg = registration_options(v["rp_id"].as_str().unwrap(), &bytes(&v["user_id"]), v["user_name"].as_str().unwrap(), &bytes(&v["registration_challenge"]));
    assert_eq!(serde_json::to_string(&reg).unwrap(), serde_json::to_string(&v["registration_options"]).unwrap());
    let ids: Vec<String> = v["cred_ids"].as_array().unwrap().iter().map(|c| c.as_str().unwrap().to_string()).collect();
    let asr = assertion_options(v["rp_id"].as_str().unwrap(), &bytes(&v["assertion_challenge"]), &ids);
    assert_eq!(serde_json::to_string(&asr).unwrap(), serde_json::to_string(&v["assertion_options"]).unwrap());
}

#[test]
fn rust_accepts_what_python_registered_and_stored() {
    let v = vectors();
    let py = &v["python"];
    let (rp, origin) = (v["rp_id"].as_str().unwrap(), v["origin"].as_str().unwrap());
    let (id, cose, count) = verify_registration(&py["registration"], &bytes(&v["registration_challenge"]), origin, rp).unwrap();
    assert_eq!(json!([id, cose, count]), json!([py["cred_id"], py["cose_pub"], py["sign_count"]]));
    let stored = py["cose_pub"].as_str().unwrap();
    assert_eq!(parse_cose(stored).unwrap().alg(), ALG_ES256);
    let new = verify_assertion(&py["assertion"], &bytes(&v["assertion_challenge"]), origin, rp, stored, py["sign_count"].as_u64().unwrap() as u32).unwrap();
    assert_eq!(json!(new), py["new_sign_count"]);
    let replay = verify_assertion(&py["assertion"], &bytes(&v["assertion_challenge"]), origin, rp, stored, new).unwrap_err();
    assert!(replay.to_string().contains("was not greater than current count"), "{replay}");
}

#[test]
fn register_then_assert_round_trips() {
    for (label, auth, alg) in [
        ("p256", SoftAuthenticator::new(ORIGIN, RP), ALG_ES256),
        ("ed25519", SoftAuthenticator::new_ed25519(ORIGIN, RP), ALG_EDDSA),
    ] {
        let (mut auth, id, cose, count) = enrolled(auth);
        assert_eq!(id, b64u(&auth.cred_id), "{label}");
        assert_eq!(unb64u(&cose).unwrap(), auth.cose(), "{label}: the COSE key is stored as the authenticator sent it");
        assert_eq!(count, 0);
        assert_eq!(parse_cose(&cose).unwrap().alg(), alg);
        let first = auth.assert(&asr_options(&id));
        assert_eq!(first["response"]["userHandle"], json!(b64u(b"user-1")));
        assert_eq!(verify_assertion(&first, ASR_CHALLENGE, ORIGIN, RP, &cose, count).unwrap(), 1, "{label}");
        let second = auth.assert(&asr_options(&id));
        assert_eq!(verify_assertion(&second, ASR_CHALLENGE, ORIGIN, RP, &cose, 1).unwrap(), 2, "{label}");
    }
}

#[test]
fn packed_self_attestation_verifies() {
    for mut auth in [SoftAuthenticator::new(ORIGIN, RP), SoftAuthenticator::new_ed25519(ORIGIN, RP)] {
        auth.packed = true;
        let (_, _, cose, _) = enrolled(auth);
        assert!(parse_cose(&cose).is_ok());
    }
    let mut auth = SoftAuthenticator::new(ORIGIN, RP);
    auth.packed = true;
    let mut cred = auth.register(&reg_options());
    let att = bytes(&cred["response"]["attestationObject"]);
    let Cbor::Map(mut map) = ciborium::from_reader::<Cbor, _>(att.as_slice()).unwrap() else { panic!() };
    for (k, v) in map.iter_mut() {
        if k.as_text() == Some("attStmt") {
            let other = SoftAuthenticator::new(ORIGIN, RP).sign(b"other data");
            *v = Cbor::Map(vec![(Cbor::Text("alg".into()), Cbor::Integer(ALG_ES256.into())), (Cbor::Text("sig".into()), Cbor::Bytes(other))]);
        }
    }
    let mut out = Vec::new();
    ciborium::into_writer(&Cbor::Map(map), &mut out).unwrap();
    cred["response"]["attestationObject"] = json!(b64u(&out));
    assert_eq!(reg_err(&cred), "registration refused: Could not verify attestation statement signature (Packed|Self)");
}

#[test]
fn registration_refusals() {
    let cred = SoftAuthenticator::new(ORIGIN, RP).register(&reg_options());
    let other = verify_registration(&cred, b"another challenge", ORIGIN, RP).unwrap_err();
    assert_eq!(other, PasskeyError::Registration("Client data challenge was not expected challenge".into()));
    assert_eq!(
        verify_registration(&cred, REG_CHALLENGE, "https://evil.example", RP).unwrap_err().to_string(),
        "registration refused: Unexpected client data origin \"https://approver.example\", expected \"https://evil.example\""
    );
    assert_eq!(verify_registration(&cred, REG_CHALLENGE, ORIGIN, "evil.example").unwrap_err().to_string(), "registration refused: Unexpected RP ID hash");
    assert_eq!(reg_err(&SoftAuthenticator::new(ORIGIN, "evil.example").register(&reg_options())), "registration refused: Unexpected RP ID hash");

    let mut auth = SoftAuthenticator::new(ORIGIN, RP);
    auth.flags = UP;
    assert_eq!(reg_err(&auth.register(&reg_options())), "registration refused: User verification is required but user was not verified during attestation");
    auth.flags = UV;
    assert_eq!(reg_err(&auth.register(&reg_options())), "registration refused: User presence was required, but was not present during attestation");

    let mut auth = SoftAuthenticator::new(ORIGIN, RP);
    auth.alg = -35;
    assert_eq!(reg_err(&auth.register(&reg_options())), "registration refused: Unsupported credential public key alg \"-35\", expected one of: [-8, -7, -257]");

    let mut wrong_id = cred.clone();
    wrong_id["id"] = json!(b64u(b"another id"));
    assert_eq!(reg_err(&wrong_id), "registration refused: id and raw_id were not equivalent");
    let mut wrong_type = cred.clone();
    wrong_type["type"] = json!("password");
    assert_eq!(reg_err(&wrong_type), "registration refused: Credential had unexpected type");

    let assertion = {
        let (mut auth, id, _, _) = enrolled(SoftAuthenticator::new(ORIGIN, RP));
        auth.assert(&asr_options(&id))
    };
    let mut get_as_create = cred.clone();
    get_as_create["response"]["clientDataJSON"] = assertion["response"]["clientDataJSON"].clone();
    assert!(reg_err(&get_as_create).contains("Unexpected client data type \"webauthn.get\""));
    assert!(reg_err(&json!({"id": "x"})).starts_with("registration refused: "));
    assert!(reg_err(&json!("not an object")).starts_with("registration refused: "));
}

#[test]
fn assertion_refusals() {
    let (mut auth, id, cose, count) = enrolled(SoftAuthenticator::new(ORIGIN, RP));
    let good = auth.assert(&asr_options(&id));
    assert_eq!(
        verify_assertion(&good, b"another challenge", ORIGIN, RP, &cose, count).unwrap_err(),
        PasskeyError::Assertion("Client data challenge was not expected challenge".into())
    );
    assert!(verify_assertion(&good, ASR_CHALLENGE, "https://evil.example", RP, &cose, count).unwrap_err().to_string().starts_with("passkey refused: Unexpected client data origin"));
    assert_eq!(verify_assertion(&good, ASR_CHALLENGE, ORIGIN, "evil.example", &cose, count).unwrap_err().to_string(), "passkey refused: Unexpected RP ID hash");
    assert_eq!(asr_err(&good, &cose, 1), "passkey refused: Response sign count of 1 was not greater than current count of 1");
    assert_eq!(asr_err(&good, &cose, 7), "passkey refused: Response sign count of 1 was not greater than current count of 7");
    assert_eq!(verify_assertion(&good, ASR_CHALLENGE, ORIGIN, RP, &cose, count).unwrap(), 1);

    auth.flags = UP;
    assert_eq!(asr_err(&auth.assert(&asr_options(&id)), &cose, 1), "passkey refused: User verification is required but user was not verified during authentication");
    auth.flags = UV;
    assert_eq!(asr_err(&auth.assert(&asr_options(&id)), &cose, 1), "passkey refused: User was not present during authentication");
    auth.flags = UP | UV;

    let next = auth.assert(&asr_options(&id));
    let mut swapped = next.clone();
    swapped["response"]["signature"] = good["response"]["signature"].clone();
    assert_eq!(asr_err(&swapped, &cose, 1), "passkey refused: Could not verify authentication signature");
    let (_, _, other_cose, _) = enrolled(SoftAuthenticator::new_ed25519(ORIGIN, RP));
    assert_eq!(asr_err(&next, &other_cose, 1), "passkey refused: Could not verify authentication signature");

    let mut auth0 = SoftAuthenticator::new(ORIGIN, RP);
    auth0.sign_count = u32::MAX;
    let (mut auth0, id0, cose0, _) = enrolled(auth0);
    let zero = auth0.assert(&asr_options(&id0));
    assert_eq!(verify_assertion(&zero, ASR_CHALLENGE, ORIGIN, RP, &cose0, 0).unwrap(), 0, "a counter that stays 0 passes, as py_webauthn allows");

    let mut create_as_get = next.clone();
    create_as_get["response"]["clientDataJSON"] = SoftAuthenticator::new(ORIGIN, RP).register(&reg_options())["response"]["clientDataJSON"].clone();
    assert!(asr_err(&create_as_get, &cose, 1).contains("Unexpected client data type \"webauthn.create\""));
    let mut bad_handle = next.clone();
    bad_handle["response"]["userHandle"] = json!(5);
    assert_eq!(asr_err(&bad_handle, &cose, 1), "passkey refused: Credential response had unexpected userHandle");
    assert!(asr_err(&next, "AAAA", 1).starts_with("passkey refused: "));
}

#[test]
fn parse_cose_refuses_what_it_cannot_use() {
    assert!(matches!(parse_cose("!!"), Err(PasskeyError::Key(_))));
    assert!(parse_cose(&b64u(b"\xa1\x01\x02")).is_err());
    let mut auth = SoftAuthenticator::new(ORIGIN, RP);
    auth.alg = ALG_EDDSA;
    assert!(parse_cose(&b64u(&auth.cose())).unwrap_err().to_string().contains("does not take alg"));
    let point = p256::ecdsa::SigningKey::random(&mut rand::rngs::OsRng).verifying_key().to_encoded_point(false);
    assert_eq!(parse_cose(&b64u(point.as_bytes())).unwrap().alg(), ALG_ES256, "the uncompressed U2F form py_webauthn accepts");
}
