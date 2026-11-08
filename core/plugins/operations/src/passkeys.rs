//! WebAuthn registration and assertion with user verification required (R64 item 4), as py_webauthn checks them.

use awm_trust::grant::{b64u, unb64u};
use ciborium::Value as Cbor;
use rand::RngCore;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

/// The relying party name the approver shows in the passkey prompt.
pub const RP_NAME: &str = "awm approver";
/// The ceremony timeout in milliseconds, py_webauthn's default.
pub const TIMEOUT_MS: u64 = 60000;
/// COSE ES256: ECDSA P-256 with SHA-256.
pub const ALG_ES256: i64 = -7;
/// COSE EdDSA: Ed25519.
pub const ALG_EDDSA: i64 = -8;
/// COSE RS256: RSASSA-PKCS1-v1_5 with SHA-256.
pub const ALG_RS256: i64 = -257;
/// The algorithms offered and accepted, in py_webauthn's default order.
pub const SUPPORTED_ALGS: [i64; 3] = [ALG_EDDSA, ALG_ES256, ALG_RS256];

const FLAG_UP: u8 = 1 << 0;
const FLAG_UV: u8 = 1 << 2;
const FLAG_BE: u8 = 1 << 3;
const FLAG_BS: u8 = 1 << 4;
const FLAG_AT: u8 = 1 << 6;
const FLAG_ED: u8 = 1 << 7;

/// A passkey response that fails a check.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum PasskeyError {
    /// The registration (`navigator.credentials.create`) response.
    #[error("registration refused: {0}")]
    Registration(String),
    /// The assertion (`navigator.credentials.get`) response.
    #[error("passkey refused: {0}")]
    Assertion(String),
    /// A stored COSE public key.
    #[error("passkey key refused: {0}")]
    Key(String),
}

/// A credential public key decoded from COSE.
#[derive(Debug, Clone)]
pub enum CoseKey {
    /// ES256 over P-256.
    Es256(p256::ecdsa::VerifyingKey),
    /// EdDSA over Ed25519.
    EdDsa(ed25519_dalek::VerifyingKey),
    /// RS256.
    Rs256(rsa::RsaPublicKey),
}

impl CoseKey {
    /// The key's COSE algorithm.
    pub fn alg(&self) -> i64 {
        match self {
            CoseKey::Es256(_) => ALG_ES256,
            CoseKey::EdDsa(_) => ALG_EDDSA,
            CoseKey::Rs256(_) => ALG_RS256,
        }
    }

    /// Whether `sig` (DER for ES256, raw for EdDSA, PKCS#1 v1.5 for RS256) signs `data`.
    pub fn verify(&self, data: &[u8], sig: &[u8]) -> bool {
        use ed25519_dalek::Verifier as _;
        match self {
            CoseKey::Es256(key) => p256::ecdsa::Signature::from_der(sig).is_ok_and(|s| key.verify(data, &s).is_ok()),
            CoseKey::EdDsa(key) => ed25519_dalek::Signature::from_slice(sig).is_ok_and(|s| key.verify(data, &s).is_ok()),
            CoseKey::Rs256(key) => {
                key.verify(rsa::Pkcs1v15Sign::new::<Sha256>(), &Sha256::digest(data), sig).is_ok()
            }
        }
    }
}

fn cbor_get<'a>(map: &'a [(Cbor, Cbor)], key: &Cbor) -> Option<&'a Cbor> {
    map.iter().find(|(k, _)| k == key).map(|(_, v)| v)
}

fn cose_int(map: &[(Cbor, Cbor)], label: i64) -> Option<i128> {
    cbor_get(map, &Cbor::Integer(label.into())).and_then(Cbor::as_integer).map(i128::from)
}

fn cose_bytes<'a>(map: &'a [(Cbor, Cbor)], label: i64, what: &str) -> Result<&'a [u8], String> {
    match cbor_get(map, &Cbor::Integer(label.into())) {
        Some(Cbor::Bytes(b)) if !b.is_empty() => Ok(b),
        _ => Err(format!("credential public key missing {what}")),
    }
}

fn supported_algs() -> String {
    format!("[{}]", SUPPORTED_ALGS.map(|a| a.to_string()).join(", "))
}

fn decode_cose(bytes: &[u8]) -> Result<CoseKey, String> {
    if bytes.first() == Some(&0x04) {
        let key = p256::ecdsa::VerifyingKey::from_sec1_bytes(bytes).map_err(|_| "uncompressed EC2 public key is not a P-256 point".to_string())?;
        return Ok(CoseKey::Es256(key));
    }
    let mut rest = bytes;
    let value: Cbor = ciborium::from_reader(&mut rest).map_err(|e| format!("credential public key is not CBOR: {e}"))?;
    if !rest.is_empty() {
        return Err("credential public key has trailing bytes".into());
    }
    let Cbor::Map(map) = value else { return Err("credential public key is not a CBOR map".into()) };
    let kty = cose_int(&map, 1).ok_or("Credential public key missing kty")?;
    let alg = cose_int(&map, 3).ok_or("Credential public key missing alg")?;
    if !SUPPORTED_ALGS.iter().any(|a| i128::from(*a) == alg) {
        return Err(format!("Unsupported credential public key alg \"{alg}\", expected one of: {}", supported_algs()));
    }
    let crv = cose_int(&map, -1);
    match (kty, alg) {
        (2, a) if a == ALG_ES256.into() => {
            if crv != Some(1) {
                return Err(format!("EC2 credential public key curve {crv:?} is not P-256"));
            }
            let (x, y) = (cose_bytes(&map, -2, "x")?, cose_bytes(&map, -3, "y")?);
            if x.len() != 32 || y.len() != 32 {
                return Err("EC2 credential public key coordinates are not 32 bytes".into());
            }
            let point = p256::EncodedPoint::from_affine_coordinates(p256::FieldBytes::from_slice(x), p256::FieldBytes::from_slice(y), false);
            let key = p256::ecdsa::VerifyingKey::from_encoded_point(&point).map_err(|_| "EC2 credential public key is not a P-256 point".to_string())?;
            Ok(CoseKey::Es256(key))
        }
        (1, a) if a == ALG_EDDSA.into() => {
            if crv != Some(6) {
                return Err(format!("OKP public key with alg {alg} and crv {crv:?} is not supported"));
            }
            let x: [u8; 32] = cose_bytes(&map, -2, "x")?.try_into().map_err(|_| "OKP credential public key is not 32 bytes".to_string())?;
            let key = ed25519_dalek::VerifyingKey::from_bytes(&x).map_err(|_| "OKP credential public key is not an Ed25519 point".to_string())?;
            Ok(CoseKey::EdDsa(key))
        }
        (3, a) if a == ALG_RS256.into() => {
            let n = rsa::BigUint::from_bytes_be(cose_bytes(&map, -1, "n")?);
            let e = rsa::BigUint::from_bytes_be(cose_bytes(&map, -2, "e")?);
            let key = rsa::RsaPublicKey::new(n, e).map_err(|e| format!("RSA credential public key is invalid: {e}"))?;
            Ok(CoseKey::Rs256(key))
        }
        (1..=3, _) => Err(format!("credential public key kty {kty} does not take alg {alg}")),
        _ => Err(format!("Unsupported credential public key type \"{kty}\"")),
    }
}

/// The stored COSE public key `b64u`, decoded; check-state uses it to prove each stored credential parses.
pub fn parse_cose(cose_b64u: &str) -> Result<CoseKey, PasskeyError> {
    let bytes = unb64u(cose_b64u).ok_or_else(|| PasskeyError::Key("not base64url".into()))?;
    decode_cose(&bytes).map_err(PasskeyError::Key)
}

fn challenge_or_random(challenge: &[u8]) -> Vec<u8> {
    if !challenge.is_empty() {
        return challenge.to_vec();
    }
    let mut fresh = vec![0u8; 64];
    rand::rngs::OsRng.fill_bytes(&mut fresh);
    fresh
}

/// The creation options py_webauthn gives for this user: resident key preferred, UV required, attestation none.
pub fn registration_options(rp_id: &str, user_id: &[u8], user_name: &str, challenge: &[u8]) -> Value {
    let params: Vec<Value> = SUPPORTED_ALGS.iter().map(|alg| json!({"type": "public-key", "alg": alg})).collect();
    json!({
        "rp": {"name": RP_NAME, "id": rp_id},
        "user": {"id": b64u(&challenge_or_random(user_id)), "name": user_name, "displayName": user_name},
        "challenge": b64u(&challenge_or_random(challenge)),
        "pubKeyCredParams": params,
        "timeout": TIMEOUT_MS,
        "excludeCredentials": [],
        "authenticatorSelection": {"residentKey": "preferred", "requireResidentKey": false, "userVerification": "required"},
        "attestation": "none",
    })
}

/// The request options py_webauthn gives for these credentials, UV required.
pub fn assertion_options(rp_id: &str, challenge: &[u8], cred_ids: &[String]) -> Value {
    let allow: Vec<Value> = cred_ids
        .iter()
        .map(|c| json!({"id": unb64u(c).map_or_else(|| c.clone(), |b| b64u(&b)), "type": "public-key"}))
        .collect();
    json!({
        "challenge": b64u(&challenge_or_random(challenge)),
        "timeout": TIMEOUT_MS,
        "rpId": rp_id,
        "allowCredentials": allow,
        "userVerification": "required",
    })
}

struct Attested {
    cred_id: Vec<u8>,
    cose: Vec<u8>,
}

struct AuthData {
    rp_id_hash: Vec<u8>,
    flags: u8,
    sign_count: u32,
    attested: Option<Attested>,
}

fn cbor_item_len(bytes: &[u8], what: &str) -> Result<usize, String> {
    let mut rest = bytes;
    let _: Cbor = ciborium::from_reader(&mut rest).map_err(|e| format!("{what} is not CBOR: {e}"))?;
    Ok(bytes.len() - rest.len())
}

/// py_webauthn's fix for authenticators that encode an Ed25519 key's map header as 3 items, not 4.
const BAD_EDDSA_COSE: [u8; 17] = [0xa3, 0x01, 0x63, 0x4f, 0x4b, 0x50, 0x03, 0x27, 0x20, 0x67, 0x45, 0x64, 0x32, 0x35, 0x35, 0x31, 0x39];

fn parse_auth_data(raw: &[u8]) -> Result<AuthData, String> {
    if raw.len() < 37 {
        return Err(format!("Authenticator data was {} bytes, expected at least 37 bytes", raw.len()));
    }
    let flags = raw[32];
    let mut count = [0u8; 4];
    count.copy_from_slice(&raw[33..37]);
    let mut data = AuthData { rp_id_hash: raw[..32].to_vec(), flags, sign_count: u32::from_be_bytes(count), attested: None };
    let mut buf = raw.to_vec();
    let mut at = 37;
    let short = || "Authenticator data is truncated".to_string();
    if flags & FLAG_AT != 0 {
        let len_at = at + 16;
        let len_bytes = buf.get(len_at..len_at + 2).ok_or_else(short)?;
        let id_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
        at = len_at + 2;
        let cred_id = buf.get(at..at + id_len).ok_or_else(short)?.to_vec();
        at += id_len;
        if buf[at..].starts_with(&BAD_EDDSA_COSE) {
            buf[at] = 0xa4;
        }
        let key_len = cbor_item_len(&buf[at..], "credential public key")?;
        let cose = buf[at..at + key_len].to_vec();
        at += key_len;
        data.attested = Some(Attested { cred_id, cose });
    }
    if flags & FLAG_ED != 0 {
        at += cbor_item_len(&buf[at..], "extensions")?;
    }
    if buf.len() > at {
        return Err("Leftover bytes detected while parsing authenticator data".into());
    }
    if flags & FLAG_BE == 0 && flags & FLAG_BS != 0 {
        return Err("Single-device credential indicated that it was backed up, which should be impossible.".into());
    }
    Ok(data)
}

fn json_str<'a>(obj: &'a Map<String, Value>, key: &str, err: &str) -> Result<&'a str, String> {
    obj.get(key).and_then(Value::as_str).ok_or_else(|| err.to_string())
}

fn json_b64u(obj: &Map<String, Value>, key: &str, err: &str) -> Result<Vec<u8>, String> {
    unb64u(json_str(obj, key, err)?).ok_or_else(|| format!("{key} is not base64url"))
}

/// The credential JSON's common parts: `(response, clientDataJSON)`, once id, rawId and type check out.
fn parse_credential(credential: &Value) -> Result<(&Map<String, Value>, Vec<u8>), String> {
    let cred = credential.as_object().ok_or("Credential was not a JSON object")?;
    let id = json_str(cred, "id", "Credential missing required id")?;
    let raw_id = json_b64u(cred, "rawId", "Credential missing required rawId")?;
    let response = cred.get("response").and_then(Value::as_object).ok_or("Credential missing required response")?;
    let cdj = json_b64u(response, "clientDataJSON", "Credential response missing required clientDataJSON")?;
    if cred.get("type").and_then(Value::as_str) != Some("public-key") {
        return Err("Credential had unexpected type".into());
    }
    if let Some(att) = cred.get("authenticatorAttachment").and_then(Value::as_str) {
        if att != "platform" && att != "cross-platform" {
            return Err("Credential had unexpected authenticatorAttachment".into());
        }
    }
    if b64u(&raw_id) != id {
        return Err("id and raw_id were not equivalent".into());
    }
    Ok((response, cdj))
}

fn check_client_data(cdj: &[u8], kind: &str, challenge: &[u8], origin: &str) -> Result<(), String> {
    let malformed = || "clientDataJSON was malformed".to_string();
    let data: Value = serde_json::from_slice(cdj).map_err(|_| malformed())?;
    let data = data.as_object().ok_or_else(malformed)?;
    let (Some(got_type), Some(got_challenge), Some(got_origin)) = (data.get("type"), data.get("challenge"), data.get("origin")) else {
        return Err(malformed());
    };
    if got_type.as_str() != Some(kind) {
        return Err(format!("Unexpected client data type \"{}\", expected \"{kind}\"", got_type.as_str().unwrap_or_default()));
    }
    if got_challenge.as_str().and_then(unb64u).as_deref() != Some(challenge) {
        return Err("Client data challenge was not expected challenge".into());
    }
    if got_origin.as_str() != Some(origin) {
        return Err(format!("Unexpected client data origin \"{}\", expected \"{origin}\"", got_origin.as_str().unwrap_or_default()));
    }
    if let Some(binding) = data.get("tokenBinding").and_then(Value::as_object) {
        let status = binding.get("status").ok_or_else(malformed)?;
        if status.as_str() == Some("not-supported") {
            return Err("Unexpected token_binding status of \"not-supported\", expected one of \"supported,present\"".into());
        }
    }
    Ok(())
}

fn check_flags(data: &AuthData, rp_id: &str, ceremony: &str) -> Result<(), String> {
    if data.rp_id_hash != Sha256::digest(rp_id.as_bytes()).as_slice() {
        return Err("Unexpected RP ID hash".into());
    }
    if data.flags & FLAG_UP == 0 {
        return Err(match ceremony {
            "attestation" => "User presence was required, but was not present during attestation".into(),
            _ => "User was not present during authentication".into(),
        });
    }
    if data.flags & FLAG_UV == 0 {
        return Err(format!("User verification is required but user was not verified during {ceremony}"));
    }
    Ok(())
}

fn signed_data(auth_data: &[u8], cdj: &[u8]) -> Vec<u8> {
    let mut data = auth_data.to_vec();
    data.extend_from_slice(&Sha256::digest(cdj));
    data
}

fn check_attestation(fmt: &str, stmt: &[(Cbor, Cbor)], auth_data: &[u8], cdj: &[u8], key: &CoseKey) -> Result<(), String> {
    match fmt {
        "none" => {
            let known = ["alg", "sig", "ver", "x5c", "response", "certInfo", "pubArea"];
            if stmt.iter().any(|(k, v)| !v.is_null() && k.as_text().is_some_and(|k| known.contains(&k))) {
                return Err("None attestation had unexpected attestation statement".into());
            }
            Ok(())
        }
        "packed" => {
            let sig = match cbor_get(stmt, &Cbor::Text("sig".into())) {
                Some(Cbor::Bytes(b)) if !b.is_empty() => b,
                _ => return Err("Attestation statement was missing signature (Packed)".into()),
            };
            let alg = cbor_get(stmt, &Cbor::Text("alg".into()))
                .and_then(Cbor::as_integer)
                .map(i128::from)
                .filter(|a| *a != 0)
                .ok_or("Attestation statement was missing algorithm (Packed)")?;
            // CAUTION: a packed statement with an x5c chain is accepted unverified. The approver asks for
            // attestation "none" and trusts no attestation, so the chain would prove nothing it relies on.
            if cbor_get(stmt, &Cbor::Text("x5c".into())).is_some() {
                return Ok(());
            }
            if alg != i128::from(key.alg()) {
                return Err(format!("Credential public key alg {} did not equal attestation statement alg {alg}", key.alg()));
            }
            if !key.verify(&signed_data(auth_data, cdj), sig) {
                return Err("Could not verify attestation statement signature (Packed|Self)".into());
            }
            Ok(())
        }
        // CAUTION: every other format is accepted without verifying its statement, for the same reason.
        _ => Ok(()),
    }
}

fn registration(credential: &Value, challenge: &[u8], origin: &str, rp_id: &str) -> Result<(String, String, u32), String> {
    let (response, cdj) = parse_credential(credential)?;
    let att_obj = json_b64u(response, "attestationObject", "Credential response missing required attestationObject")?;
    check_client_data(&cdj, "webauthn.create", challenge, origin)?;
    let att: Cbor = ciborium::from_reader(att_obj.as_slice()).map_err(|_| "Could not parse attestationObject as CBOR data")?;
    let Cbor::Map(att) = att else { return Err("attestationObject was not a dict".into()) };
    let fmt = cbor_get(&att, &Cbor::Text("fmt".into())).ok_or("attestationObject missing required property \"fmt\"")?;
    let auth_raw = match cbor_get(&att, &Cbor::Text("authData".into())) {
        Some(Cbor::Bytes(b)) => b,
        Some(_) => return Err("attestationObject authData is not bytes".into()),
        None => return Err("attestationObject missing required property \"authData\"".into()),
    };
    let stmt: &[(Cbor, Cbor)] = match cbor_get(&att, &Cbor::Text("attStmt".into())) {
        Some(Cbor::Map(m)) => m,
        None => &[],
        Some(_) => return Err("attestationObject attStmt is not a map".into()),
    };
    let auth = parse_auth_data(auth_raw)?;
    check_flags(&auth, rp_id, "attestation")?;
    let attested = auth.attested.as_ref().ok_or("Authenticator did not provide attested credential data")?;
    if attested.cred_id.is_empty() {
        return Err("Authenticator did not provide a credential ID".into());
    }
    let key = decode_cose(&attested.cose)?;
    let fmt = fmt.as_text().ok_or("malformed attestation type")?;
    check_attestation(fmt, stmt, auth_raw, &cdj, &key)?;
    Ok((b64u(&attested.cred_id), b64u(&attested.cose), auth.sign_count))
}

/// `(credential id, COSE public key, sign count)` from a registration response, ids and key in base64url.
pub fn verify_registration(credential: &Value, challenge: &[u8], origin: &str, rp_id: &str) -> Result<(String, String, u32), PasskeyError> {
    registration(credential, challenge, origin, rp_id).map_err(PasskeyError::Registration)
}

fn assertion(credential: &Value, challenge: &[u8], origin: &str, rp_id: &str, cose_pub_b64u: &str, sign_count: u32) -> Result<u32, String> {
    let (response, cdj) = parse_credential(credential)?;
    let auth_raw = json_b64u(response, "authenticatorData", "Credential response missing required authenticatorData")?;
    let sig = json_b64u(response, "signature", "Credential response missing required signature")?;
    match response.get("userHandle") {
        None | Some(Value::Null) => {}
        Some(Value::String(h)) if unb64u(h).is_some() => {}
        Some(_) => return Err("Credential response had unexpected userHandle".into()),
    }
    check_client_data(&cdj, "webauthn.get", challenge, origin)?;
    let auth = parse_auth_data(&auth_raw)?;
    check_flags(&auth, rp_id, "authentication")?;
    if (auth.sign_count > 0 || sign_count > 0) && auth.sign_count <= sign_count {
        return Err(format!("Response sign count of {} was not greater than current count of {sign_count}", auth.sign_count));
    }
    let key = unb64u(cose_pub_b64u).ok_or("the stored public key is not base64url")?;
    let key = decode_cose(&key)?;
    if !key.verify(&signed_data(&auth_raw, &cdj), &sig) {
        return Err("Could not verify authentication signature".into());
    }
    Ok(auth.sign_count)
}

/// The new sign count, once an assertion verifies against the stored key and count.
pub fn verify_assertion(credential: &Value, challenge: &[u8], origin: &str, rp_id: &str, cose_pub_b64u: &str, sign_count: u32) -> Result<u32, PasskeyError> {
    assertion(credential, challenge, origin, rp_id, cose_pub_b64u, sign_count).map_err(PasskeyError::Assertion)
}

/// A software passkey for tests: the browser's credential JSON from a soft authenticator.
pub mod soft {
    use super::*;

    /// The soft authenticator's credential key.
    pub enum SoftKey {
        /// ES256.
        P256(p256::ecdsa::SigningKey),
        /// EdDSA.
        Ed25519(ed25519_dalek::SigningKey),
    }

    /// A platform authenticator in software. Fields are public so tests can bend one rule at a time.
    pub struct SoftAuthenticator {
        /// The origin written into clientDataJSON.
        pub origin: String,
        /// The rp id hashed into authenticatorData.
        pub rp_id: String,
        /// The credential key.
        pub key: SoftKey,
        /// The COSE alg the public key claims.
        pub alg: i64,
        /// The credential id.
        pub cred_id: Vec<u8>,
        /// The signature counter: an assertion increments it first.
        pub sign_count: u32,
        /// The UP/UV flag bits set on every response.
        pub flags: u8,
        /// Attest as `packed` self-attestation instead of `none`.
        pub packed: bool,
        /// The user handle from the creation options.
        pub user_handle: Option<Vec<u8>>,
    }

    impl SoftAuthenticator {
        /// A P-256 authenticator.
        pub fn new(origin: &str, rp_id: &str) -> SoftAuthenticator {
            Self::with_key(origin, rp_id, SoftKey::P256(p256::ecdsa::SigningKey::random(&mut rand::rngs::OsRng)))
        }

        /// An Ed25519 authenticator.
        pub fn new_ed25519(origin: &str, rp_id: &str) -> SoftAuthenticator {
            Self::with_key(origin, rp_id, {
                let mut seed = [0u8; 32];
                rand::rngs::OsRng.fill_bytes(&mut seed);
                SoftKey::Ed25519(ed25519_dalek::SigningKey::from_bytes(&seed))
            })
        }

        fn with_key(origin: &str, rp_id: &str, key: SoftKey) -> SoftAuthenticator {
            let alg = match key {
                SoftKey::P256(_) => ALG_ES256,
                SoftKey::Ed25519(_) => ALG_EDDSA,
            };
            let mut cred_id = vec![0u8; 16];
            rand::rngs::OsRng.fill_bytes(&mut cred_id);
            SoftAuthenticator { origin: origin.into(), rp_id: rp_id.into(), key, alg, cred_id, sign_count: 0, flags: FLAG_UP | FLAG_UV, packed: false, user_handle: None }
        }

        /// The credential's COSE public key.
        pub fn cose(&self) -> Vec<u8> {
            let int = |i: i64| Cbor::Integer(i.into());
            let map = match &self.key {
                SoftKey::P256(k) => {
                    let point = k.verifying_key().to_encoded_point(false);
                    let coord = |c: Option<&p256::FieldBytes>| Cbor::Bytes(c.map(|b| b.to_vec()).unwrap_or_default());
                    vec![(int(1), int(2)), (int(3), int(self.alg)), (int(-1), int(1)), (int(-2), coord(point.x())), (int(-3), coord(point.y()))]
                }
                SoftKey::Ed25519(k) => vec![
                    (int(1), int(1)),
                    (int(3), int(self.alg)),
                    (int(-1), int(6)),
                    (int(-2), Cbor::Bytes(k.verifying_key().to_bytes().to_vec())),
                ],
            };
            cbor(&Cbor::Map(map))
        }

        /// The signature over `data` as WebAuthn carries it: DER for ES256, raw for EdDSA.
        pub fn sign(&self, data: &[u8]) -> Vec<u8> {
            match &self.key {
                SoftKey::P256(k) => {
                    let sig: p256::ecdsa::Signature = p256::ecdsa::signature::Signer::sign(k, data);
                    sig.to_der().as_bytes().to_vec()
                }
                SoftKey::Ed25519(k) => ed25519_dalek::Signer::sign(k, data).to_bytes().to_vec(),
            }
        }

        fn client_data(&self, kind: &str, options: &Value) -> Vec<u8> {
            let challenge = options.get("challenge").and_then(Value::as_str).unwrap_or_default();
            serde_json::to_vec(&json!({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": false})).unwrap_or_default()
        }

        fn auth_data(&self, flags: u8) -> Vec<u8> {
            let mut data = Sha256::digest(self.rp_id.as_bytes()).to_vec();
            data.push(flags);
            data.extend_from_slice(&self.sign_count.to_be_bytes());
            data
        }

        /// The credential JSON `navigator.credentials.create` gives approver.js for these creation options.
        pub fn register(&mut self, options: &Value) -> Value {
            self.user_handle = options.pointer("/user/id").and_then(Value::as_str).and_then(unb64u);
            let cdj = self.client_data("webauthn.create", options);
            let mut auth = self.auth_data(self.flags | FLAG_AT);
            auth.extend_from_slice(&[0u8; 16]);
            auth.extend_from_slice(&u16::try_from(self.cred_id.len()).unwrap_or(u16::MAX).to_be_bytes());
            auth.extend_from_slice(&self.cred_id);
            auth.extend_from_slice(&self.cose());
            let text = |s: &str| Cbor::Text(s.into());
            let (fmt, stmt) = if self.packed {
                let sig = self.sign(&signed_data(&auth, &cdj));
                ("packed", vec![(text("alg"), Cbor::Integer(self.alg.into())), (text("sig"), Cbor::Bytes(sig))])
            } else {
                ("none", vec![])
            };
            let att = cbor(&Cbor::Map(vec![(text("fmt"), text(fmt)), (text("attStmt"), Cbor::Map(stmt)), (text("authData"), Cbor::Bytes(auth))]));
            json!({
                "id": b64u(&self.cred_id), "rawId": b64u(&self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(&cdj), "attestationObject": b64u(&att), "transports": ["internal"]},
                "clientExtensionResults": {},
            })
        }

        /// The credential JSON `navigator.credentials.get` gives approver.js for these request options.
        pub fn assert(&mut self, options: &Value) -> Value {
            self.sign_count = self.sign_count.wrapping_add(1);
            let cdj = self.client_data("webauthn.get", options);
            let auth = self.auth_data(self.flags);
            let sig = self.sign(&signed_data(&auth, &cdj));
            let mut response = json!({"clientDataJSON": b64u(&cdj), "authenticatorData": b64u(&auth), "signature": b64u(&sig)});
            if let Some(h) = &self.user_handle {
                response["userHandle"] = json!(b64u(h));
            }
            json!({
                "id": b64u(&self.cred_id), "rawId": b64u(&self.cred_id), "type": "public-key",
                "response": response, "clientExtensionResults": {},
            })
        }
    }

    fn cbor(value: &Cbor) -> Vec<u8> {
        let mut out = Vec::new();
        ciborium::into_writer(value, &mut out).unwrap_or_default();
        out
    }

    /// UP, the user-present flag bit.
    pub const UP: u8 = FLAG_UP;
    /// UV, the user-verified flag bit.
    pub const UV: u8 = FLAG_UV;
}
