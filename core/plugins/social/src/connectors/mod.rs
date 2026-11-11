//! One live platform connection per configured account (`connectors/base.py`).
//!
//! A connector owns the socket to one platform for one account. It runs its own receive loop
//! with its own reconnect, and hands each inbound message to [`Hooks::on_message`]. Persistence
//! of the live feed, the emit and the seat routing stay in the plugin: a connector writes only the
//! synced history of a bot account, through [`Hooks::history`].

use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;
use base64::Engine;
use futures_util::future::BoxFuture;
use serde::Serialize;
use serde_json::{json, Value};

use crate::config::AccountConfig;
use crate::db::{Db, History};

pub mod discord;
pub mod slack;

pub use awm_chat::{blocks, permission, runtime, unsupported, value, Attachment, Channel, ChatKind, Downloaded, Identity, Inbound, Result};
pub use awm_chat::slack::{attachments as slack_attachments, looks_like_user_id as looks_like_slack_user_id};

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
        let Some(o) = v.as_object() else {
            return Err(value(format!("a file is {{filename, path|b64|text}}, not {v}")));
        };
        let str_of = |k: &str| o.get(k).and_then(Value::as_str);
        let body = if let Some(p) = str_of("path") {
            FileBody::Path(PathBuf::from(p))
        } else if let Some(b) = str_of("b64") {
            FileBody::B64(b.to_string())
        } else {
            FileBody::Text(match o.get("text") {
                Some(Value::String(s)) => s.clone(),
                None | Some(Value::Null) => return Err(value(format!("a file needs path, b64 or text: {v}"))),
                Some(other) => other.to_string(),
            })
        };
        let filename = match (str_of("filename").filter(|f| !f.is_empty()), &body) {
            (Some(f), _) => f.to_string(),
            (None, FileBody::Path(p)) => p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default(),
            _ => String::new(),
        };
        if filename.is_empty() {
            return Err(value(format!("a file needs a filename: {v}")));
        }
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

/// Build one account's connector: a Slack token routes by its prefix inside the Slack client.
pub fn build(cfg: AccountConfig, hooks: Hooks) -> anyhow::Result<Arc<dyn Connector>> {
    Ok(match cfg.platform.as_str() {
        "slack" => slack::SlackConnector::create(cfg, hooks)?,
        "discord" => discord::DiscordConnector::create(cfg, hooks)?,
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
