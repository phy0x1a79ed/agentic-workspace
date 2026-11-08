//! alcor's own keys: the Ed25519 grant key, the P-256 ack key and the ssh user CA.

use std::fs;
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;

use anyhow::{anyhow, bail, Context};
use awm_trust::grant::{b64u, canonical, sign_envelope};
use ed25519_dalek::pkcs8::{DecodePrivateKey as _, EncodePrivateKey as _, KeypairBytes};
use p256::pkcs8::{EncodePublicKey as _, LineEnding};
use rand::rngs::OsRng;
use rand::RngCore;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use ssh_key::certificate::{Builder, CertType};
use ssh_key::public::KeyData;
use ssh_key::{Algorithm, PrivateKey, PublicKey};

/// The Ed25519 grant key, PKCS#8 PEM.
pub const GRANT_FILE: &str = "grant_ed25519.pem";
/// The P-256 ack key, PKCS#8 PEM.
pub const ACK_FILE: &str = "ack_p256.pem";
/// The ssh user CA, an OpenSSH Ed25519 private key with no passphrase.
pub const SSH_CA_FILE: &str = "ssh_user_ca";
/// The ssh user CA's public key line.
pub const SSH_CA_PUB_FILE: &str = "ssh_user_ca.pub";

const SSH_CA_COMMENT: &str = "awm-admin-ca";
/// The extensions `ssh-keygen -s` gives a user certificate by default.
const DEFAULT_USER_EXTENSIONS: [&str; 5] =
    ["permit-X11-forwarding", "permit-agent-forwarding", "permit-port-forwarding", "permit-pty", "permit-user-rc"];

/// alcor's three keys, loaded from its key directory.
pub struct Keys {
    grant: ed25519_dalek::SigningKey,
    ack: p256::ecdsa::SigningKey,
    ssh_ca: PrivateKey,
    ssh_ca_pub: String,
}

fn read_text(dir: &Path, name: &str) -> anyhow::Result<String> {
    fs::read_to_string(dir.join(name)).with_context(|| format!("{name}: cannot read {}", dir.join(name).display()))
}

fn load_grant(dir: &Path) -> anyhow::Result<ed25519_dalek::SigningKey> {
    let pem = read_text(dir, GRANT_FILE)?;
    ed25519_dalek::SigningKey::from_pkcs8_pem(&pem).map_err(|e| anyhow!("{GRANT_FILE}: not a PKCS#8 Ed25519 key: {e}"))
}

fn load_ack(dir: &Path) -> anyhow::Result<p256::ecdsa::SigningKey> {
    let pem = read_text(dir, ACK_FILE)?;
    p256::ecdsa::SigningKey::from_pkcs8_pem(&pem).map_err(|e| anyhow!("{ACK_FILE}: not a PKCS#8 P-256 key: {e}"))
}

fn load_ssh_ca(dir: &Path) -> anyhow::Result<PrivateKey> {
    let pem = read_text(dir, SSH_CA_FILE)?;
    let key = PrivateKey::from_openssh(&pem).map_err(|e| anyhow!("{SSH_CA_FILE}: not an OpenSSH private key: {e}"))?;
    if key.is_encrypted() {
        bail!("{SSH_CA_FILE}: has a passphrase");
    }
    if key.algorithm() != Algorithm::Ed25519 {
        bail!("{SSH_CA_FILE}: is {}, not ssh-ed25519", key.algorithm());
    }
    Ok(key)
}

fn load_ssh_ca_pub(dir: &Path, ca: Option<&PrivateKey>) -> anyhow::Result<String> {
    let line = read_text(dir, SSH_CA_PUB_FILE)?.trim().to_string();
    let public = PublicKey::from_openssh(&line).map_err(|e| anyhow!("{SSH_CA_PUB_FILE}: not an OpenSSH public key: {e}"))?;
    if ca.is_some_and(|ca| ca.public_key().key_data() != public.key_data()) {
        bail!("{SSH_CA_PUB_FILE}: is not the public half of {SSH_CA_FILE}");
    }
    Ok(line)
}

fn write_new(path: &Path, data: &[u8], mode: u32) -> anyhow::Result<()> {
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .open(path)
        .with_context(|| format!("cannot create {}", path.display()))?;
    f.write_all(data).with_context(|| format!("cannot write {}", path.display()))
}

fn utc(t: i64) -> String {
    chrono::DateTime::from_timestamp(t, 0).map_or_else(|| t.to_string(), |d| d.format("%Y%m%d%H%M%SZ").to_string())
}

/// The certificate serial for `key_id`, as Python derives it: the top 63 bits of its SHA-256.
pub fn ssh_serial(key_id: &str) -> u64 {
    let digest = Sha256::digest(key_id.as_bytes());
    let mut top = [0u8; 8];
    top.copy_from_slice(&digest[..8]);
    u64::from_be_bytes(top) >> 1
}

/// Each problem with the key set in `dir`, one line apiece; empty when every key parses.
pub fn check_state(dir: &Path) -> Vec<String> {
    let mut problems = Vec::new();
    if let Err(e) = load_grant(dir) {
        problems.push(format!("{e:#}"));
    }
    if let Err(e) = load_ack(dir) {
        problems.push(format!("{e:#}"));
    }
    let ca = load_ssh_ca(dir).map_err(|e| problems.push(format!("{e:#}"))).ok();
    if let Err(e) = load_ssh_ca_pub(dir, ca.as_ref()) {
        problems.push(format!("{e:#}"));
    }
    problems
}

impl Keys {
    /// The key set in `dir`. Never creates a key: a missing or unparsable file is an error naming it.
    pub fn load(dir: &Path) -> anyhow::Result<Keys> {
        let grant = load_grant(dir)?;
        let ack = load_ack(dir)?;
        let ssh_ca = load_ssh_ca(dir)?;
        let ssh_ca_pub = load_ssh_ca_pub(dir, Some(&ssh_ca))?;
        Ok(Keys { grant, ack, ssh_ca, ssh_ca_pub })
    }

    /// A fresh key set written to `dir` in the formats Python writes, for tests and fixtures only.
    pub fn generate(dir: &Path) -> anyhow::Result<Keys> {
        fs::create_dir_all(dir).with_context(|| format!("cannot create {}", dir.display()))?;
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?;
        let mut seed = [0u8; 32];
        OsRng.fill_bytes(&mut seed);
        let grant = ed25519_dalek::SigningKey::from_bytes(&seed);
        let grant_pem = KeypairBytes { secret_key: grant.to_bytes(), public_key: None }
            .to_pkcs8_pem(LineEnding::LF)
            .map_err(|e| anyhow!("{GRANT_FILE}: {e}"))?;
        write_new(&dir.join(GRANT_FILE), grant_pem.as_bytes(), 0o600)?;
        let ack_pem = p256::ecdsa::SigningKey::random(&mut OsRng)
            .to_pkcs8_pem(LineEnding::LF)
            .map_err(|e| anyhow!("{ACK_FILE}: {e}"))?;
        write_new(&dir.join(ACK_FILE), ack_pem.as_bytes(), 0o600)?;
        let mut ca = PrivateKey::random(&mut OsRng, Algorithm::Ed25519).map_err(|e| anyhow!("{SSH_CA_FILE}: {e}"))?;
        ca.set_comment(SSH_CA_COMMENT);
        let ca_pem = ca.to_openssh(ssh_key::LineEnding::LF).map_err(|e| anyhow!("{SSH_CA_FILE}: {e}"))?;
        write_new(&dir.join(SSH_CA_FILE), ca_pem.as_bytes(), 0o600)?;
        let ca_pub = ca.public_key().to_openssh().map_err(|e| anyhow!("{SSH_CA_PUB_FILE}: {e}"))?;
        write_new(&dir.join(SSH_CA_PUB_FILE), format!("{ca_pub}\n").as_bytes(), 0o644)?;
        Keys::load(dir)
    }

    /// The grant key's raw 32-byte public key, base64url.
    pub fn grant_pub_b64u(&self) -> String {
        b64u(&self.grant.verifying_key().to_bytes())
    }

    /// The grant signing key.
    pub fn grant_signing_key(&self) -> &ed25519_dalek::SigningKey {
        &self.grant
    }

    /// `{field: body, sig, key_id}`, signed with the grant key.
    pub fn envelope(&self, field: &str, body: Value) -> Value {
        sign_envelope(&self.grant, field, body)
    }

    /// The ack key's DER SubjectPublicKeyInfo, base64url, as WebCrypto imports it.
    pub fn ack_pub_spki_b64u(&self) -> String {
        self.ack.verifying_key().to_public_key_der().map(|d| b64u(d.as_bytes())).unwrap_or_default()
    }

    /// The body (an object) plus `sig`: a P1363 ECDSA-SHA256 signature over its canonical JSON.
    pub fn sign_ack(&self, body: Value) -> Value {
        let mut body: Map<String, Value> = body.as_object().cloned().unwrap_or_default();
        let sig: p256::ecdsa::Signature =
            p256::ecdsa::signature::Signer::sign(&self.ack, &canonical(&Value::Object(body.clone())));
        body.insert("sig".into(), Value::String(b64u(&sig.to_bytes())));
        Value::Object(body)
    }

    /// The ssh user CA's public key line.
    pub fn ssh_ca_pub(&self) -> String {
        self.ssh_ca_pub.clone()
    }

    /// The `*-cert.pub` line `ssh-keygen -s ssh_user_ca -I key_id -n principals -V … -z serial` writes.
    pub fn ssh_cert(&self, pubkey_line: &str, principals: &[String], not_before: i64, not_after: i64, key_id: &str) -> anyhow::Result<String> {
        let mut nonce = [0u8; 32];
        OsRng.fill_bytes(&mut nonce);
        self.ssh_cert_with_nonce(pubkey_line, principals, not_before, not_after, key_id, nonce)
    }

    /// [`Keys::ssh_cert`] with a given nonce, so a test can match an ssh-keygen certificate byte for byte.
    pub fn ssh_cert_with_nonce(&self, pubkey_line: &str, principals: &[String], not_before: i64, not_after: i64, key_id: &str, nonce: [u8; 32]) -> anyhow::Result<String> {
        let mut fields = pubkey_line.split_whitespace();
        let (Some(kind), Some(blob)) = (fields.next(), fields.next()) else { bail!("not an ssh public key line") };
        let subject = PublicKey::from_openssh(&format!("{kind} {blob}")).map_err(|e| anyhow!("not an ssh public key: {e}"))?;
        if matches!(subject.key_data(), KeyData::Other(_)) {
            bail!("ssh key type {kind} is not supported");
        }
        let comment = pubkey_line.trim().splitn(3, char::is_whitespace).nth(2).map(str::trim).unwrap_or("");
        let names: Vec<&str> = principals.iter().flat_map(|p| p.split(',')).collect();
        if names.is_empty() || names.iter().any(|n| n.is_empty()) {
            bail!("Empty principal name");
        }
        let (Ok(after), Ok(before)) = (u64::try_from(not_before), u64::try_from(not_after)) else {
            bail!("invalid validity interval {}:{}", utc(not_before), utc(not_after));
        };
        if before <= after {
            bail!("Empty certificate validity interval");
        }
        let mut b = Builder::new(nonce.to_vec(), subject.key_data().clone(), after, before)?;
        b.serial(ssh_serial(key_id))?.cert_type(CertType::User)?.key_id(key_id)?.comment(comment)?;
        for n in names {
            b.valid_principal(n)?;
        }
        for ext in DEFAULT_USER_EXTENSIONS {
            b.extension(ext, "")?;
        }
        let cert = b.sign(&self.ssh_ca).map_err(|e| anyhow!("cannot sign the certificate: {e}"))?;
        Ok(cert.to_openssh()?)
    }
}
