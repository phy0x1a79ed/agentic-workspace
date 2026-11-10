//! The Discord connector: one bot account over twilight (`discord_conn.py`).
//!
//! Bot token only: a user token is a self-bot and bannable, so that path does not exist. The
//! gateway holds the privileged `message_content` intent, which the developer portal must grant
//! too, and reconnects on its own. A DM is reached as `dm:<user_id>`. The bot never writes first:
//! `send` refuses a DM where nobody but the bot has written. `/approve` is the bot's one slash
//! command, offered only in its DM and only to an operator.
//!
//! `AWM_DISCORD_API` (`http://host:port`) points the REST client at a stand-in, and
//! `AWM_DISCORD_GATEWAY` (`ws://host:port`) the gateway.

mod events;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::VerbError;
use serde_json::{json, Value};
use tokio::sync::{mpsc, watch};
use twilight_gateway::{ConfigBuilder, Event, EventTypeFlags, Intents, Shard, ShardId, StreamExt as _};
use twilight_http::api_error::ApiError;
use twilight_http::error::ErrorType;
use twilight_http::request::channel::reaction::RequestReactionType;
use twilight_http::request::Request;
use twilight_http::response::marker::EmptyBody;
use twilight_http::routing::Route;
use twilight_model::application::interaction::Interaction;
use twilight_model::channel::message::MessageFlags;
use twilight_model::channel::{ChannelType, Message};
use twilight_model::http::attachment::Attachment as OutAttachment;
use twilight_model::http::interaction::{InteractionResponse, InteractionResponseType};
use twilight_model::id::Id;
use twilight_util::builder::InteractionResponseDataBuilder;

use self::events::{approve_of, approve_payload, inbound, verdict, Cache, Next, APPROVE_COMMAND, DM_PREFIX};
use super::{emoji, permission, runtime, value, Channel, Command, Connector, Downloaded, Hooks, Identity, Inbound, OutFile, Result, Sent};
use crate::config::AccountConfig;

const READY_WAIT: Duration = Duration::from_secs(30);
const PAGE: u16 = 100;
/// How far back the first-contact guard looks for someone else's message.
const FIRST_CONTACT_SCAN: usize = 1000;

/// One bot account's gateway and REST client.
pub struct DiscordConnector {
    cfg: AccountConfig,
    hooks: Hooks,
    http: twilight_http::Client,
    cdn: reqwest::Client,
    gateway: Option<String>,
    cache: Mutex<Cache>,
    ready: watch::Sender<bool>,
    started: AtomicBool,
    connected: AtomicBool,
}

impl DiscordConnector {
    /// Build the connector. Nothing connects until `start`.
    pub fn create(cfg: AccountConfig, hooks: Hooks) -> anyhow::Result<Arc<dyn Connector>> {
        let api = std::env::var("AWM_DISCORD_API").ok().filter(|s| !s.trim().is_empty());
        let gateway = std::env::var("AWM_DISCORD_GATEWAY").ok().filter(|s| !s.trim().is_empty());
        Ok(Arc::new(Self::build(cfg, hooks, api.as_deref(), gateway, true)?))
    }

    fn build(cfg: AccountConfig, hooks: Hooks, api: Option<&str>, gateway: Option<String>, ratelimit: bool) -> anyhow::Result<Self> {
        if cfg.token.is_empty() {
            anyhow::bail!("discord account {:?} has no bot token", cfg.name);
        }
        // The build links both ring and aws-lc-rs, so rustls has no default provider until one
        // is installed, and twilight's REST and gateway clients panic without it.
        let _ = tokio_rustls::rustls::crypto::ring::default_provider().install_default();
        let mut b = twilight_http::Client::builder().token(cfg.token.clone());
        if let Some(api) = api {
            let api = api.trim().trim_end_matches('/');
            b = match api.strip_prefix("http://") {
                Some(host) => b.proxy(host.to_string(), true),
                None => b.proxy(api.strip_prefix("https://").unwrap_or(api).to_string(), false),
            };
        }
        if !ratelimit {
            b = b.ratelimiter(None);
        }
        Ok(Self {
            cfg,
            hooks,
            http: b.build(),
            cdn: reqwest::Client::builder().timeout(Duration::from_secs(60)).build()?,
            gateway,
            cache: Mutex::new(Cache::default()),
            ready: watch::channel(false).0,
            started: AtomicBool::new(false),
            connected: AtomicBool::new(false),
        })
    }

    fn cache(&self) -> MutexGuard<'_, Cache> {
        self.cache.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn name(&self) -> &str {
        &self.cfg.name
    }

    async fn ensure(&self) -> Result<()> {
        if !self.started.load(Ordering::SeqCst) {
            return Err(runtime(format!("discord[{}] not started", self.name())));
        }
        let mut rx = self.ready.subscribe();
        let up = matches!(tokio::time::timeout(READY_WAIT, rx.wait_for(|r| *r)).await, Ok(Ok(_)));
        if up {
            Ok(())
        } else {
            Err(runtime(format!("discord[{}] not connected", self.name())))
        }
    }

    /// The DM channel with a user, opened once and then cached.
    async fn dm_channel(&self, user: &str) -> Result<u64> {
        let uid = snowflake(user)?;
        if let Some(ch) = self.cache().dm_of.get(&uid) {
            return Ok(*ch);
        }
        let ch = self.http.create_private_channel(Id::new(uid)).await.map_err(http_err)?.model().await.map_err(|e| runtime(e.to_string()))?;
        let mut c = self.cache();
        c.put_channel(&ch);
        c.dm_of.insert(uid, ch.id.get());
        Ok(ch.id.get())
    }

    /// A channel id, or `dm:<user_id>` for the DM with that user, and the channel's type.
    async fn resolve(&self, channel: &str) -> Result<(u64, ChannelType)> {
        if let Some(user) = channel.strip_prefix(DM_PREFIX) {
            return Ok((self.dm_channel(user).await?, ChannelType::Private));
        }
        let id = snowflake(channel)?;
        if let Some(c) = self.cache().chans.get(&id) {
            return Ok((id, c.kind));
        }
        let ch = self.http.channel(Id::new(id)).await.map_err(http_err)?.model().await.map_err(|e| runtime(e.to_string()))?;
        self.cache().put_channel(&ch);
        Ok((id, ch.kind))
    }

    async fn page(&self, channel: u64, limit: u16, before: Option<u64>) -> Result<Vec<Message>> {
        let req = self.http.channel_messages(Id::new(channel));
        let resp = match before {
            Some(b) => req.before(Id::new(b)).limit(limit).await,
            None => req.limit(limit).await,
        };
        resp.map_err(http_err)?.models().await.map_err(|e| runtime(e.to_string()))
    }

    /// Whether someone other than us has written in this DM: the cache, else its recent history.
    async fn spoken_in(&self, channel: u64) -> Result<bool> {
        if self.cache().spoken.contains(&channel) {
            return Ok(true);
        }
        let (mut before, mut seen) = (None, 0);
        while seen < FIRST_CONTACT_SCAN {
            let page = self.page(channel, PAGE.min((FIRST_CONTACT_SCAN - seen) as u16), before).await?;
            seen += page.len();
            let found = {
                let c = self.cache();
                page.iter().any(|m| !m.author.bot && !c.is_me(m.author.id.get()))
            };
            if found {
                self.cache().spoken.insert(channel);
                return Ok(true);
            }
            match page.last() {
                Some(m) if page.len() == PAGE as usize => before = Some(m.id.get()),
                _ => break,
            }
        }
        Ok(false)
    }

    fn first_contact(&self, channel: u64) -> VerbError {
        permission(format!("discord {channel}: nobody has written to Opal here yet, and Opal never messages a person first"))
    }

    /// A fresh session replays nothing, so fetch what each seat and mounted chat got since its
    /// last message. A resume needs none of this: the gateway replays the gap itself. The first
    /// sight of a chat only sets its cursor.
    async fn catch_up(&self, tx: &mpsc::UnboundedSender<Inbound>) {
        for channel in self.hooks.db.gap_chats(self.name()).await {
            let Ok(id) = channel.parse::<u64>() else { continue };
            let cursor = self.hooks.db.cursor(self.name(), &channel).await.and_then(|c| c.parse::<u64>().ok());
            let page = match cursor {
                Some(after) => self.http.channel_messages(Id::new(id)).after(Id::new(after)).limit(PAGE).await.map_err(http_err),
                None => self.http.channel_messages(Id::new(id)).limit(1).await.map_err(http_err),
            };
            let mut page = match page {
                Ok(r) => r.models().await.unwrap_or_default(),
                Err(e) => {
                    tracing::warn!(account = %self.name(), "discord resync of {channel} failed: {}", e.message);
                    continue;
                }
            };
            page.sort_by_key(|m| m.id.get());
            if let Some(newest) = page.last() {
                self.hooks.db.set_cursor(self.name(), &channel, &newest.id.get().to_string()).await;
            }
            if cursor.is_none() {
                continue;
            }
            for m in &page {
                let found = self.cache().message(self.name(), m);
                if let Some(mut msg) = found {
                    msg.resynced = true;
                    let _ = tx.send(msg);
                }
            }
        }
    }

    async fn register(&self, app: u64) {
        let body = serde_json::to_vec(&approve_payload()).unwrap_or_default();
        let req = match Request::builder(&Route::SetGlobalCommands { application_id: app }).body(body).build() {
            Ok(r) => r,
            Err(e) => return tracing::warn!(account = %self.name(), "discord command sync failed: {e}"),
        };
        match self.http.request::<EmptyBody>(req).await {
            Ok(_) => tracing::info!(account = %self.name(), "discord synced 1 app command"),
            Err(e) => tracing::warn!(account = %self.name(), "discord command sync failed: {e}"),
        }
    }

    /// Gate and ack one `/approve`, then hand it to `on_command`. The ack goes out before the
    /// command, inside Discord's 3-second deadline.
    async fn on_interaction(&self, i: Interaction) {
        let Some(inv) = approve_of(&i) else { return };
        let operator = if inv.in_guild { None } else { self.hooks.db.lookup("discord", &inv.user_id).await };
        let v = verdict(&inv, operator);
        if !inv.in_guild && v.operator.is_none() {
            tracing::warn!(account = %self.name(), user = %inv.user_id, "discord refused /{APPROVE_COMMAND} from a non-operator");
        }
        let mut flags = MessageFlags::SUPPRESS_EMBEDS;
        if v.ephemeral {
            flags |= MessageFlags::EPHEMERAL;
        }
        let resp = InteractionResponse {
            kind: InteractionResponseType::ChannelMessageWithSource,
            data: Some(InteractionResponseDataBuilder::new().content(v.reply.clone()).flags(flags).build()),
        };
        if let Err(e) = self.http.interaction(i.application_id).create_response(i.id, &i.token, &resp).await {
            tracing::warn!(account = %self.name(), "discord /{APPROVE_COMMAND} reply failed: {e}");
        }
        let (Some(operator), Some(on_command)) = (v.operator, self.hooks.on_command.as_ref()) else { return };
        let mut options = serde_json::Map::new();
        options.insert("device".into(), json!(inv.device));
        on_command(Command {
            account: self.name().to_string(),
            platform: "discord".into(),
            name: APPROVE_COMMAND.into(),
            options,
            user_id: inv.user_id,
            user_name: inv.user_name,
            channel_id: inv.channel_id,
            is_dm: true,
            operator,
        })
        .await;
    }

    fn mark_ready(&self) {
        self.connected.store(true, Ordering::SeqCst);
        self.ready.send_replace(true);
    }
}

fn snowflake(s: &str) -> Result<u64> {
    s.trim().parse::<u64>().ok().filter(|n| *n != 0).ok_or_else(|| value(format!("invalid literal for int() with base 10: '{s}'")))
}

fn reason(status: u16) -> &'static str {
    match status {
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        _ => "",
    }
}

/// discord.py's `HTTPException.text`: the API's message, or the raw body.
fn api_text(error: &ApiError, body: &[u8]) -> (u64, String) {
    match error {
        ApiError::General(g) => (g.code, g.message.clone()),
        _ => (0, String::from_utf8_lossy(body).into_owned()),
    }
}

/// A twilight error as the discord.py exception the Python raised: `Forbidden`, `NotFound`,
/// `DiscordServerError` or `HTTPException`, with discord.py's text.
fn http_err(e: twilight_http::Error) -> VerbError {
    match e.kind() {
        ErrorType::Response { body, error, status } => {
            let s = status.get();
            let (code, text) = api_text(error, body);
            let class = match s {
                403 => "Forbidden",
                404 => "NotFound",
                500.. => "DiscordServerError",
                _ => "HTTPException",
            };
            VerbError::new(class, format!("{s} {} (error code: {code}): {text}", reason(s)))
        }
        ErrorType::Validation => value(format!("discord: {e}")),
        _ => runtime(format!("discord: {e}")),
    }
}

/// A custom emoji as `<:name:id>`, `<a:name:id>` or `name:id`; anything else is unicode.
fn reaction_type(e: &str) -> RequestReactionType<'_> {
    let inner = e.strip_prefix('<').and_then(|s| s.strip_suffix('>')).map(|s| s.strip_prefix("a:").or_else(|| s.strip_prefix(':')).unwrap_or(s)).unwrap_or(e);
    if let Some((name, id)) = inner.rsplit_once(':') {
        if let Some(id) = id.parse::<u64>().ok().and_then(Id::new_checked) {
            return RequestReactionType::Custom { id, name: Some(name).filter(|n| !n.is_empty()) };
        }
    }
    RequestReactionType::Unicode { name: e }
}

#[async_trait]
impl Connector for DiscordConnector {
    fn platform(&self) -> &str {
        "discord"
    }

    fn account(&self) -> &AccountConfig {
        &self.cfg
    }

    fn sends_files(&self) -> bool {
        true
    }

    fn max_file_bytes(&self) -> u64 {
        10 * 1024 * 1024
    }

    async fn start(self: Arc<Self>) -> anyhow::Result<()> {
        self.started.store(true, Ordering::SeqCst);
        let intents = Intents::GUILDS | Intents::GUILD_MESSAGES | Intents::DIRECT_MESSAGES | Intents::MESSAGE_CONTENT;
        let mut config = ConfigBuilder::new(self.cfg.token.clone(), intents);
        if let Some(g) = &self.gateway {
            config = config.proxy_url(g.clone());
        }
        let mut shard = Shard::with_config(ShardId::ONE, config.build());
        // One consumer keeps the live feed in order, so a slow hook never delays an interaction.
        let (tx, mut rx) = mpsc::unbounded_channel::<Inbound>();
        let on_message = self.hooks.on_message.clone();
        tokio::spawn(async move {
            while let Some(m) = rx.recv().await {
                on_message(m).await;
            }
        });
        let wanted = EventTypeFlags::READY
            | EventTypeFlags::RESUMED
            | EventTypeFlags::GUILD_CREATE
            | EventTypeFlags::GUILD_UPDATE
            | EventTypeFlags::GUILD_DELETE
            | EventTypeFlags::CHANNEL_CREATE
            | EventTypeFlags::CHANNEL_UPDATE
            | EventTypeFlags::CHANNEL_DELETE
            | EventTypeFlags::THREAD_CREATE
            | EventTypeFlags::THREAD_UPDATE
            | EventTypeFlags::THREAD_DELETE
            | EventTypeFlags::MESSAGE_CREATE
            | EventTypeFlags::INTERACTION_CREATE;
        let mut closed = String::from("the gateway closed");
        while let Some(item) = shard.next_event(wanted).await {
            let event = match item {
                Ok(e) => e,
                Err(e) => {
                    tracing::warn!(account = %self.name(), "discord gateway receive failed: {e}");
                    continue;
                }
            };
            match &event {
                Event::GatewayClose(frame) => {
                    self.connected.store(false, Ordering::SeqCst);
                    if let Some(f) = frame {
                        closed = format!("close code {} {}", f.code, f.reason);
                    }
                    continue;
                }
                Event::Resumed => self.mark_ready(),
                _ => {}
            }
            let next = self.cache().apply(&self.cfg.name, &event);
            match next {
                Next::Ready(app) => {
                    self.mark_ready();
                    tracing::info!(account = %self.name(), "discord ready");
                    let me = self.clone();
                    let gap = tx.clone();
                    tokio::spawn(async move {
                        me.catch_up(&gap).await;
                        me.register(app).await;
                    });
                }
                Next::Message(m) => {
                    self.hooks.db.set_cursor(self.name(), &m.channel_id, &m.message_id).await;
                    let _ = tx.send(*m);
                }
                Next::Interaction(i) => {
                    let me = self.clone();
                    tokio::spawn(async move { me.on_interaction(*i).await });
                }
                Next::Nothing => {}
            }
        }
        self.connected.store(false, Ordering::SeqCst);
        anyhow::bail!("discord[{}] connection ended: {closed}", self.name())
    }

    async fn send(&self, channel: &str, text: &str, thread: Option<&str>, files: &[OutFile]) -> Result<Sent> {
        self.ensure().await?;
        let target = thread.filter(|t| !t.is_empty()).unwrap_or(channel);
        let (id, kind) = self.resolve(target).await?;
        if matches!(kind, ChannelType::Private | ChannelType::Group) && !self.spoken_in(id).await? {
            return Err(self.first_contact(id));
        }
        // A DM is a notification feed: link previews there are noise.
        let dm = channel.starts_with(DM_PREFIX) || kind == ChannelType::Private;
        let atts = files.iter().enumerate().map(|(i, f)| Ok(OutAttachment::from_bytes(f.filename.clone(), f.bytes()?, i as u64))).collect::<Result<Vec<_>>>()?;
        let mut req = self.http.create_message(Id::new(id));
        if !text.is_empty() {
            req = req.content(text);
        }
        if !atts.is_empty() {
            req = req.attachments(&atts);
        }
        if dm {
            req = req.flags(MessageFlags::SUPPRESS_EMBEDS);
        }
        let sent = req.await.map_err(http_err)?.model().await.map_err(|e| runtime(e.to_string()))?;
        let mut out = Sent::new();
        out.insert("message_id".into(), json!(sent.id.get().to_string()));
        out.insert("channel_id".into(), json!(sent.channel_id.get().to_string()));
        out.insert("ts".into(), json!(events::snowflake_iso(sent.id.get())));
        Ok(out)
    }

    async fn fetch(&self, channel: &str, limit: usize, before: Option<&str>) -> Result<Vec<Inbound>> {
        self.ensure().await?;
        let (id, _) = self.resolve(channel).await?;
        let mut before = before.filter(|b| !b.is_empty()).map(snowflake).transpose()?;
        let place = {
            let c = self.cache();
            c.place(id, c.chans.get(&id).and_then(|c| c.guild))
        };
        let mut out = vec![];
        while out.len() < limit {
            let want = (limit - out.len()).min(PAGE as usize) as u16;
            let page = self.page(id, want, before).await?;
            let full = page.len() == want as usize;
            before = page.last().map(|m| m.id.get());
            out.extend(page.iter().map(|m| inbound(&self.cfg.name, m, &place, true)));
            if !full {
                break;
            }
        }
        out.reverse();
        Ok(out)
    }

    async fn download_attachments(&self, channel: &str, message_id: &str, idx: Option<i64>) -> Result<Vec<Downloaded>> {
        self.ensure().await?;
        let (id, _) = self.resolve(channel).await?;
        let m = self.http.message(Id::new(id), Id::new(snowflake(message_id)?)).await.map_err(http_err)?.model().await.map_err(|e| runtime(e.to_string()))?;
        let mut out = vec![];
        for (i, a) in m.attachments.iter().enumerate() {
            if idx.is_some_and(|x| x != i as i64) {
                continue;
            }
            let resp = self.cdn.get(&a.url).send().await.map_err(|e| runtime(format!("discord attachment fetch failed: {e}")))?;
            let s = resp.status().as_u16();
            if !resp.status().is_success() {
                let (class, text) = match s {
                    404 => ("NotFound", "asset not found"),
                    403 => ("Forbidden", "cannot retrieve asset"),
                    _ => ("HTTPException", "failed to get asset"),
                };
                return Err(VerbError::new(class, format!("{s} {} (error code: 0): {text}", reason(s))));
            }
            let data = resp.bytes().await.map_err(|e| runtime(format!("discord attachment fetch failed: {e}")))?.to_vec();
            let filename = if a.filename.is_empty() { format!("attachment-{i}") } else { a.filename.clone() };
            out.push(Downloaded { filename, mime: a.content_type.clone().unwrap_or_default(), data });
        }
        Ok(out)
    }

    async fn react(&self, channel: &str, message_id: &str, emoji: &str, remove: bool) -> Result<Value> {
        self.ensure().await?;
        let ch = emoji::to_char(emoji).unwrap_or_else(|| emoji.trim().to_string());
        let (id, _) = self.resolve(channel).await?;
        let mid = snowflake(message_id)?;
        let r = reaction_type(&ch);
        let result = if remove {
            self.http.delete_current_user_reaction(Id::new(id), Id::new(mid), &r).await.map(drop)
        } else {
            self.http.create_reaction(Id::new(id), Id::new(mid), &r).await.map(drop)
        };
        if let Err(e) = result {
            return Err(match e.kind() {
                ErrorType::Response { body, error, status } if status.get() == 403 => {
                    runtime(format!("discord[{}] may not react in {channel}: {}", self.name(), api_text(error, body).1))
                }
                ErrorType::Response { body, error, .. } => runtime(format!("discord[{}] rejected '{ch}': {}", self.name(), api_text(error, body).1)),
                _ => runtime(format!("discord[{}] rejected '{ch}': {e}", self.name())),
            });
        }
        Ok(json!({"as": "reaction", "emoji": ch, "removed": remove, "message_id": mid.to_string(), "channel_id": id.to_string()}))
    }

    async fn list_channels(&self, include_dms: bool) -> Result<Vec<Channel>> {
        self.ensure().await?;
        Ok(self.cache().channels(include_dms))
    }

    /// An operator's DM is a seat: its plain messages reach the holder, while slash commands and
    /// the bot's own notices never do.
    async fn list_seats(&self) -> Result<Vec<Channel>> {
        let mut seats = self.list_channels(false).await?;
        let operators = self.hooks.db.list_operators(Some("discord".into())).await.map_err(|e| runtime(e.to_string()))?;
        for row in operators {
            let uid = row.get("platform_user_id").and_then(Value::as_str).unwrap_or("").to_string();
            match self.open_dm(&uid).await {
                Ok(c) => seats.push(c),
                Err(e) => tracing::info!(account = %self.name(), user = %uid, "discord cannot open an operator's DM: {e}"),
            }
        }
        Ok(seats)
    }

    async fn open_dm(&self, user: &str) -> Result<Channel> {
        self.ensure().await?;
        let uid = user.strip_prefix(DM_PREFIX).unwrap_or(user);
        let id = self.dm_channel(uid).await?;
        let name = self.cache().chans.get(&id).and_then(|c| c.recipient.as_ref().map(|r| r.1.clone())).unwrap_or_else(|| format!("{DM_PREFIX}{uid}"));
        Ok(Channel { id: id.to_string(), name, kind: "dm".into() })
    }

    async fn identity(&self) -> Result<Identity> {
        self.ensure().await?;
        let me = self.cache().me.clone();
        Ok(me.map(|(id, name)| Identity { id: id.to_string(), name }).unwrap_or_default())
    }

    async fn writable(&self, channel: &str) -> Result<bool> {
        self.ensure().await?;
        let (id, kind) = self.resolve(channel).await?;
        if !matches!(kind, ChannelType::Private | ChannelType::Group) {
            return Ok(true);
        }
        self.spoken_in(id).await
    }

    fn live(&self) -> bool {
        self.connected.load(Ordering::SeqCst)
    }

    async fn close(&self) {
        self.connected.store(false, Ordering::SeqCst);
    }
}

#[cfg(test)]
mod tests;
