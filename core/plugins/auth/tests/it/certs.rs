//! The edge's CA, leaf and SAN set: the port of tlsfront's `test_certs_trust_consumer`,
//! httpsfront's `test_cert_dir`, the SAN vectors, and the cert copy (ports/auth.md).

use std::collections::BTreeSet;
use std::ffi::OsString;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use awm_plugin_auth::certs::{self, CertError};
use rcgen::{CertificateParams, KeyPair, SanType, PKCS_ECDSA_P256_SHA256};
use serde_json::Value;

const FAKE_ROOT: &[u8] = b"-----BEGIN CERTIFICATE-----\nnot-a-real-root\n-----END CERTIFICATE-----\n";
const SANS_VECTORS: &str = include_str!("../vectors/sans.json");
const RSA_VECTORS: &str = include_str!("../vectors/rsa_leaf.json");

fn s(v: &[&str]) -> Vec<String> {
    v.iter().map(|x| x.to_string()).collect()
}

struct Dirs {
    tmp: tempfile::TempDir,
    ca: PathBuf,
    cert: PathBuf,
}

fn dirs() -> Dirs {
    let tmp = tempfile::tempdir().unwrap();
    let ca = tmp.path().join("ca");
    let cert = tmp.path().join(".certs");
    std::fs::create_dir(&ca).unwrap();
    Dirs { tmp, ca, cert }
}

fn ensure(cert: &Path, ca: &Path, sans: &[&str]) -> Result<certs::CertInfo, CertError> {
    certs::ensure_certs(cert, Some(ca), Some(&s(sans)))
}

fn trust_consumer_msg(r: Result<certs::CertInfo, CertError>) -> String {
    match r {
        Err(CertError::TrustConsumer(m)) => m,
        other => panic!("expected a trust-consumer refusal, got {other:?}"),
    }
}

#[derive(Clone, Default)]
struct Logs(Arc<Mutex<Vec<u8>>>);

impl std::io::Write for Logs {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Logs {
    type Writer = Logs;
    fn make_writer(&'a self) -> Logs {
        self.clone()
    }
}

fn capture<T>(f: impl FnOnce() -> T) -> (T, String) {
    let logs = Logs::default();
    let sub = tracing_subscriber::fmt().with_writer(logs.clone()).with_ansi(false).with_max_level(tracing::Level::INFO).finish();
    let out = tracing::subscriber::with_default(sub, f);
    let text = String::from_utf8(logs.0.lock().unwrap().clone()).unwrap();
    (out, text)
}

/// Lay out a trust-consumer node: `ca.pem` only, plus a leaf pair.
fn provision(tmp: &Path, root: &[u8], cert_from: &Path, key_from: &Path) -> (PathBuf, PathBuf) {
    let ca = tmp.join("consumer-ca");
    let cd = tmp.join("consumer-certs");
    std::fs::create_dir_all(&ca).unwrap();
    std::fs::create_dir_all(&cd).unwrap();
    std::fs::write(ca.join("ca.pem"), root).unwrap();
    std::fs::copy(cert_from.join("cert.pem"), cd.join("cert.pem")).unwrap();
    std::fs::copy(key_from.join("key.pem"), cd.join("key.pem")).unwrap();
    (ca, cd)
}

/// A leaf signed by the CA in `ca_dir`, valid from now for `seconds`.
fn leaf_valid_for(ca_dir: &Path, out_dir: &Path, sans: &[&str], seconds: i64) {
    let ca_pem = std::fs::read_to_string(ca_dir.join("ca.pem")).unwrap();
    let ca_kp = certs::load_key_pair(&ca_dir.join("ca-key.pem")).unwrap();
    let issuer = CertificateParams::from_ca_cert_pem(&ca_pem).unwrap().self_signed(&ca_kp).unwrap();
    let kp = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap();
    let mut p = CertificateParams::default();
    p.subject_alt_names = sans
        .iter()
        .map(|t| match t.strip_prefix("IP:") {
            Some(ip) => SanType::IpAddress(ip.parse().unwrap()),
            None => SanType::DnsName(t.strip_prefix("DNS:").unwrap().try_into().unwrap()),
        })
        .collect();
    let now = time::OffsetDateTime::now_utc();
    p.not_before = now - time::Duration::days(400);
    p.not_after = now + time::Duration::seconds(seconds);
    let cert = p.signed_by(&kp, &issuer, &ca_kp).unwrap();
    std::fs::create_dir_all(out_dir).unwrap();
    std::fs::write(out_dir.join("cert.pem"), cert.pem()).unwrap();
    std::fs::write(out_dir.join("key.pem"), kp.serialize_pem()).unwrap();
}

fn mtimes(dir: &Path) -> BTreeSet<(String, std::time::SystemTime)> {
    std::fs::read_dir(dir)
        .unwrap()
        .map(|e| {
            let e = e.unwrap();
            (e.file_name().to_string_lossy().into_owned(), e.metadata().unwrap().modified().unwrap())
        })
        .collect()
}

fn leaf_is_p256(cert: &Path) -> bool {
    let pem = std::fs::read(cert).unwrap();
    let der = rustls_pemfile::certs(&mut pem.as_slice()).next().unwrap().unwrap();
    let (_, c) = x509_parser::parse_x509_certificate(&der).unwrap();
    let alg = &c.public_key().algorithm;
    alg.algorithm.to_id_string() == "1.2.840.10045.2.1"
        && alg.parameters.as_ref().unwrap().as_oid().unwrap().to_id_string() == "1.2.840.10045.3.1.7"
}

// ---------------------------------------------------------------------------
// The refusal: it never gets as far as minting.
// ---------------------------------------------------------------------------

#[test]
fn test_a_copied_root_without_a_leaf_refuses() {
    let d = dirs();
    std::fs::write(d.ca.join("ca.pem"), FAKE_ROOT).unwrap();
    let m = trust_consumer_msg(ensure(&d.cert, &d.ca, &["IP:10.74.81.213"]));
    assert!(m.contains("cannot sign"), "{m}");
    assert!(m.contains("IP:10.74.81.213"), "{m}");
}

#[test]
fn a_consumer_never_mints_a_root_or_a_leaf() {
    let d = dirs();
    std::fs::write(d.ca.join("ca.pem"), FAKE_ROOT).unwrap();
    for _ in 0..2 {
        trust_consumer_msg(ensure(&d.cert, &d.ca, &["IP:10.74.81.213"]));
    }
    let names: BTreeSet<_> = std::fs::read_dir(&d.cert).unwrap().map(|e| e.unwrap().file_name()).collect();
    assert_eq!(names, BTreeSet::from([OsString::from("ca.pem")]), "only the root mirror is written");
    assert!(!d.ca.join("ca-key.pem").exists());
}

// ---------------------------------------------------------------------------
// The happy paths: these mint.
// ---------------------------------------------------------------------------

#[test]
fn test_an_empty_ca_dir_still_mints_a_root() {
    let d = dirs();
    let info = ensure(&d.cert, &d.ca, &["IP:127.0.0.1"]).unwrap();
    assert!(d.ca.join("ca.pem").exists());
    assert!(d.ca.join("ca-key.pem").exists());
    assert!(d.cert.join("cert.pem").exists());
    assert_eq!(info.san, "IP:127.0.0.1");
    let mode = |p: PathBuf| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(d.ca.join("ca-key.pem")), 0o600);
    assert_eq!(mode(d.cert.join("key.pem")), 0o600);
    assert!(leaf_is_p256(&d.cert.join("cert.pem")) && leaf_is_p256(&d.ca.join("ca.pem")));
    let before = mtimes(&d.cert);
    ensure(&d.cert, &d.ca, &["IP:127.0.0.1"]).unwrap();
    assert_eq!(mtimes(&d.cert), before, "a second start touches nothing");
}

#[test]
fn a_san_set_in_another_order_does_not_remint() {
    let d = dirs();
    ensure(&d.cert, &d.ca, &["IP:127.0.0.1", "DNS:localhost", "IP:10.74.81.213"]).unwrap();
    let cert = std::fs::read(d.cert.join("cert.pem")).unwrap();
    let before = mtimes(&d.cert);
    ensure(&d.cert, &d.ca, &["IP:10.74.81.213", "IP:127.0.0.1", "DNS:LOCALHOST", "DNS:localhost"]).unwrap();
    assert_eq!(std::fs::read(d.cert.join("cert.pem")).unwrap(), cert);
    assert_eq!(mtimes(&d.cert), before);
}

#[test]
fn a_changed_san_set_remints_on_the_holder() {
    let d = dirs();
    ensure(&d.cert, &d.ca, &["IP:127.0.0.1"]).unwrap();
    let info = ensure(&d.cert, &d.ca, &["IP:127.0.0.1", "DNS:node.example.test"]).unwrap();
    assert_eq!(info.san, "IP:127.0.0.1,DNS:node.example.test");
    assert_eq!(certs::cert_sans(&d.cert.join("cert.pem")), BTreeSet::from(["IP:127.0.0.1".into(), "DNS:node.example.test".into()]));
    assert_eq!(std::fs::read_to_string(d.cert.join(".san")).unwrap(), "IP:127.0.0.1,DNS:node.example.test");
}

#[test]
fn a_holder_remints_a_leaf_near_expiry() {
    let d = dirs();
    ensure(&d.cert, &d.ca, &["IP:127.0.0.1"]).unwrap();
    leaf_valid_for(&d.ca, &d.cert, &["IP:127.0.0.1"], 5 * 86_400);
    assert_eq!(certs::cert_days_left(&d.cert.join("cert.pem")), Some(4));
    ensure(&d.cert, &d.ca, &["IP:127.0.0.1"]).unwrap();
    assert!(certs::cert_days_left(&d.cert.join("cert.pem")).unwrap() >= 390);
}

#[test]
fn test_a_pre_provisioned_leaf_is_accepted() {
    let d = dirs();
    ensure(&d.cert, &d.ca, &["IP:10.74.81.213"]).unwrap();
    let root = std::fs::read(d.ca.join("ca.pem")).unwrap();
    let (ca, cd) = provision(d.tmp.path(), &root, &d.cert, &d.cert);
    assert!(!cd.join(".san").exists());
    let info = ensure(&cd, &ca, &["IP:10.74.81.213"]).unwrap();
    assert_eq!(info.cert, cd.join("cert.pem"));
    assert_eq!(std::fs::read(ca.join("ca.pem")).unwrap(), root);
    assert_eq!(std::fs::read(cd.join("ca.pem")).unwrap(), root);
    assert!(!ca.join("ca-key.pem").exists());
}

#[test]
fn test_partial_san_coverage_warns_but_serves() {
    let d = dirs();
    ensure(&d.cert, &d.ca, &["IP:10.74.81.213"]).unwrap();
    let root = std::fs::read(d.ca.join("ca.pem")).unwrap();
    let (ca, cd) = provision(d.tmp.path(), &root, &d.cert, &d.cert);
    let (info, logs) = capture(|| ensure(&cd, &ca, &["IP:10.74.81.213", "IP:172.18.0.1"]));
    assert_eq!(info.unwrap().cert, cd.join("cert.pem"));
    assert!(logs.contains("172.18.0.1"), "{logs}");
    assert!(logs.contains("Re-mint"), "{logs}");
}

// ---------------------------------------------------------------------------
// cert_sans
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// The wrong pair, and the clock.
// ---------------------------------------------------------------------------

#[test]
fn test_an_expired_leaf_refuses_rather_than_serving() {
    let d = dirs();
    ensure(&d.cert, &d.ca, &["IP:10.74.81.213"]).unwrap();
    let root = std::fs::read(d.ca.join("ca.pem")).unwrap();
    let expired = d.tmp.path().join("expired");
    leaf_valid_for(&d.ca, &expired, &["IP:10.74.81.213"], -(2 * 86_400 + 3600));
    let (ca, cd) = provision(d.tmp.path(), &root, &expired, &expired);
    let m = trust_consumer_msg(ensure(&cd, &ca, &["IP:10.74.81.213"]));
    assert!(m.contains("expired 3 day(s) ago"), "{m}");
}

// ---------------------------------------------------------------------------
// The fleet's existing RSA material.
// ---------------------------------------------------------------------------

fn rsa() -> Value {
    serde_json::from_str(RSA_VECTORS).unwrap()
}

fn lay_out_rsa(root: &Path, ca_key: Option<&str>, leaf_key: &str) -> (PathBuf, PathBuf) {
    let v = rsa();
    let ca = root.join("rsa-ca");
    let cd = root.join("rsa-certs");
    std::fs::create_dir_all(&ca).unwrap();
    std::fs::create_dir_all(&cd).unwrap();
    std::fs::write(ca.join("ca.pem"), v["ca_pem"].as_str().unwrap()).unwrap();
    if let Some(k) = ca_key {
        std::fs::write(ca.join("ca-key.pem"), v[k].as_str().unwrap()).unwrap();
    }
    std::fs::write(cd.join("ca.pem"), v["ca_pem"].as_str().unwrap()).unwrap();
    std::fs::write(cd.join("cert.pem"), v["leaf_pem"].as_str().unwrap()).unwrap();
    std::fs::write(cd.join("key.pem"), v[leaf_key].as_str().unwrap()).unwrap();
    (ca, cd)
}

#[test]
fn a_holder_with_an_rsa_leaf_and_the_same_san_set_does_not_remint() {
    let tmp = tempfile::tempdir().unwrap();
    let (ca, cd) = lay_out_rsa(tmp.path(), Some("ca_key_pkcs8"), "leaf_key_pkcs8");
    let cert = std::fs::read(cd.join("cert.pem")).unwrap();
    let before = mtimes(&cd);
    // The Python sidecar is absent and the order differs: neither may trigger a re-cut.
    ensure(&cd, &ca, &["IP:10.74.81.213", "DNS:localhost", "IP:127.0.0.1"]).unwrap();
    assert_eq!(std::fs::read(cd.join("cert.pem")).unwrap(), cert);
    assert_eq!(mtimes(&cd), before);
}

#[test]
fn an_rsa_ca_key_signs_a_new_p256_leaf() {
    for ca_key in ["ca_key_pkcs8", "ca_key_pkcs1"] {
        let tmp = tempfile::tempdir().unwrap();
        let (ca, cd) = lay_out_rsa(tmp.path(), Some(ca_key), "leaf_key_pkcs8");
        let info = ensure(&cd, &ca, &["IP:127.0.0.1", "DNS:node.example.test"]).unwrap();
        assert!(leaf_is_p256(&info.cert), "{ca_key}");
        assert_eq!(certs::leaf_matches_ca(&info.cert, &info.key, &ca.join("ca.pem")), None, "{ca_key}");
        // The leaf's authority key id names the RSA root's subject key id.
        let read = |p: &Path| {
            let pem = std::fs::read(p).unwrap();
            let der = rustls_pemfile::certs(&mut pem.as_slice()).next().unwrap().unwrap().to_vec();
            der
        };
        let (leaf_der, ca_der) = (read(&info.cert), read(&ca.join("ca.pem")));
        let (_, leaf) = x509_parser::parse_x509_certificate(&leaf_der).unwrap();
        let (_, root) = x509_parser::parse_x509_certificate(&ca_der).unwrap();
        let aki = leaf
            .iter_extensions()
            .find_map(|e| match e.parsed_extension() {
                x509_parser::extensions::ParsedExtension::AuthorityKeyIdentifier(a) => a.key_identifier.as_ref().map(|k| k.0.to_vec()),
                _ => None,
            })
            .unwrap();
        let ski = root
            .iter_extensions()
            .find_map(|e| match e.parsed_extension() {
                x509_parser::extensions::ParsedExtension::SubjectKeyIdentifier(k) => Some(k.0.to_vec()),
                _ => None,
            })
            .unwrap();
        assert_eq!(aki, ski);
        assert_eq!(leaf.issuer().as_raw(), root.subject().as_raw());
    }
}

#[test]
fn a_consumer_accepts_the_rsa_leaf_in_either_key_format() {
    for leaf_key in ["leaf_key_pkcs8", "leaf_key_pkcs1"] {
        let tmp = tempfile::tempdir().unwrap();
        let (ca, cd) = lay_out_rsa(tmp.path(), None, leaf_key);
        let info = ensure(&cd, &ca, &["IP:127.0.0.1", "DNS:localhost", "IP:10.74.81.213"]).unwrap();
        assert_eq!(info.san, "DNS:localhost,IP:10.74.81.213,IP:127.0.0.1", "{leaf_key}");
        assert!(!ca.join("ca-key.pem").exists());
    }
}

// ---------------------------------------------------------------------------
// The SAN normaliser, against the Python's own output.
// ---------------------------------------------------------------------------

fn vectors() -> Value {
    serde_json::from_str(SANS_VECTORS).unwrap()
}

#[test]
fn the_normaliser_matches_the_python_vectors() {
    let v = vectors();
    let cases = v["normalize"].as_array().unwrap();
    assert!(cases.len() > 50);
    for c in cases {
        let input = c["input"].as_str().unwrap();
        assert_eq!(certs::normalize_san(input).as_deref(), c["output"].as_str(), "input {input:?}");
    }
}

#[test]
fn extra_sans_matches_the_python_vectors() {
    let v = vectors();
    let tmp = tempfile::tempdir().unwrap();
    for c in v["extra"].as_array().unwrap() {
        let env = c["env"].as_str().unwrap();
        let file = match c["file"].as_str() {
            Some(text) => {
                let p = tmp.path().join(".sans");
                std::fs::write(&p, text).unwrap();
                p
            }
            None => tmp.path().join("absent.sans"),
        };
        let want: Vec<String> = serde_json::from_value(c["output"].clone()).unwrap();
        assert_eq!(certs::extra_sans(Some(env), Some(&file)).unwrap(), want, "env {env:?} file {:?}", c["file"]);
    }
}

#[test]
fn resolve_sans_matches_the_python_vectors() {
    let v = vectors();
    let tmp = tempfile::tempdir().unwrap();
    for c in v["resolve"].as_array().unwrap() {
        let defaults: Vec<String> = serde_json::from_value(c["defaults"].clone()).unwrap();
        let env = c["env"].as_str().unwrap();
        let file = match c["file"].as_str() {
            Some(text) => {
                let p = tmp.path().join(".sans");
                std::fs::write(&p, text).unwrap();
                p
            }
            None => tmp.path().join("absent.sans"),
        };
        let want: Vec<String> = serde_json::from_value(c["output"].clone()).unwrap();
        assert_eq!(certs::resolve_sans_with(defaults, Some(env), Some(&file)).unwrap(), want, "{c}");
    }
}

#[test]
fn san_sets_ignore_order_case_and_address_spelling() {
    let a = s(&["IP:127.0.0.1", "DNS:Localhost", "IP:2001:db8::42"]);
    let b = s(&["IP:2001:DB8:0:0:0:0:0:42", "DNS:localhost", "IP:127.0.0.1", "IP:127.0.0.1"]);
    assert_eq!(certs::san_set(&a), certs::san_set(&b));
    assert_ne!(certs::san_set(&a), certs::san_set(&s(&["IP:127.0.0.1", "DNS:localhost"])));
}

// ---------------------------------------------------------------------------
// The WSL fallback.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Where the leaf lives, and the one-time copy.
// ---------------------------------------------------------------------------

fn legacy(root: &Path) -> PathBuf {
    let svc = root.join("awm/services/httpsfront");
    std::fs::create_dir_all(svc.join(".certs")).unwrap();
    std::fs::set_permissions(svc.join(".certs"), std::fs::Permissions::from_mode(0o750)).unwrap();
    for (name, body, mode) in [("cert.pem", "legacy-cert", 0o644), ("key.pem", "legacy-key", 0o600), ("ca.pem", "legacy-ca", 0o644), (".san", "IP:127.0.0.1", 0o644)] {
        let p = svc.join(".certs").join(name);
        std::fs::write(&p, body).unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(mode)).unwrap();
    }
    std::fs::write(svc.join(".sans"), "10.74.81.9\n").unwrap();
    std::fs::set_permissions(svc.join(".sans"), std::fs::Permissions::from_mode(0o640)).unwrap();
    svc
}

fn mode(p: &Path) -> u32 {
    std::fs::metadata(p).unwrap().permissions().mode() & 0o7777
}

#[test]
fn copy_legacy_once_copies_once_and_keeps_modes() {
    let tmp = tempfile::tempdir().unwrap();
    let svc = legacy(tmp.path());
    let state = tmp.path().join("state");
    let copied = certs::copy_legacy_once(&svc.join(".certs"), &svc.join(".sans"), &state).unwrap();
    assert_eq!(copied.len(), 5);
    assert_eq!(std::fs::read_to_string(state.join("certs/key.pem")).unwrap(), "legacy-key");
    assert_eq!(std::fs::read_to_string(state.join("sans")).unwrap(), "10.74.81.9\n");
    assert_eq!(mode(&state.join("certs")), 0o750);
    assert_eq!(mode(&state.join("certs/key.pem")), 0o600);
    assert_eq!(mode(&state.join("certs/cert.pem")), 0o644);
    assert_eq!(mode(&state.join("sans")), 0o640);
    assert!(certs::copy_legacy_once(&svc.join(".certs"), &svc.join(".sans"), &state).unwrap().is_empty());
}

#[test]
fn copy_legacy_once_never_overwrites_state() {
    let tmp = tempfile::tempdir().unwrap();
    let svc = legacy(tmp.path());
    let state = tmp.path().join("state");
    std::fs::create_dir_all(state.join("certs")).unwrap();
    std::fs::write(state.join("certs/cert.pem"), "minted-cert").unwrap();
    std::fs::write(state.join("sans"), "node.example.test\n").unwrap();
    let copied = certs::copy_legacy_once(&svc.join(".certs"), &svc.join(".sans"), &state).unwrap();
    assert_eq!(std::fs::read_to_string(state.join("certs/cert.pem")).unwrap(), "minted-cert");
    // Half a pair in state keeps the other legacy half out: a cert and key from different mints never meet.
    assert!(!state.join("certs/key.pem").exists());
    assert_eq!(std::fs::read_to_string(state.join("sans")).unwrap(), "node.example.test\n");
    assert_eq!(copied, vec![state.join("certs/.san"), state.join("certs/ca.pem")]);
}

#[test]
fn test_the_leaf_can_live_outside_the_checkout() {
    let tmp = tempfile::tempdir().unwrap();
    let svc = legacy(tmp.path());
    let state = tmp.path().join("state");
    let edge = tmp.path().join("var/lib/awm/state/services/httpsfront/certs");
    // Set: it is the cert dir, read and minted into in place, and nothing lands in <state>/certs.
    assert_eq!(certs::cert_dir_for(Some(edge.clone().into()), &state, &svc), edge);
    assert!(!state.join("certs").exists());
    assert_eq!(std::fs::read_to_string(state.join("sans")).unwrap(), "10.74.81.9\n");
    // Unset, or empty: <state>/certs, seeded once from the checkout.
    for unset in [None, Some(OsString::new())] {
        assert_eq!(certs::cert_dir_for(unset, &state, &svc), state.join("certs"));
    }
    assert_eq!(std::fs::read_to_string(state.join("certs/cert.pem")).unwrap(), "legacy-cert");
    assert_eq!(certs::sans_file(&state), state.join("sans"));
    assert_eq!(certs::legacy_service_dir(tmp.path()), svc);
}


#[test]
fn a_consumer_refuses_a_leaf_that_is_not_its_cas_or_has_expired() {
    let d = dirs();
    ensure(&d.cert, &d.ca, &["IP:10.74.81.213"]).unwrap();
    let root = std::fs::read(d.ca.join("ca.pem")).unwrap();

    let other_ca = d.tmp.path().join("other-ca");
    let other_certs = d.tmp.path().join("other-certs");
    std::fs::create_dir(&other_ca).unwrap();
    ensure(&other_certs, &other_ca, &["IP:10.74.81.213"]).unwrap();
    let (ca, cd) = provision(d.tmp.path(), &root, &other_certs, &other_certs);
    assert!(trust_consumer_msg(ensure(&cd, &ca, &["IP:10.74.81.213"])).contains("chain"));

    let second = d.tmp.path().join("second");
    ensure(&second, &d.ca, &["IP:10.74.81.99"]).unwrap();
    let (ca, cd) = provision(d.tmp.path(), &root, &d.cert, &second);
    assert!(trust_consumer_msg(ensure(&cd, &ca, &["IP:10.74.81.213"])).contains("private key"));

    let soon = d.tmp.path().join("soon");
    leaf_valid_for(&d.ca, &soon, &["IP:10.74.81.213"], 12 * 86_400 + 3600);
    let (ca, cd) = provision(d.tmp.path(), &root, &soon, &soon);
    let (r, logs) = capture(|| ensure(&cd, &ca, &["IP:10.74.81.213"]));
    r.unwrap();
    assert!(logs.contains("expires in 12 day(s)"), "{logs}");
}
