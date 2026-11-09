//! A room's escalation to encryption, exactly as the relay's how-to tells a guest.
//!
//! Symmetric SPAKE2 over Ed25519, as Rust `spake2` 0.4 and Python `spake2` do it. The identity
//! binds the exchange to one room. HKDF-SHA256 then gives one key per direction, and
//! ChaCha20-Poly1305 seals each message with its count as the nonce. The how-to is the guest's
//! whole protocol, so every constant here must match its text in `tether-relay/src/room.rs`.
//!
//! An offer's SPAKE2 state lives in memory only: `spake2` 0.4 cannot serialise it, and the
//! secret scalar then never reaches the disk. An offer open when the plugin restarts is gone.

use base64::Engine;
use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};
use hkdf::Hkdf;
use rand::Rng;
use sha2::Sha256;
use spake2::{Ed25519Group, Identity, Password, Spake2};

pub const HOST_TO_GUEST: &str = "tether-room host to guest";
pub const GUEST_TO_HOST: &str = "tether-room guest to host";

/// No 0, o, 1, l or i, so a password survives being read aloud.
const ALPHABET: &[u8] = b"abcdefghjkmnpqrstuvwxyz23456789";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CryptoError(pub String);

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::STANDARD;

/// Twelve characters in three groups of four, about 59 bits.
pub fn new_password() -> String {
    let mut rng = rand::rng();
    let raw: Vec<char> = (0..12).map(|_| ALPHABET[rng.random_range(0..ALPHABET.len())] as char).collect();
    raw.chunks(4).map(|c| c.iter().collect::<String>()).collect::<Vec<_>>().join("-")
}

fn identity(room: &str) -> Identity {
    Identity::new(format!("tether-room:{room}").as_bytes())
}

/// An offer out to the guest: the state that finishes it.
pub struct Offer(Spake2<Ed25519Group>);

/// Start SPAKE2: the offer to keep and our message to post, as base64.
pub fn offer(room: &str, password: &str) -> (Offer, String) {
    let (state, ours) = Spake2::<Ed25519Group>::start_symmetric(&Password::new(password.as_bytes()), &identity(room));
    (Offer(state), B64.encode(ours))
}

/// The host's two keys, as hex.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Keys {
    pub out: String,
    pub r#in: String,
}

/// Finish SPAKE2 with the guest's message.
pub fn finish(room: &str, offer: Offer, theirs: &str) -> Result<Keys, CryptoError> {
    let theirs = unb64(theirs)?;
    let k = offer.0.finish(&theirs).map_err(|e| CryptoError(format!("not a SPAKE2 message: {e:?}")))?;
    Ok(Keys { out: hex::encode(derive(&k, room, HOST_TO_GUEST)), r#in: hex::encode(derive(&k, room, GUEST_TO_HOST)) })
}

pub fn derive(k: &[u8], room: &str, info: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    Hkdf::<Sha256>::new(Some(room.as_bytes()), k).expand(info.as_bytes(), &mut out).expect("32 bytes is a valid HKDF length");
    out
}

fn nonce(n: i64) -> [u8; 12] {
    let mut out = [0u8; 12];
    out[4..].copy_from_slice(&(n as u64).to_be_bytes());
    out
}

fn cipher(key: &str) -> Result<ChaCha20Poly1305, CryptoError> {
    let raw = hex::decode(key).map_err(|_| CryptoError("the key is not hex".into()))?;
    if raw.len() != 32 {
        return Err(CryptoError("the key is not 32 bytes".into()));
    }
    Ok(ChaCha20Poly1305::new(Key::from_slice(&raw)))
}

pub fn seal(key: &str, n: i64, room: &str, text: &str) -> Result<String, CryptoError> {
    let sealed = cipher(key)?
        .encrypt(Nonce::from_slice(&nonce(n)), Payload { msg: text.as_bytes(), aad: room.as_bytes() })
        .map_err(|_| CryptoError("could not seal".into()))?;
    Ok(B64.encode(sealed))
}

pub fn open(key: &str, n: i64, room: &str, sealed: &str) -> Result<String, CryptoError> {
    let plain = cipher(key)?
        .decrypt(Nonce::from_slice(&nonce(n)), Payload { msg: &unb64(sealed)?, aad: room.as_bytes() })
        .map_err(|_| CryptoError("a sealed message did not open".into()))?;
    Ok(String::from_utf8_lossy(&plain).into_owned())
}

/// The nonce of one chunk of a sealed file: a last-chunk flag as 4 bytes, then the chunk's
/// index as 8, both big-endian. The flag makes a file cut at a chunk boundary fail to open.
fn chunk_nonce(index: u64, last: bool) -> [u8; 12] {
    let mut out = [0u8; 12];
    out[..4].copy_from_slice(&(last as u32).to_be_bytes());
    out[4..].copy_from_slice(&index.to_be_bytes());
    out
}

/// One chunk of a sealed file, under the file's own key.
pub fn seal_chunk(key: &[u8; 32], index: u64, last: bool, plain: &[u8]) -> Vec<u8> {
    ChaCha20Poly1305::new(Key::from_slice(key)).encrypt(Nonce::from_slice(&chunk_nonce(index, last)), plain).expect("sealing in memory does not fail")
}

pub fn open_chunk(key: &[u8; 32], index: u64, last: bool, sealed: &[u8]) -> Result<Vec<u8>, CryptoError> {
    ChaCha20Poly1305::new(Key::from_slice(key))
        .decrypt(Nonce::from_slice(&chunk_nonce(index, last)), sealed)
        .map_err(|_| CryptoError(format!("chunk {index} of a sealed file did not open")))
}

pub fn b64(bytes: &[u8]) -> String {
    B64.encode(bytes)
}

pub fn unb64(s: &str) -> Result<Vec<u8>, CryptoError> {
    B64.decode(s.as_bytes()).map_err(|_| CryptoError("not base64".into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_sealed_message_opens_only_with_its_key_count_and_room() {
        let k = "11".repeat(32);
        let sealed = seal(&k, 3, "acre-anchor-kettle", "hi").unwrap();
        assert_eq!(open(&k, 3, "acre-anchor-kettle", &sealed).unwrap(), "hi");
        assert!(open(&k, 4, "acre-anchor-kettle", &sealed).is_err());
        assert!(open(&k, 3, "acre-anchor-other", &sealed).is_err());
        assert!(open(&"22".repeat(32), 3, "acre-anchor-kettle", &sealed).is_err());
    }
}
