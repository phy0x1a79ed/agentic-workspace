//! Task ids (sessions.md R58): the first 8 characters of a UUID4 in URL-safe base64.

use awm_trust::grant::b64u;
use uuid::Uuid;

pub fn mint_task_id() -> String {
    from_uuid(Uuid::new_v4())
}

pub fn from_uuid(u: Uuid) -> String {
    b64u(u.as_bytes())[..8].to_string()
}

pub fn is_task_id(s: &str) -> bool {
    s.len() == 8 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// One task id per federation-board card, so a replayed post opens no second card.
pub fn intake_task_id(board_card: &str) -> String {
    from_uuid(Uuid::new_v5(&Uuid::NAMESPACE_URL, format!("awm-board:{board_card}").as_bytes()))
}

/// An opaque URL-safe token of `n` random bytes, as Python's `secrets.token_urlsafe(n)`.
pub fn token(n: usize) -> String {
    use rand::RngCore;
    let mut buf = vec![0u8; n];
    rand::thread_rng().fill_bytes(&mut buf);
    b64u(&buf)
}
