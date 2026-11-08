//! alcor's keys: the file formats Python writes, the grant envelope, the ack signature and the ssh certificate.
//!
//! `KEYGEN_CERTS` are synthetic: Python's `Keys.ssh_cert` (so `ssh-keygen -s`, OpenSSH 9.6) issued them
//! with a CA whose seed is `sha256(CA_SEED_TEXT)`, for the subject keys beside them. Rust must
//! reproduce each byte for byte given the same nonce.

const CA_SEED_TEXT: &[u8] = b"awm synthetic ssh user ca";
const CA_PUB: &str = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEEwPBJlC9FR7+QZJMBgRJZYW1sRAE8mdBCnEGCPPJOR awm-admin-ca";
/// `(label, subject public key line, ssh-keygen certificate line)`, issued for principals tony,root.
const KEYGEN_CERTS: [(&str, &str, &str); 3] = [
    ("ed25519", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINMyNxhs6WHmVqBMSD+L2dT3uy9XtrH5Km25rkIhv+/R tony@laptop", "ssh-ed25519-cert-v01@openssh.com AAAAIHNzaC1lZDI1NTE5LWNlcnQtdjAxQG9wZW5zc2guY29tAAAAII338n4HNjjsjXT4eoyPRDzBYmMcW4esuc/KxU3TqyOmAAAAINMyNxhs6WHmVqBMSD+L2dT3uy9XtrH5Km25rkIhv+/RElWUbHCrlhoAAAABAAAADWdyYW50LWVkMjU1MTkAAAAQAAAABHRvbnkAAAAEcm9vdAAAAABrNuyAAAAAAGs2+pAAAAAAAAAAggAAABVwZXJtaXQtWDExLWZvcndhcmRpbmcAAAAAAAAAF3Blcm1pdC1hZ2VudC1mb3J3YXJkaW5nAAAAAAAAABZwZXJtaXQtcG9ydC1mb3J3YXJkaW5nAAAAAAAAAApwZXJtaXQtcHR5AAAAAAAAAA5wZXJtaXQtdXNlci1yYwAAAAAAAAAAAAAAMwAAAAtzc2gtZWQyNTUxOQAAACBBMDwSZQvRUe/kGSTAYESWWFtbEQBPJnQQpxBgjzyTkQAAAFMAAAALc3NoLWVkMjU1MTkAAABA1UJaI80Rjpn0bbpEKt18m6RcPMfZ9bO71OEmibJnNa8HtsoLF4tEgR+0RpWeGNMM0yuNmeVrmEwNVgX1dqLlBQ== tony@laptop"),
    ("ecdsa", "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBOT2mqf+My9A8OhOgSO/IyadhuJ+Z2/WUJ/tFGJByVDBvHwLn/nkpPT00DsVGiByO3FpqKFfO5fncPNseoNWd4Y= phone key", "ecdsa-sha2-nistp256-cert-v01@openssh.com AAAAKGVjZHNhLXNoYTItbmlzdHAyNTYtY2VydC12MDFAb3BlbnNzaC5jb20AAAAg8qezDAiZf48GmAcPIa7RjufOoUd9cFqhVoGvDUnXlZwAAAAIbmlzdHAyNTYAAABBBOT2mqf+My9A8OhOgSO/IyadhuJ+Z2/WUJ/tFGJByVDBvHwLn/nkpPT00DsVGiByO3FpqKFfO5fncPNseoNWd4Zb+08OjqER+QAAAAEAAAALZ3JhbnQtZWNkc2EAAAAQAAAABHRvbnkAAAAEcm9vdAAAAABrNuyAAAAAAGs2+pAAAAAAAAAAggAAABVwZXJtaXQtWDExLWZvcndhcmRpbmcAAAAAAAAAF3Blcm1pdC1hZ2VudC1mb3J3YXJkaW5nAAAAAAAAABZwZXJtaXQtcG9ydC1mb3J3YXJkaW5nAAAAAAAAAApwZXJtaXQtcHR5AAAAAAAAAA5wZXJtaXQtdXNlci1yYwAAAAAAAAAAAAAAMwAAAAtzc2gtZWQyNTUxOQAAACBBMDwSZQvRUe/kGSTAYESWWFtbEQBPJnQQpxBgjzyTkQAAAFMAAAALc3NoLWVkMjU1MTkAAABA5eN/S5BV6m7lV86e8sV7L7Ezr5q9osWoDh0xgaXOWp/tOxvTaseHhjVauL22gxigCPJlRdYgE0iCUya/mBNyCw== phone key"),
    ("rsa", "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCTsrF8WGndj6Jfg8u9p+IRitOjTeLMK4UGbIrFc53gAlYAwWI8SEBhix6wgbgkYOSurq1EWplfkMsXebT4AZ5UDdOWkEkGvmQ0pV98tZCinamHgaXdzpX/jsqzNX1u95VIdAsHZlW/W0N+IlKychcrU8CpnQTzcrCVhtAvM+fhp1gkMdgGCPWInrUyXzbpjTNfDlVcZkra/Do/s/rZF3H+kh2xbtHeDbprkeJj42ZXvxuww7VrStDp8Xim0qrSPDqRqRRmFh2v41OV+Frx+PB/uCvmulfcJOsO1MWHMkMz0TpHJZTRSwssBjGaN7zcu3r5y746Cc7ICXrTvKNBLmSp", "ssh-rsa-cert-v01@openssh.com AAAAHHNzaC1yc2EtY2VydC12MDFAb3BlbnNzaC5jb20AAAAgP1mWq/zOAFyf82Z1pgPzs+1hSzCdvrUrA+x4wGfSS3QAAAADAQABAAABAQCTsrF8WGndj6Jfg8u9p+IRitOjTeLMK4UGbIrFc53gAlYAwWI8SEBhix6wgbgkYOSurq1EWplfkMsXebT4AZ5UDdOWkEkGvmQ0pV98tZCinamHgaXdzpX/jsqzNX1u95VIdAsHZlW/W0N+IlKychcrU8CpnQTzcrCVhtAvM+fhp1gkMdgGCPWInrUyXzbpjTNfDlVcZkra/Do/s/rZF3H+kh2xbtHeDbprkeJj42ZXvxuww7VrStDp8Xim0qrSPDqRqRRmFh2v41OV+Frx+PB/uCvmulfcJOsO1MWHMkMz0TpHJZTRSwssBjGaN7zcu3r5y746Cc7ICXrTvKNBLmSpAzN3EYXZsSYAAAABAAAACWdyYW50LXJzYQAAABAAAAAEdG9ueQAAAARyb290AAAAAGs27IAAAAAAazb6kAAAAAAAAACCAAAAFXBlcm1pdC1YMTEtZm9yd2FyZGluZwAAAAAAAAAXcGVybWl0LWFnZW50LWZvcndhcmRpbmcAAAAAAAAAFnBlcm1pdC1wb3J0LWZvcndhcmRpbmcAAAAAAAAACnBlcm1pdC1wdHkAAAAAAAAADnBlcm1pdC11c2VyLXJjAAAAAAAAAAAAAAAzAAAAC3NzaC1lZDI1NTE5AAAAIEEwPBJlC9FR7+QZJMBgRJZYW1sRAE8mdBCnEGCPPJORAAAAUwAAAAtzc2gtZWQyNTUxOQAAAED9QIoXzZ+5FhMxIaysppVA1kscc6EP+rTf5nEaRm/aaOEMUeIQ7dqn/gZi3drryhECWCOy63rau76Uz/vVT4AF /tmp/tmp063miuxp/key.pub"),
];

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use awm_plugin_operations::keys::{self, check_state, ssh_serial, Keys};
use awm_trust::grant::{canonical, load_public_key, unb64u, verify_grant, Expect};
use p256::pkcs8::DecodePublicKey;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use ssh_key::certificate::CertType;
use ssh_key::{Certificate, HashAlg, PublicKey};

const FILES: [&str; 4] = [keys::GRANT_FILE, keys::ACK_FILE, keys::SSH_CA_FILE, keys::SSH_CA_PUB_FILE];
const NOT_BEFORE: i64 = 1_798_761_600; // 2027-01-01T00:00:00Z
const NOT_AFTER: i64 = 1_798_765_200;
const EXTENSIONS: [&str; 5] = ["permit-X11-forwarding", "permit-agent-forwarding", "permit-port-forwarding", "permit-pty", "permit-user-rc"];

fn mode(path: &Path) -> u32 {
    std::fs::metadata(path).unwrap().permissions().mode() & 0o777
}

fn fresh() -> (tempfile::TempDir, PathBuf, Keys) {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("keys");
    let k = Keys::generate(&dir).unwrap();
    (tmp, dir, k)
}

/// Swap the ssh CA in `dir` for the synthetic one `KEYGEN_CERTS` were issued with.
fn use_synthetic_ca(dir: &Path) {
    let seed: [u8; 32] = Sha256::digest(CA_SEED_TEXT).into();
    let mut ca = ssh_key::PrivateKey::from(ssh_key::private::Ed25519Keypair::from_seed(&seed));
    ca.set_comment("awm-admin-ca");
    assert_eq!(ca.public_key().to_openssh().unwrap(), CA_PUB);
    for f in [keys::SSH_CA_FILE, keys::SSH_CA_PUB_FILE] {
        let _ = std::fs::remove_file(dir.join(f));
    }
    std::fs::write(dir.join(keys::SSH_CA_FILE), ca.to_openssh(ssh_key::LineEnding::LF).unwrap().as_bytes()).unwrap();
    std::fs::set_permissions(dir.join(keys::SSH_CA_FILE), std::fs::Permissions::from_mode(0o600)).unwrap();
    std::fs::write(dir.join(keys::SSH_CA_PUB_FILE), format!("{CA_PUB}\n")).unwrap();
}

#[test]
fn generate_then_load_round_trips() {
    let (_tmp, dir, k) = fresh();
    assert_eq!(mode(&dir), 0o700);
    for f in [keys::GRANT_FILE, keys::ACK_FILE, keys::SSH_CA_FILE] {
        assert_eq!(mode(&dir.join(f)), 0o600, "{f}");
    }
    let grant_pem = std::fs::read_to_string(dir.join(keys::GRANT_FILE)).unwrap();
    assert!(grant_pem.starts_with("-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2Vw"), "PKCS#8 v1, as cryptography writes it: {grant_pem}");
    assert!(std::fs::read_to_string(dir.join(keys::ACK_FILE)).unwrap().starts_with("-----BEGIN PRIVATE KEY-----\n"));
    assert!(std::fs::read_to_string(dir.join(keys::SSH_CA_FILE)).unwrap().starts_with("-----BEGIN OPENSSH PRIVATE KEY-----\n"));
    assert!(k.ssh_ca_pub().starts_with("ssh-ed25519 ") && k.ssh_ca_pub().ends_with(" awm-admin-ca"));

    let again = Keys::load(&dir).unwrap();
    assert_eq!(again.grant_pub_b64u(), k.grant_pub_b64u());
    assert_eq!(again.ack_pub_spki_b64u(), k.ack_pub_spki_b64u());
    assert_eq!(again.ssh_ca_pub(), k.ssh_ca_pub());
    assert_eq!(unb64u(&k.grant_pub_b64u()).unwrap().len(), 32);
    assert!(check_state(&dir).is_empty(), "{:?}", check_state(&dir));
    assert!(Keys::generate(&dir).is_err(), "generate never overwrites a key");
}

#[test]
fn load_refuses_each_missing_or_broken_file() {
    let (tmp, dir, _k) = fresh();
    for (i, name) in FILES.iter().enumerate() {
        let partial = tmp.path().join(format!("missing{i}"));
        std::fs::create_dir(&partial).unwrap();
        for f in FILES.iter().filter(|f| *f != name) {
            std::fs::copy(dir.join(f), partial.join(f)).unwrap();
        }
        let err = format!("{:#}", Keys::load(&partial).err().expect("load refuses"));
        assert!(err.starts_with(&format!("{name}:")), "{name}: {err}");
        assert_eq!(check_state(&partial).len(), 1, "{:?}", check_state(&partial));
        assert!(!partial.join(name).exists(), "load never creates {name}");

        let broken = tmp.path().join(format!("broken{i}"));
        std::fs::create_dir(&broken).unwrap();
        for f in FILES {
            std::fs::copy(dir.join(f), broken.join(f)).unwrap();
        }
        std::fs::write(broken.join(name), "garbage\n").unwrap();
        let err = format!("{:#}", Keys::load(&broken).err().expect("load refuses"));
        assert!(err.starts_with(&format!("{name}:")), "{name}: {err}");
    }

    let (_tmp2, other, _) = fresh();
    std::fs::copy(other.join(keys::SSH_CA_PUB_FILE), dir.join(keys::SSH_CA_PUB_FILE)).unwrap();
    let err = format!("{:#}", Keys::load(&dir).err().expect("a CA pub from another CA"));
    assert!(err.contains("is not the public half"), "{err}");
    assert_eq!(check_state(&dir), vec![err]);
}

#[test]
fn envelope_verifies_with_the_grant_key() {
    let (_tmp, _dir, k) = fresh();
    let body = json!({"grant_id": "g1", "kind": "trust_raise", "node": "home", "not_before": 100, "not_after": 200});
    let env = k.envelope("grant", body.clone());
    let pk = load_public_key(&k.grant_pub_b64u()).unwrap();
    assert_eq!(pk, k.grant_signing_key().verifying_key().to_bytes());
    assert_eq!(verify_grant(&env, &pk, &Expect { kind: Some("trust_raise"), ..Default::default() }, 150).unwrap(), body);
    let mut forged = env.clone();
    forged["grant"]["node"] = json!("other");
    assert!(verify_grant(&forged, &pk, &Expect::default(), 150).is_err());
}

/// The ack body without its `sig`, once the signature verifies against the ack key.
fn ack_body(k: &Keys, ack: &Value) -> Option<Value> {
    use p256::ecdsa::signature::Verifier;
    let mut body = ack.clone();
    let sig = unb64u(body.as_object_mut()?.remove("sig")?.as_str()?)?;
    assert_eq!(sig.len(), 64, "P1363 r||s");
    let key = p256::ecdsa::VerifyingKey::from_public_key_der(&unb64u(&k.ack_pub_spki_b64u())?).ok()?;
    key.verify(&canonical(&body), &p256::ecdsa::Signature::from_slice(&sig).ok()?).ok()?;
    Some(body)
}

#[test]
fn ack_verifies_with_webcrypto_style_p256() {
    let (_tmp, _dir, k) = fresh();
    let body = json!({"device": "d1", "seq": 3, "head": "abc"});
    let ack = k.sign_ack(body.clone());
    assert_eq!(ack_body(&k, &ack), Some(body));
    let mut forged = ack.clone();
    forged["device"] = json!("d2");
    assert_eq!(ack_body(&k, &forged), None, "the signature covers every field");
}

fn check_cert(line: &str, subject_line: &str, key_id: &str, ca_pub: &str) -> Certificate {
    let cert = Certificate::from_openssh(line).unwrap();
    let subject = PublicKey::from_openssh(subject_line).unwrap();
    assert_eq!(cert.public_key(), subject.key_data());
    assert_eq!(cert.cert_type(), CertType::User);
    assert_eq!(cert.key_id(), key_id);
    assert_eq!(cert.serial(), ssh_serial(key_id));
    assert_eq!((cert.valid_after(), cert.valid_before()), (NOT_BEFORE as u64, NOT_AFTER as u64));
    assert_eq!(cert.valid_principals(), ["tony", "root"]);
    assert!(cert.critical_options().is_empty());
    assert_eq!(cert.extensions().keys().map(String::as_str).collect::<Vec<_>>(), EXTENSIONS);
    assert!(cert.extensions().values().all(String::is_empty));
    assert_eq!(cert.nonce().len(), 32);
    let ca_fp = PublicKey::from_openssh(ca_pub).unwrap().fingerprint(HashAlg::Sha256);
    cert.validate_at(NOT_BEFORE as u64, &[ca_fp]).unwrap();
    cert
}

#[test]
fn ssh_cert_is_byte_for_byte_what_ssh_keygen_issued() {
    let (_tmp, dir, _) = fresh();
    use_synthetic_ca(&dir);
    let k = Keys::load(&dir).unwrap();
    assert_eq!(k.ssh_ca_pub(), CA_PUB);
    let principals = ["tony".to_string(), "root".to_string()];
    for (label, subject, theirs) in KEYGEN_CERTS {
        let key_id = format!("grant-{label}");
        let keygen = check_cert(theirs, subject, &key_id, CA_PUB);
        let nonce: [u8; 32] = keygen.nonce().try_into().unwrap();
        let ours = k.ssh_cert_with_nonce(subject, &principals, NOT_BEFORE, NOT_AFTER, &key_id, nonce).unwrap();
        let fields = |l: &str| l.splitn(3, ' ').map(str::to_string).collect::<Vec<_>>();
        let (ours, theirs) = (fields(&ours), fields(theirs));
        assert_eq!(ours[..2], theirs[..2], "{label}: type and certificate bytes");
        match subject.splitn(3, ' ').nth(2) {
            Some(comment) => assert_eq!(ours[2], comment, "{label}"),
            None => {
                assert_eq!(ours.len(), 2, "{label}: no comment");
                assert!(theirs[2].ends_with("/key.pub"), "{label}: ssh-keygen names the temp file instead");
            }
        }
        let random = k.ssh_cert(subject, &principals, NOT_BEFORE, NOT_AFTER, &key_id).unwrap();
        assert_ne!(check_cert(&random, subject, &key_id, CA_PUB).nonce(), &nonce[..]);
    }
}

#[test]
fn ssh_cert_refuses_what_ssh_keygen_refuses() {
    let (_tmp, _dir, k) = fresh();
    let line = KEYGEN_CERTS[0].1;
    let p = |s: &[&str]| s.iter().map(|x| x.to_string()).collect::<Vec<_>>();
    let err = |r: anyhow::Result<String>| r.err().map(|e| e.to_string()).unwrap_or_default();
    assert_eq!(err(k.ssh_cert(line, &[], NOT_BEFORE, NOT_AFTER, "x")), "Empty principal name");
    assert_eq!(err(k.ssh_cert(line, &p(&["a,,b"]), NOT_BEFORE, NOT_AFTER, "x")), "Empty principal name");
    assert_eq!(err(k.ssh_cert(line, &p(&["a"]), NOT_AFTER, NOT_BEFORE, "x")), "Empty certificate validity interval");
    assert!(k.ssh_cert("ssh-ed25519", &p(&["a"]), NOT_BEFORE, NOT_AFTER, "x").is_err());
    assert!(k.ssh_cert("ssh-ed25519 not-base64!", &p(&["a"]), NOT_BEFORE, NOT_AFTER, "x").is_err());
    assert!(k.ssh_cert(line, &p(&["a"]), -1, NOT_AFTER, "x").is_err());
    let split = Certificate::from_openssh(&k.ssh_cert(&format!("  {line}\t\n"), &p(&["a,b"]), NOT_BEFORE, NOT_AFTER, "x").unwrap()).unwrap();
    assert_eq!(split.valid_principals(), ["a", "b"]);
    assert_eq!(split.comment(), "tony@laptop");
}
