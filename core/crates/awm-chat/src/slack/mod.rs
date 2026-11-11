//! Reading Slack's Web API objects, and the one client both kinds of account share.

use awm_plugin_sdk::VerbError;
use serde_json::{json, Value};

use crate::{Attachment, ChatKind};

pub mod api;

pub fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

pub fn truthy(v: &Value, k: &str) -> bool {
    match v.get(k) {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(_) => true,
    }
}

pub fn ts_f(t: &str) -> f64 {
    t.parse().unwrap_or(0.0)
}

/// Python's `repr` of a string, for error text that quoted with `!r`.
pub fn repr(x: &str) -> String {
    format!("'{x}'")
}

/// The `Channel.kind` of a conversation: `dm`, `group`, `private` or `channel`.
pub fn chat_kind(ch: &Value) -> &'static str {
    if truthy(ch, "is_im") {
        "dm"
    } else if truthy(ch, "is_mpim") {
        "group"
    } else if truthy(ch, "is_private") {
        "private"
    } else {
        "channel"
    }
}

pub fn kind_of(ch: &Value) -> ChatKind {
    match chat_kind(ch) {
        "dm" => ChatKind::Dm,
        "group" => ChatKind::Group,
        _ => ChatKind::Channel,
    }
}

/// A message event's `channel_type`: `im`, `mpim`, else a channel.
pub fn kind_of_type(channel_type: &str) -> Option<ChatKind> {
    match channel_type {
        "" => None,
        "im" => Some(ChatKind::Dm),
        "mpim" => Some(ChatKind::Group),
        _ => Some(ChatKind::Channel),
    }
}

pub fn api_err(e: api::ApiError) -> VerbError {
    e.verb()
}

/// A Slack message's `files[]`. A hidden or tombstoned file with no url is skipped.
pub fn attachments(m: &Value) -> Vec<Attachment> {
    let mut out = vec![];
    for (i, f) in m.get("files").and_then(Value::as_array).into_iter().flatten().enumerate() {
        let s = |k: &str| f.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let url = Some(s("url_private_download")).filter(|u| !u.is_empty()).unwrap_or_else(|| s("url_private"));
        if url.is_empty() && matches!(s("mode").as_str(), "hidden" | "tombstone") {
            continue;
        }
        let filename = [s("name"), s("title")].into_iter().find(|n| !n.is_empty()).unwrap_or_else(|| format!("attachment-{i}"));
        out.push(Attachment {
            idx: i as i64,
            filename,
            mime: s("mimetype"),
            size: f.get("size").and_then(Value::as_i64).unwrap_or(0),
            url: url.clone(),
            r#ref: json!({"id": s("id"), "url": url}),
        });
    }
    out
}

/// Slack user ids are `U…`, or `W…` on enterprise grid, in capitals.
pub fn looks_like_user_id(s: &str) -> bool {
    let mut c = s.chars();
    matches!(c.next(), Some('U' | 'W')) && s.len() > 1 && s[1..].chars().all(|c| c.is_ascii_alphanumeric()) && s == s.to_uppercase()
}
