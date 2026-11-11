//! The chat types and the Slack Web API client that `social` (the bots) and `productivity`
//! (Tony's own accounts) share.

use std::path::Path;

use awm_plugin_sdk::VerbError;
use serde::Serialize;
use serde_json::{json, Value};

pub mod blocks;
pub mod config;
pub mod slack;

pub type Result<T> = std::result::Result<T, VerbError>;

/// The error classes the Python raised, which callers read from `error_class`.
pub fn runtime(msg: impl Into<String>) -> VerbError {
    VerbError::new("RuntimeError", msg)
}
pub fn value(msg: impl Into<String>) -> VerbError {
    VerbError::new("ValueError", msg)
}
pub fn unsupported(msg: impl Into<String>) -> VerbError {
    VerbError::new("NotImplementedError", msg)
}
pub fn permission(msg: impl Into<String>) -> VerbError {
    VerbError::new("PermissionError", msg)
}

/// A file on a message. `url` and `ref` are hints only: a download re-fetches the message.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct Attachment {
    pub idx: i64,
    pub filename: String,
    pub mime: String,
    pub size: i64,
    pub url: String,
    #[serde(skip)]
    pub r#ref: Value,
}

/// Where a chat sits, for a seat's line and session name.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ChatKind {
    /// A direct message with one person.
    Dm,
    /// A group DM.
    Group,
    #[default]
    Channel,
}

/// A message from a platform, normalised. `to_holder` is false for a message kept from a seat.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Inbound {
    pub account: String,
    pub platform: String,
    pub channel_id: String,
    pub channel_name: String,
    pub text: String,
    pub sender_id: String,
    pub sender_name: String,
    pub thread_id: String,
    pub message_id: String,
    pub ts: String,
    pub attachments: Vec<Attachment>,
    pub sender_bot: bool,
    pub to_holder: bool,
    /// The workspace or guild name. Empty for a DM.
    pub server: String,
    pub kind: ChatKind,
    /// The sender is outside the lab: a Slack restricted or foreign-team user.
    pub outside: bool,
    /// Found by a resync after a gap, not by the live feed.
    pub resynced: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Channel {
    pub id: String,
    pub name: String,
    pub kind: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Identity {
    pub id: String,
    pub name: String,
}

/// One downloaded attachment.
#[derive(Debug, Clone, PartialEq)]
pub struct Downloaded {
    pub filename: String,
    pub mime: String,
    pub data: Vec<u8>,
}

pub fn att(a: &Attachment) -> Value {
    json!({"idx": a.idx, "filename": a.filename, "mime": a.mime, "size": a.size, "url": a.url})
}

/// A message as a verb returns it. `awm_user` is the awm user the sender maps to, if any.
pub fn msg(m: &Inbound, awm_user: &str) -> Value {
    json!({
        "account": m.account, "platform": m.platform, "direction": "in", "channel_id": m.channel_id, "channel_name": m.channel_name,
        "thread_id": m.thread_id, "sender_id": m.sender_id, "sender_name": m.sender_name, "sender_bot": m.sender_bot,
        "awm_user": awm_user, "text": m.text, "ts": m.ts, "message_id": m.message_id,
        "attachments": m.attachments.iter().map(att).collect::<Vec<_>>(),
    })
}

pub fn chan(c: &Channel) -> Value {
    json!({"id": c.id, "name": c.name, "kind": c.kind})
}

/// A filesystem-safe, non-empty basename for one attachment.
pub fn safe_name(name: &str, idx: usize) -> String {
    let base = Path::new(name).file_name().map(|b| b.to_string_lossy().trim().to_string()).unwrap_or_default();
    let mut out = String::new();
    for c in base.chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
            out.push(c);
        } else if !out.ends_with('_') {
            out.push('_');
        }
    }
    let out = out.trim_matches(|c| c == '.' || c == '_').to_string();
    if out.is_empty() { format!("attachment-{idx}") } else { out }
}

/// What one reply carries inline, raw bytes before base64: the gateway's limit.
pub const MAX_INLINE_BYTES: usize = 24 * 1024 * 1024;

/// Downloaded files as the `b64` entries the caller's gateway writes to disk on its own node.
pub fn inline_files(files: &[Downloaded], node: &str) -> Result<Value> {
    use base64::Engine;
    let total: usize = files.iter().map(|f| f.data.len()).sum();
    if total > MAX_INLINE_BYTES {
        return Err(value(format!("the attachments come to {total} bytes, over the {MAX_INLINE_BYTES} a reply carries; fetch one at a time with idx")));
    }
    let b64 = base64::engine::general_purpose::STANDARD;
    let out: Vec<Value> = files
        .iter()
        .enumerate()
        .map(|(i, f)| json!({"filename": safe_name(&f.filename, i), "mime": if f.mime.is_empty() { "application/octet-stream" } else { &f.mime }, "size": f.data.len(), "b64": b64.encode(&f.data)}))
        .collect();
    Ok(json!({"files": out, "count": out.len(), "node": node}))
}
