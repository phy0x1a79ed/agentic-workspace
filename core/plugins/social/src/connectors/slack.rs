//! Slack: one Web API client serves both the Opal bot and Tony's read-only account
//! (`slack_bot_conn.py`, `slack_conn.py`).
//!
//! [`SlackConnector::create`] routes by the token: an `xoxb-` token is the bot, anything else is a
//! person's own account (see [`user`]). The API base is `AWM_SLACK_API`, default
//! `https://slack.com/api`.
//!
//! The bot acts as a Slack app's bot user, never as a person. Receive is Socket Mode through an
//! `xapp-` app token; with none, the bot sends but does not receive. It keeps a synced history:
//! live events write it as they arrive, edits and deletes included, and a delta sync over the
//! bot's memberships fills whatever arrived while the socket was down, at start, after each
//! `hello`, every [`RESYNC_S`], and on the first message from a chat it has not synced yet.
//!
//! Only the sync moves a chat's cursor, and only to the newest top-level message it saw: a live
//! event can land before the sync that covers a gap, and must not step the cursor over it. Each
//! sync re-reads the replies newer than the cursor on every thread with activity in the
//! [`THREAD_WINDOW_S`] below it. A message the sync finds after an existing cursor goes on to
//! `on_message` marked `resynced`; a chat's first sync, its backfill, only records.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::{Backoff, VerbError};
use serde_json::{json, Value};
use tokio::sync::{mpsc, Notify};

use super::blocks::slack_text;
use super::{
    emoji, looks_like_slack_user_id, runtime, slack_attachments, slack_reaction, value, Channel, ChatKind, Connector, Downloaded, Hooks, Identity, Inbound,
    OutFile, Result, Sent,
};
use crate::config::AccountConfig;
use api::{params, ApiError, Client};

mod api;
mod socket;
mod user;
#[cfg(test)]
mod tests;

pub const RESYNC_S: u64 = 600;
/// How far back the first sync of a chat reaches, in messages.
pub const BACKFILL_MAX: usize = 1000;
/// How far below a chat's cursor a sync looks for threads with new replies.
pub const THREAD_WINDOW_S: f64 = 7.0 * 86400.0;
/// The subtypes that are a message someone posted. Edits and deletes are handled apart.
const POSTED: [&str; 4] = ["", "file_share", "thread_broadcast", "bot_message"];
const MEMBER_TYPES: &str = "public_channel,private_channel,mpim,im";

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn truthy(v: &Value, k: &str) -> bool {
    match v.get(k) {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(_) => true,
    }
}

fn posted(m: &Value) -> bool {
    POSTED.contains(&s(m, "subtype"))
}

fn ts_f(t: &str) -> f64 {
    t.parse().unwrap_or(0.0)
}

/// Python's `repr` of a string, for error text that quoted with `!r`.
fn repr(x: &str) -> String {
    format!("'{x}'")
}

/// Whether `ts` is strictly newer than `since`, an absent `since` being older than anything.
fn after(ts: &str, since: Option<&str>) -> bool {
    !ts.is_empty() && since.is_none_or(|s| ts_f(ts) > ts_f(s))
}

/// The `Channel.kind` of a conversation: `dm`, `group`, `private` or `channel`.
fn chat_kind(ch: &Value) -> &'static str {
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

fn kind_of(ch: &Value) -> ChatKind {
    match chat_kind(ch) {
        "dm" => ChatKind::Dm,
        "group" => ChatKind::Group,
        _ => ChatKind::Channel,
    }
}

/// A message event's `channel_type`: `im`, `mpim`, else a channel.
fn kind_of_type(channel_type: &str) -> Option<ChatKind> {
    match channel_type {
        "" => None,
        "im" => Some(ChatKind::Dm),
        "mpim" => Some(ChatKind::Group),
        _ => Some(ChatKind::Channel),
    }
}

fn api_err(e: ApiError) -> VerbError {
    e.verb()
}

/// Background tasks that die with the loop that spawned them.
struct Tasks(Vec<tokio::task::JoinHandle<()>>);

impl Drop for Tasks {
    fn drop(&mut self) {
        for t in &self.0 {
            t.abort();
        }
    }
}

/// Waits for `close`, which may come before anyone waits.
#[derive(Default)]
struct Shutdown {
    closed: AtomicBool,
    notify: Notify,
}

impl Shutdown {
    fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
        self.notify.notify_one();
    }

    async fn wait(&self) {
        while !self.closed.load(Ordering::SeqCst) {
            self.notify.notified().await;
        }
    }

    /// Sleep `d`, or less if closed. True when closed.
    async fn sleep(&self, d: Duration) -> bool {
        tokio::select! {
            _ = self.wait() => true,
            _ = tokio::time::sleep(d) => self.closed.load(Ordering::SeqCst),
        }
    }
}

#[derive(Default, Clone)]
struct Me {
    user: String,
    bot: String,
    name: String,
    team: String,
}

#[derive(Clone)]
struct Chat {
    name: String,
    kind: ChatKind,
}

/// The Opal bot (`xoxb-` plus an `xapp-` app token).
pub struct SlackConnector {
    cfg: AccountConfig,
    hooks: Hooks,
    api: Client,
    me: std::sync::Mutex<Me>,
    synced: std::sync::Mutex<HashSet<String>>,
    chats: std::sync::Mutex<HashMap<String, Chat>>,
    /// DMs where someone other than Opal wrote.
    spoken_to: std::sync::Mutex<HashSet<String>>,
    resync: Notify,
    sync_lock: tokio::sync::Mutex<()>,
    live: AtomicBool,
    shutdown: Shutdown,
}

impl SlackConnector {
    /// The Slack connector for this account: the bot for an `xoxb-` token, else the read-only user.
    pub fn create(cfg: AccountConfig, hooks: Hooks) -> anyhow::Result<Arc<dyn Connector>> {
        let base = Client::base_from_env();
        Ok(if cfg.token.starts_with("xoxb-") { Arc::new(Self::bot(cfg, hooks, &base)) } else { Arc::new(user::SlackUser::new(cfg, hooks, &base)) })
    }

    fn bot(cfg: AccountConfig, hooks: Hooks, base: &str) -> Self {
        Self {
            api: Client::new(base, &cfg.token, "", None),
            cfg,
            hooks,
            me: Default::default(),
            synced: Default::default(),
            chats: Default::default(),
            spoken_to: Default::default(),
            resync: Notify::new(),
            sync_lock: tokio::sync::Mutex::new(()),
            live: AtomicBool::new(false),
            shutdown: Shutdown::default(),
        }
    }

    fn me(&self) -> Me {
        self.me.lock().map(|m| m.clone()).unwrap_or_default()
    }

    fn is_own(&self, m: &Value) -> bool {
        let me = self.me();
        (!me.bot.is_empty() && s(m, "bot_id") == me.bot) || (!me.user.is_empty() && s(m, "user") == me.user)
    }

    fn server_for(&self, kind: ChatKind) -> String {
        if kind == ChatKind::Dm {
            String::new()
        } else {
            self.me().team
        }
    }

    async fn inbound(&self, channel: &str, m: &Value, channel_name: &str) -> Inbound {
        let user = s(m, "user");
        let sender = if user.is_empty() { s(m, "bot_id") } else { user };
        let person = self.api.person(user).await;
        let name = [person.name, s(m.get("bot_profile").unwrap_or(&Value::Null), "name").to_string(), s(m, "username").to_string()]
            .into_iter()
            .find(|n| !n.is_empty())
            .unwrap_or_else(|| sender.to_string());
        Inbound {
            account: self.cfg.name.clone(),
            platform: "slack".into(),
            channel_id: channel.into(),
            channel_name: channel_name.into(),
            text: slack_text(m),
            sender_id: sender.into(),
            sender_name: name,
            thread_id: s(m, "thread_ts").into(),
            message_id: s(m, "ts").into(),
            ts: s(m, "ts").into(),
            attachments: slack_attachments(m),
            sender_bot: truthy(m, "bot_id"),
            to_holder: true,
            outside: person.outside,
            ..Default::default()
        }
    }

    async fn record(&self, channel: &str, m: &Value, channel_name: &str, edited: bool) -> Inbound {
        let msg = self.inbound(channel, m, channel_name).await;
        self.hooks.history.record(&msg, self.is_own(m), edited).await;
        msg
    }

    async fn chat_name(&self, ch: &Value) -> String {
        if truthy(ch, "is_im") {
            return self.api.person(s(ch, "user")).await.name;
        }
        s(ch, "name").to_string()
    }

    fn chat(&self, cid: &str) -> Option<Chat> {
        self.chats.lock().ok().and_then(|c| c.get(cid).cloned())
    }

    // -- history sync --------------------------------------------------------

    /// Pull everything in one chat newer than its cursor, threads included. Returns what goes on
    /// to `on_message`: the posts after an existing cursor, oldest first.
    async fn sync_channel(&self, cid: &str, chat: &Chat) -> std::result::Result<Vec<Inbound>, ApiError> {
        let cursor = self.hooks.history.synced_ts(cid).await;
        // An empty chat's first sync stores "0", and Slack rejects an `oldest` of zero.
        let since = cursor.clone().filter(|c| ts_f(c) > 0.0);
        let since = since.as_deref();
        let start = since.map_or(0.0, |s| ts_f(s) - THREAD_WINDOW_S);
        let window = (start > 0.0).then(|| format!("{start:.6}"));
        let parents = self
            .api
            .paged("conversations.history", "messages", if since.is_some() { None } else { Some(BACKFILL_MAX) }, &params(&[("channel", Some(cid.into())), ("oldest", window.clone())]))
            .await?;
        let mut route = vec![];
        let mut newest: Option<String> = since.map(str::to_string);
        let mut threads: Vec<String> = vec![];
        for m in &parents {
            if !posted(m) {
                continue;
            }
            let ts = s(m, "ts");
            if after(ts, since) {
                let msg = self.record(cid, m, &chat.name, false).await;
                if cursor.is_some() && !self.is_own(m) {
                    route.push(msg);
                }
                if after(ts, newest.as_deref()) {
                    newest = Some(ts.to_string());
                }
            }
            if truthy(m, "reply_count") && after(s(m, "latest_reply"), since) {
                threads.push(ts.to_string());
            }
        }
        if let Some(w) = &window {
            threads.extend(self.hooks.history.active_threads(cid, w).await);
        }
        threads.sort_by(|a, b| ts_f(a).total_cmp(&ts_f(b)));
        threads.dedup();
        for t in &threads {
            let replies = self.api.paged("conversations.replies", "messages", None, &params(&[("channel", Some(cid.into())), ("ts", Some(t.clone())), ("oldest", since.map(str::to_string))])).await?;
            for r in &replies {
                if s(r, "ts") == t || !posted(r) {
                    continue;
                }
                let msg = self.record(cid, r, &chat.name, false).await;
                if cursor.is_some() && !self.is_own(r) && after(s(r, "ts"), since) {
                    route.push(msg);
                }
            }
        }
        self.hooks.history.set_synced(cid, newest.as_deref().unwrap_or("0"), &chat.name).await;
        if let Ok(mut s) = self.synced.lock() {
            s.insert(cid.to_string());
        }
        let server = self.server_for(chat.kind);
        for m in &mut route {
            m.kind = chat.kind;
            m.server = server.clone();
            m.resynced = true;
        }
        route.sort_by(|a, b| ts_f(&a.ts).total_cmp(&ts_f(&b.ts)));
        Ok(route)
    }

    /// Delta-sync every chat the bot is a member of. Returns the chat count.
    pub async fn sync(&self) -> std::result::Result<usize, ApiError> {
        let _one = self.sync_lock.lock().await;
        let chats = self.api.paged("users.conversations", "channels", None, &[("types", MEMBER_TYPES.into()), ("exclude_archived", "true".into())]).await?;
        for ch in &chats {
            let cid = s(ch, "id");
            let chat = Chat { name: self.chat_name(ch).await, kind: kind_of(ch) };
            if let Ok(mut c) = self.chats.lock() {
                c.insert(cid.to_string(), chat.clone());
            }
            match self.sync_channel(cid, &chat).await {
                Ok(route) => {
                    for m in route {
                        (self.hooks.on_message)(m).await;
                    }
                }
                Err(e) => tracing::warn!("slack-bot[{}] sync of {cid} failed: {e}", self.cfg.name),
            }
        }
        Ok(chats.len())
    }

    async fn resync_loop(self: Arc<Self>) {
        loop {
            tokio::select! {
                _ = self.resync.notified() => {}
                _ = tokio::time::sleep(Duration::from_secs(RESYNC_S)) => {}
            }
            match self.sync().await {
                Ok(n) => tracing::info!("slack-bot[{}] synced {n} chat(s)", self.cfg.name),
                Err(e) => tracing::warn!("slack-bot[{}] sync failed: {e}", self.cfg.name),
            }
        }
    }

    // -- Socket Mode receive -------------------------------------------------

    async fn handle_event(&self, event: Value) {
        if s(&event, "type") != "message" {
            return; // app_mention duplicates the message event
        }
        let cid = s(&event, "channel");
        match s(&event, "subtype") {
            "message_changed" => {
                if let Some(m) = event.get("message").filter(|m| m.is_object()) {
                    self.record(cid, m, "", true).await;
                }
                return;
            }
            "message_deleted" => {
                self.hooks.history.mark_deleted(cid, s(&event, "deleted_ts")).await;
                return;
            }
            _ if !posted(&event) => return,
            _ => {}
        }
        let chat = self.chat(cid);
        let name = chat.as_ref().map(|c| c.name.clone()).unwrap_or_default();
        let mut msg = self.record(cid, &event, &name, false).await;
        if !self.synced.lock().map(|s| s.contains(cid)).unwrap_or(false) {
            self.resync.notify_one(); // first sight of this chat: pull its backlog
        }
        if self.is_own(&event) {
            return;
        }
        msg.kind = kind_of_type(s(&event, "channel_type")).or(chat.map(|c| c.kind)).unwrap_or_default();
        msg.server = self.server_for(msg.kind);
        (self.hooks.on_message)(msg).await;
    }

    async fn authenticate(&self) -> anyhow::Result<()> {
        let mut backoff = Backoff::doubling(1.0, 30.0);
        let auth = loop {
            match self.api.call("auth.test", &[]).await {
                Ok(a) => break a,
                // A transport failure is not a bad token: keep trying.
                Err(e) if e.error.starts_with("http") => {
                    let wait = backoff.wait();
                    tracing::warn!("slack-bot[{}] auth.test failed ({e}); retry in {:.1}s", self.cfg.name, wait.as_secs_f64());
                    if self.shutdown.sleep(wait).await {
                        anyhow::bail!("closed");
                    }
                }
                Err(e) => anyhow::bail!("slack-bot[{}] auth.test failed; account down: {e}", self.cfg.name),
            }
        };
        if let Ok(mut t) = self.api.team_id.lock() {
            *t = s(&auth, "team_id").to_string();
        }
        let team = self.api.team_name(s(&auth, "team")).await;
        tracing::info!("slack-bot[{}] authenticated as {} in {team}", self.cfg.name, s(&auth, "user"));
        if let Ok(mut me) = self.me.lock() {
            *me = Me { user: s(&auth, "user_id").into(), bot: s(&auth, "bot_id").into(), name: s(&auth, "user").into(), team };
        }
        Ok(())
    }

    // -- verbs ---------------------------------------------------------------

    /// The chat id to write to, or `FirstContactRefused`. A user id resolves to the DM with that
    /// user. A DM or group DM is writable once someone other than Opal has written in it; a
    /// channel always is, since posting there reaches no one first.
    async fn writable_id(&self, channel: &str) -> Result<String> {
        let mut channel = channel.to_string();
        if looks_like_slack_user_id(&channel) {
            let r = self.api.call("conversations.open", &[("users", channel.clone())]).await.map_err(api_err)?;
            channel = r.pointer("/channel/id").and_then(Value::as_str).unwrap_or("").to_string();
        }
        if self.spoken_to.lock().map(|s| s.contains(&channel)).unwrap_or(false) {
            return Ok(channel);
        }
        let info = self.api.call("conversations.info", &[("channel", channel.clone())]).await.map_err(api_err)?;
        let info = info.get("channel").cloned().unwrap_or(json!({}));
        if !(truthy(&info, "is_im") || truthy(&info, "is_mpim")) {
            return Ok(channel);
        }
        if !self.someone_wrote(&channel).await? {
            return Err(VerbError::new(
                "FirstContactRefused",
                format!("slack {channel}: nobody has written to Opal here yet, and Opal never messages a person first"),
            ));
        }
        if let Ok(mut s) = self.spoken_to.lock() {
            s.insert(channel.clone());
        }
        Ok(channel)
    }

    async fn someone_wrote(&self, channel: &str) -> Result<bool> {
        if self.hooks.history.has_inbound(channel).await {
            return Ok(true);
        }
        let msgs = self.api.paged("conversations.history", "messages", Some(1000), &[("channel", channel.into())]).await.map_err(api_err)?;
        Ok(msgs.iter().any(|m| posted(m) && !self.is_own(m) && !s(m, "user").is_empty()))
    }

    async fn upload(&self, channel: &str, text: &str, thread: Option<&str>, files: &[OutFile]) -> Result<Sent> {
        let scope = |e: ApiError| {
            if e.error == "missing_scope" {
                runtime(format!("slack account {} cannot upload until its app holds the files:write scope", repr(&self.cfg.name)))
            } else {
                e.verb()
            }
        };
        let mut shared = vec![];
        for f in files {
            let data = f.bytes()?;
            let r = self.api.call("files.getUploadURLExternal", &[("filename", f.filename.clone()), ("length", data.len().to_string())]).await.map_err(scope)?;
            let up = self.api.http().post(s(&r, "upload_url")).body(data).send().await.map_err(|e| runtime(format!("slack upload of {}: {}", repr(&f.filename), e.without_url())))?;
            if up.status().as_u16() != 200 {
                return Err(runtime(format!("slack upload of {}: http {}", repr(&f.filename), up.status().as_u16())));
            }
            shared.push(json!({"id": s(&r, "file_id"), "title": f.filename}));
        }
        let r = self
            .api
            .call(
                "files.completeUploadExternal",
                &params(&[
                    ("files", Some(Value::Array(shared).to_string())),
                    ("channel_id", Some(channel.into())),
                    ("initial_comment", Some(text.to_string()).filter(|t| !t.is_empty())),
                    ("thread_ts", thread.map(str::to_string)),
                ]),
            )
            .await
            .map_err(scope)?;
        let files: Vec<Value> = r
            .get("files")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|f| json!({"id": s(f, "id"), "title": s(f, "title"), "permalink": s(f, "permalink")}))
            .collect();
        let mut out = Sent::new();
        out.insert("message_id".into(), json!(""));
        out.insert("channel_id".into(), json!(channel));
        out.insert("ts".into(), json!(""));
        out.insert("files".into(), Value::Array(files));
        Ok(out)
    }

    async fn resolve_user_id(&self, who: &str) -> Result<String> {
        if looks_like_slack_user_id(who) {
            return Ok(who.to_string());
        }
        let needle = who.trim_start_matches('@').to_lowercase();
        for u in self.api.paged("users.list", "members", None, &[]).await.map_err(api_err)? {
            let p = u.get("profile").cloned().unwrap_or(json!({}));
            let names = [s(&u, "name"), s(&u, "real_name"), s(&p, "display_name"), s(&p, "real_name")];
            if names.iter().any(|n| !n.is_empty() && n.to_lowercase() == needle) {
                return Ok(s(&u, "id").to_string());
            }
        }
        Err(value(format!("no slack user named {}", repr(who))))
    }

    async fn channels(&self, method: &str, types: &str) -> Result<Vec<Channel>> {
        let mut out = vec![];
        for ch in self.api.paged(method, "channels", None, &[("types", types.into()), ("exclude_archived", "true".into())]).await.map_err(api_err)? {
            out.push(Channel { id: s(&ch, "id").into(), name: self.chat_name(&ch).await, kind: chat_kind(&ch).into() });
        }
        Ok(out)
    }
}

#[async_trait]
impl Connector for SlackConnector {
    fn platform(&self) -> &str {
        "slack"
    }

    fn account(&self) -> &AccountConfig {
        &self.cfg
    }

    fn keeps_history(&self) -> bool {
        true
    }

    fn sends_files(&self) -> bool {
        true
    }

    fn max_file_bytes(&self) -> u64 {
        100 * 1024 * 1024
    }

    async fn start(self: Arc<Self>) -> anyhow::Result<()> {
        if let Err(e) = self.authenticate().await {
            return if self.shutdown.closed.load(Ordering::SeqCst) { Ok(()) } else { Err(e) };
        }
        self.resync.notify_one();
        let mut tasks = Tasks(vec![tokio::spawn(self.clone().resync_loop())]);
        let Some(app_token) = self.cfg.app_token.clone().filter(|t| !t.is_empty()) else {
            tracing::warn!("slack-bot[{}] has no app_token (xapp-); send-only", self.cfg.name);
            self.live.store(true, Ordering::SeqCst);
            self.shutdown.wait().await;
            return Ok(());
        };
        let (tx, mut rx) = mpsc::unbounded_channel();
        let me = self.clone();
        tasks.0.push(tokio::spawn(async move {
            while let Some(sig) = rx.recv().await {
                match sig {
                    socket::Signal::Hello => me.resync.notify_one(),
                    socket::Signal::Event(ev) => me.handle_event(ev).await,
                }
            }
        }));
        let mut backoff = Backoff::doubling(1.0, 30.0);
        loop {
            tokio::select! {
                r = socket::run_once(&self.api, &app_token, &self.live, &tx) => match r {
                    Ok(()) => backoff.reset(),
                    Err(e) => {
                        let wait = backoff.wait();
                        tracing::warn!("slack-bot[{}] Socket Mode lost ({e}); retry in {:.1}s", self.cfg.name, wait.as_secs_f64());
                        if self.shutdown.sleep(wait).await {
                            break;
                        }
                    }
                },
                _ = self.shutdown.wait() => break,
            }
        }
        drop(tasks);
        Ok(())
    }

    async fn send(&self, channel: &str, text: &str, thread: Option<&str>, files: &[OutFile]) -> Result<Sent> {
        let channel = self.writable_id(channel).await?;
        if !files.is_empty() {
            return self.upload(&channel, text, thread, files).await;
        }
        let r = self
            .api
            .call("chat.postMessage", &params(&[("channel", Some(channel.clone())), ("text", Some(text.into())), ("thread_ts", thread.map(str::to_string))]))
            .await
            .map_err(api_err)?;
        let ch = Some(s(&r, "channel")).filter(|c| !c.is_empty()).unwrap_or(&channel).to_string();
        if let Some(m) = r.get("message").filter(|m| m.is_object()) {
            self.record(&ch, m, "", false).await;
        }
        let mut out = Sent::new();
        out.insert("message_id".into(), json!(s(&r, "ts")));
        out.insert("channel_id".into(), json!(ch));
        out.insert("ts".into(), json!(s(&r, "ts")));
        Ok(out)
    }

    async fn fetch(&self, channel: &str, limit: usize, before: Option<&str>) -> Result<Vec<Inbound>> {
        let r = self
            .api
            .call("conversations.history", &params(&[("channel", Some(channel.into())), ("limit", Some(limit.to_string())), ("latest", before.map(str::to_string))]))
            .await
            .map_err(api_err)?;
        let mut out = vec![];
        for m in r.get("messages").and_then(Value::as_array).into_iter().flatten().filter(|m| posted(m)) {
            out.push(self.inbound(channel, m, "").await);
        }
        out.reverse(); // Slack returns newest first
        Ok(out)
    }

    async fn download_attachments(&self, channel: &str, message_id: &str, idx: Option<i64>) -> Result<Vec<Downloaded>> {
        let r = self
            .api
            .call("conversations.history", &[("channel", channel.into()), ("latest", message_id.into()), ("inclusive", "true".into()), ("limit", "1".into())])
            .await
            .map_err(api_err)?;
        let Some(m) = r.get("messages").and_then(Value::as_array).into_iter().flatten().find(|m| s(m, "ts") == message_id) else {
            return Err(value(format!("message {message_id} not found in {channel}")));
        };
        let mut out = vec![];
        for a in slack_attachments(m) {
            if idx.is_some_and(|i| i != a.idx) {
                continue;
            }
            let resp = self.api.get_file(&a.url).await.map_err(|e| runtime(format!("slack file GET: {}", e.without_url())))?;
            if !resp.status().is_success() {
                return Err(runtime(format!("slack file GET {}", resp.status().as_u16())));
            }
            let data = resp.bytes().await.map_err(|e| runtime(format!("slack file GET: {}", e.without_url())))?;
            out.push(Downloaded { filename: a.filename, mime: a.mime, data: data.to_vec() });
        }
        Ok(out)
    }

    async fn react(&self, channel: &str, message_id: &str, emoji: &str, remove: bool) -> Result<Value> {
        let name = emoji::to_name(emoji);
        let method = if remove { "reactions.remove" } else { "reactions.add" };
        let error = match self.api.call(method, &[("channel", channel.into()), ("timestamp", message_id.into()), ("name", name.clone())]).await {
            Ok(_) => String::new(),
            Err(e) => e.error,
        };
        slack_reaction(&self.cfg.name, channel, message_id, &name, remove, &error)
    }

    async fn list_channels(&self, include_dms: bool) -> Result<Vec<Channel>> {
        let types = if include_dms { "public_channel,private_channel,im,mpim" } else { "public_channel,private_channel" };
        self.channels("conversations.list", types).await
    }

    async fn list_seats(&self) -> Result<Vec<Channel>> {
        self.channels("users.conversations", MEMBER_TYPES).await
    }

    async fn open_dm(&self, user: &str) -> Result<Channel> {
        let uid = self.resolve_user_id(user).await?;
        let r = self.api.call("conversations.open", &[("users", uid.clone())]).await.map_err(api_err)?;
        Ok(Channel { id: r.pointer("/channel/id").and_then(Value::as_str).unwrap_or("").into(), name: self.api.person(&uid).await.name, kind: "dm".into() })
    }

    async fn identity(&self) -> Result<Identity> {
        let me = self.me();
        if !me.user.is_empty() {
            return Ok(Identity { id: me.user, name: me.name });
        }
        let auth = self.api.call("auth.test", &[]).await.map_err(api_err)?;
        if let Ok(mut m) = self.me.lock() {
            m.user = s(&auth, "user_id").into();
            m.name = s(&auth, "user").into();
        }
        Ok(Identity { id: s(&auth, "user_id").into(), name: s(&auth, "user").into() })
    }

    async fn writable(&self, channel: &str) -> Result<bool> {
        match self.writable_id(channel).await {
            Ok(_) => Ok(true),
            Err(e) if e.error_class == "FirstContactRefused" => Ok(false),
            Err(e) => Err(e),
        }
    }

    fn live(&self) -> bool {
        self.live.load(Ordering::SeqCst)
    }

    async fn close(&self) {
        self.shutdown.close();
        self.live.store(false, Ordering::SeqCst);
    }
}
