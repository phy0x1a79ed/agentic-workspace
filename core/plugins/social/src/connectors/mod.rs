//! One live platform connection per configured account (`connectors/base.py`).
//!
//! A connector owns the socket to one platform for one account. It runs its own receive loop
//! with its own reconnect, and hands each inbound message to [`Hooks::on_message`]. Persistence
//! of the live feed, the emit and the seat routing stay in the plugin: a connector writes only the
//! synced history of a bot account, through [`Hooks::history`].

use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;
use awm_plugin_sdk::VerbError;
use base64::Engine;
use futures_util::future::BoxFuture;
use serde::Serialize;
use serde_json::{json, Value};

use crate::config::AccountConfig;
use crate::db::{Db, History};

pub mod blocks;
pub mod discord;
pub mod gmail;
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

/// A slash command, from Discord's `/approve`.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct Command {
    pub account: String,
    pub platform: String,
    pub name: String,
    pub options: serde_json::Map<String, Value>,
    pub user_id: String,
    pub user_name: String,
    pub channel_id: String,
    pub is_dm: bool,
    pub operator: String,
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

/// One file to send: inline `text`, base64 `b64`, or a `path` on this node.
#[derive(Debug, Clone, PartialEq)]
pub enum FileBody {
    Text(String),
    B64(String),
    Path(PathBuf),
}

#[derive(Debug, Clone, PartialEq)]
pub struct OutFile {
    pub filename: String,
    pub body: FileBody,
}

impl OutFile {
    /// `{filename}` plus one of `path`, `b64` or `text`, as `send`'s `files` holds it.
    pub fn from_value(v: &Value) -> Result<Self> {
        let filename = v.get("filename").and_then(Value::as_str).unwrap_or("").to_string();
        let body = if let Some(p) = v.get("path").and_then(Value::as_str) {
            FileBody::Path(PathBuf::from(p))
        } else if let Some(b) = v.get("b64").and_then(Value::as_str) {
            FileBody::B64(b.to_string())
        } else {
            FileBody::Text(match v.get("text") {
                Some(Value::String(s)) => s.clone(),
                None | Some(Value::Null) => String::new(),
                Some(other) => other.to_string(),
            })
        };
        Ok(Self { filename, body })
    }

    pub fn bytes(&self) -> Result<Vec<u8>> {
        match &self.body {
            FileBody::Text(t) => Ok(t.as_bytes().to_vec()),
            FileBody::B64(b) => base64::engine::general_purpose::STANDARD.decode(b).map_err(|e| value(format!("bad base64: {e}"))),
            FileBody::Path(p) => std::fs::read(p).map_err(|e| runtime(format!("{}: {e}", p.display()))),
        }
    }

    pub fn size(&self) -> u64 {
        match &self.body {
            FileBody::Text(t) => t.len() as u64,
            FileBody::B64(b) => (b.len() * 3 / 4 - b[b.len().saturating_sub(2)..].matches('=').count()) as u64,
            FileBody::Path(p) => std::fs::metadata(p).map(|m| m.len()).unwrap_or(0),
        }
    }
}

/// What `send` hands back: at least `message_id`, plus `channel_id`, `ts` and `files`.
pub type Sent = serde_json::Map<String, Value>;

/// One downloaded attachment.
#[derive(Debug, Clone, PartialEq)]
pub struct Downloaded {
    pub filename: String,
    pub mime: String,
    pub data: Vec<u8>,
}

pub type OnMessage = Arc<dyn Fn(Inbound) -> BoxFuture<'static, ()> + Send + Sync>;
pub type OnCommand = Arc<dyn Fn(Command) -> BoxFuture<'static, ()> + Send + Sync>;

/// What the plugin hands a connector.
#[derive(Clone)]
pub struct Hooks {
    pub on_message: OnMessage,
    pub on_command: Option<OnCommand>,
    /// The synced history, for a connector that keeps one.
    pub history: History,
    /// The operator table, for Discord's `/approve` gate.
    pub db: Db,
}

/// One account's live connection.
#[async_trait]
pub trait Connector: Send + Sync {
    fn platform(&self) -> &str;
    fn account(&self) -> &AccountConfig;
    /// A bot that keeps a synced history of every chat it is in.
    fn keeps_history(&self) -> bool {
        false
    }
    fn sends_files(&self) -> bool {
        false
    }
    fn max_file_bytes(&self) -> u64 {
        0
    }

    /// Run the receive loop until the task is cancelled. Owns its reconnect and backoff, and
    /// returns only on a permanent failure such as a bad token.
    async fn start(self: Arc<Self>) -> anyhow::Result<()>;

    async fn send(&self, _channel: &str, _text: &str, _thread: Option<&str>, _files: &[OutFile]) -> Result<Sent> {
        Err(unsupported(format!("{} account {:?} is read only", self.platform(), self.account().name)))
    }

    /// Existing messages, oldest first, live from the platform. `before` pages backwards.
    async fn fetch(&self, _channel: &str, _limit: usize, _before: Option<&str>) -> Result<Vec<Inbound>> {
        Err(unsupported(format!("{} connector does not support fetch", self.platform())))
    }

    async fn search(&self, _query: &str, _limit: usize, _channel: Option<&str>) -> Result<Vec<Inbound>> {
        Err(unsupported(format!("{} connector does not support search", self.platform())))
    }

    /// Re-fetch one message and return its attachments' bytes, or only attachment `idx`.
    async fn download_attachments(&self, _channel: &str, _message_id: &str, _idx: Option<i64>) -> Result<Vec<Downloaded>> {
        Err(unsupported(format!("{} connector does not support attachment download", self.platform())))
    }

    /// `emoji` is a character, a name or `:name:` (see [`emoji`]).
    async fn react(&self, _channel: &str, _message_id: &str, _emoji: &str, _remove: bool) -> Result<Value> {
        Err(unsupported(format!("{} connector does not support reactions", self.platform())))
    }

    async fn list_channels(&self, include_dms: bool) -> Result<Vec<Channel>>;

    /// The chats a session may mount. Defaults to every channel and DM.
    async fn list_seats(&self) -> Result<Vec<Channel>> {
        self.list_channels(true).await
    }

    async fn open_dm(&self, _user: &str) -> Result<Channel> {
        Err(unsupported(format!("{} connector does not support opening DMs", self.platform())))
    }

    async fn identity(&self) -> Result<Identity>;

    /// Whether anyone but this account has written in a DM: `send` refuses a first contact.
    /// Channels always answer true.
    async fn writable(&self, _channel: &str) -> Result<bool> {
        Ok(true)
    }

    /// Whether the live connection is up, for `accounts`.
    fn live(&self) -> bool;

    async fn close(&self) {}
}

/// The `react` result once Slack answered `reactions.add` or `.remove` with `error`.
pub fn slack_reaction(account: &str, channel: &str, message_id: &str, name: &str, remove: bool, error: &str) -> Result<Value> {
    if error == "missing_scope" {
        return Err(runtime(format!("slack account {account:?} cannot react until its app holds the reactions:write scope")));
    }
    if !error.is_empty() && error != "already_reacted" && error != "no_reaction" {
        return Err(runtime(format!("slack reactions.{}: {error}", if remove { "remove" } else { "add" })));
    }
    Ok(json!({"as": "reaction", "emoji": name, "removed": remove, "message_id": message_id, "channel_id": channel}))
}

/// A Slack message's `files[]`. A hidden or tombstoned file with no url is skipped.
pub fn slack_attachments(m: &Value) -> Vec<Attachment> {
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
pub fn looks_like_slack_user_id(s: &str) -> bool {
    let mut c = s.chars();
    matches!(c.next(), Some('U' | 'W')) && s.len() > 1 && s[1..].chars().all(|c| c.is_ascii_alphanumeric()) && s == s.to_uppercase()
}

/// Build one account's connector: a Slack token routes by its prefix inside the Slack client.
pub fn build(cfg: AccountConfig, hooks: Hooks) -> anyhow::Result<Arc<dyn Connector>> {
    Ok(match cfg.platform.as_str() {
        "slack" => slack::SlackConnector::create(cfg, hooks)?,
        "discord" => discord::DiscordConnector::create(cfg, hooks)?,
        "gmail" => gmail::GmailConnector::create(cfg, hooks)?,
        other => anyhow::bail!("no connector for platform {other:?}"),
    })
}

/// Emoji names for reactions (`emoji.py`): Slack takes a name, Discord the character.
pub mod emoji {
    const BY_NAME: [(&str, &str); 12] = [
        ("eyes", "👀"),
        ("+1", "👍"),
        ("thumbsup", "👍"),
        ("white_check_mark", "✅"),
        ("heart", "❤️"),
        ("tada", "🎉"),
        ("pray", "🙏"),
        ("x", "❌"),
        ("rocket", "🚀"),
        ("thinking_face", "🤔"),
        ("joy", "😂"),
        ("wave", "👋"),
    ];

    fn bare(e: &str) -> &str {
        e.trim().trim_matches(':')
    }

    fn name_of(c: &str) -> Option<&'static str> {
        let stripped = c.trim_end_matches('\u{fe0f}');
        BY_NAME.iter().find(|(_, ch)| *ch == c || ch.trim_end_matches('\u{fe0f}') == stripped).map(|(n, _)| *n)
    }

    /// The character for `emoji`, or None for a name the table lacks.
    pub fn to_char(emoji: &str) -> Option<String> {
        let e = emoji.trim();
        if name_of(e).is_some() || (!e.is_empty() && !e.is_ascii()) {
            return Some(e.to_string());
        }
        let b = bare(e).to_lowercase();
        BY_NAME.iter().find(|(n, _)| *n == b).map(|(_, c)| c.to_string())
    }

    /// The Slack name for `emoji`. An unknown name passes through bare.
    pub fn to_name(emoji: &str) -> String {
        let e = emoji.trim();
        name_of(e).map(str::to_string).unwrap_or_else(|| bare(e).to_string())
    }
}
