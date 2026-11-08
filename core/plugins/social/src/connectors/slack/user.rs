//! Tony's own Slack account, read only (`slack_conn.py`): it has no send and no react, since awm
//! never writes to Slack as Tony.
//!
//! API mode (an `xoxp-` token) receives through Socket Mode, which needs an `xapp-` app token.
//! Session mode (an `xoxc-` token with the `d` cookie, or a `creds_cmd` that prints both) acts as
//! the web client does: Socket Mode is app-scoped, so receive polls `conversations.history`
//! every [`POLL_S`], baseline first and then the delta. A chat seen for the first time is
//! baselined to its latest message and never replayed. The creds are short-lived, so
//! `creds_cmd` runs at start and again on a stale-credential error.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::Backoff;
use serde_json::{json, Value};
use tokio::sync::mpsc;

use super::api::{params, ApiError, Client};
use super::{api_err, chat_kind, kind_of, kind_of_type, repr, s, socket, ts_f, Shutdown, Tasks};
use crate::config::AccountConfig;
use crate::connectors::blocks::slack_text;
use crate::connectors::{runtime, slack_attachments, looks_like_slack_user_id, Channel, ChatKind, Connector, Downloaded, Hooks, Identity, Inbound, Result};

pub const POLL_S: f64 = 20.0;
const CONV_TYPES: &str = "im,mpim,private_channel,public_channel";
const HISTORY_LIMIT: usize = 50;

pub struct SlackUser {
    cfg: AccountConfig,
    hooks: Hooks,
    pub(super) api: Client,
    pub(super) session: bool,
    pub(super) self_id: std::sync::Mutex<String>,
    team: std::sync::Mutex<String>,
    kinds: std::sync::Mutex<HashMap<String, ChatKind>>,
    live: AtomicBool,
    shutdown: Shutdown,
}

impl SlackUser {
    pub fn new(cfg: AccountConfig, hooks: Hooks, base: &str) -> Self {
        let session = cfg.creds_cmd.as_deref().is_some_and(|c| !c.is_empty()) || cfg.token.starts_with("xoxc-");
        let cookie = if session { cfg.cookie.clone().unwrap_or_default() } else { String::new() };
        Self {
            api: Client::new(base, &cfg.token, &cookie, cfg.creds_cmd.clone()),
            session,
            cfg,
            hooks,
            self_id: Default::default(),
            team: Default::default(),
            kinds: Default::default(),
            live: AtomicBool::new(false),
            shutdown: Shutdown::default(),
        }
    }

    fn self_id(&self) -> String {
        self.self_id.lock().map(|s| s.clone()).unwrap_or_default()
    }

    fn is_own(&self, m: &Value) -> bool {
        let me = self.self_id();
        !me.is_empty() && s(m, "user") == me
    }

    fn inbound(&self, cid: &str, m: &Value) -> Inbound {
        let sender = s(m, "user");
        Inbound {
            account: self.cfg.name.clone(),
            platform: "slack".into(),
            channel_id: cid.into(),
            text: slack_text(m),
            sender_id: sender.into(),
            sender_name: sender.into(),
            thread_id: s(m, "thread_ts").into(),
            message_id: s(m, "ts").into(),
            ts: s(m, "ts").into(),
            attachments: slack_attachments(m),
            to_holder: true,
            ..Default::default()
        }
    }

    /// Fill what a seat needs, then hand the message on.
    async fn emit(&self, mut msg: Inbound, kind: Option<ChatKind>) {
        msg.kind = kind.or_else(|| self.kinds.lock().ok().and_then(|k| k.get(&msg.channel_id).copied())).unwrap_or_default();
        msg.server = if msg.kind == ChatKind::Dm { String::new() } else { self.team.lock().map(|t| t.clone()).unwrap_or_default() };
        msg.outside = !msg.sender_id.is_empty() && self.api.person(&msg.sender_id).await.outside;
        (self.hooks.on_message)(msg).await;
    }

    async fn handle_event(&self, event: Value) {
        if s(&event, "type") != "message" || !s(&event, "subtype").is_empty() || self.is_own(&event) {
            return;
        }
        let msg = self.inbound(s(&event, "channel"), &event);
        self.emit(msg, kind_of_type(s(&event, "channel_type"))).await;
    }

    async fn conversations(&self) -> std::result::Result<Vec<Value>, ApiError> {
        let r = self.api.call("users.conversations", &[("types", CONV_TYPES.into()), ("exclude_archived", "true".into()), ("limit", "200".into())]).await?;
        Ok(r.get("channels").and_then(Value::as_array).cloned().unwrap_or_default())
    }

    /// One poll. `baseline` maps a chat to its last-seen ts; only strictly newer messages on a
    /// chat already known are handed on.
    pub(super) async fn poll_once(&self, baseline: &mut HashMap<String, String>, first: bool) -> std::result::Result<(), ApiError> {
        let convs = self.conversations().await?;
        if first {
            tracing::info!("slack[{}] session receive: polling {} conversations every {POLL_S:.0}s", self.cfg.name, convs.len());
        }
        if let Ok(mut k) = self.kinds.lock() {
            k.extend(convs.iter().map(|c| (s(c, "id").to_string(), kind_of(c))));
        }
        for c in &convs {
            let cid = s(c, "id");
            if cid.is_empty() {
                continue;
            }
            let Some(base) = baseline.get(cid).cloned() else {
                let r = self.api.call("conversations.history", &[("channel", cid.into()), ("limit", "1".into())]).await?;
                let latest = r.pointer("/messages/0/ts").and_then(Value::as_str).map(str::to_string);
                baseline.insert(cid.into(), latest.unwrap_or_else(|| format!("{:.6}", crate::db::now())));
                continue;
            };
            let r = self.api.call("conversations.history", &[("channel", cid.into()), ("oldest", base.clone()), ("limit", HISTORY_LIMIT.to_string())]).await?;
            let mut newest = base.clone();
            for m in r.get("messages").and_then(Value::as_array).into_iter().flatten().rev() {
                let ts = s(m, "ts");
                if ts.is_empty() || ts_f(ts) <= ts_f(&base) {
                    continue; // `oldest` is inclusive here: drop the boundary
                }
                if ts_f(ts) > ts_f(&newest) {
                    newest = ts.to_string();
                }
                if s(m, "subtype").is_empty() && !self.is_own(m) {
                    self.emit(self.inbound(cid, m), Some(kind_of(c))).await;
                }
            }
            baseline.insert(cid.into(), newest);
        }
        Ok(())
    }

    async fn run_poll(&self) {
        let mut baseline = HashMap::new();
        let (mut first, mut backoff) = (true, Backoff::doubling(1.0, 60.0));
        loop {
            let wait = match self.poll_once(&mut baseline, first).await {
                Ok(()) => {
                    self.live.store(true, Ordering::SeqCst);
                    first = false;
                    backoff.reset();
                    Duration::from_secs_f64(POLL_S)
                }
                Err(e) => {
                    self.live.store(false, Ordering::SeqCst);
                    let wait = backoff.wait();
                    tracing::warn!("slack[{}] session poll error ({e}); retry in {:.1}s", self.cfg.name, wait.as_secs_f64());
                    wait
                }
            };
            if self.shutdown.sleep(wait).await {
                return;
            }
        }
    }

    async fn run_socket(self: &Arc<Self>, app_token: &str) {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let me = self.clone();
        let _tasks = Tasks(vec![tokio::spawn(async move {
            while let Some(sig) = rx.recv().await {
                if let socket::Signal::Event(ev) = sig {
                    me.handle_event(ev).await;
                }
            }
        })]);
        let mut backoff = Backoff::doubling(1.0, 30.0);
        loop {
            tokio::select! {
                r = socket::run_once(&self.api, app_token, &self.live, &tx) => match r {
                    Ok(()) => backoff.reset(),
                    Err(e) => {
                        let wait = backoff.wait();
                        tracing::warn!("slack[{}] Socket Mode lost ({e}); retry in {:.1}s", self.cfg.name, wait.as_secs_f64());
                        if self.shutdown.sleep(wait).await {
                            return;
                        }
                    }
                },
                _ = self.shutdown.wait() => return,
            }
        }
    }

    async fn resolve_user_id(&self, who: &str) -> Result<String> {
        if looks_like_slack_user_id(who) {
            return Ok(who.to_string());
        }
        let want = who.trim_start_matches('@').to_lowercase();
        let mut cursor = String::new();
        loop {
            let mut p = vec![("limit", "200".to_string())];
            if !cursor.is_empty() {
                p.push(("cursor", cursor.clone()));
            }
            let r = self.api.call("users.list", &p).await.map_err(api_err)?;
            for u in r.get("members").and_then(Value::as_array).into_iter().flatten() {
                let p = u.get("profile").cloned().unwrap_or(json!({}));
                let names = [s(u, "name"), s(u, "real_name"), s(&p, "display_name"), s(&p, "real_name")];
                if names.iter().any(|n| n.to_lowercase() == want) && !super::truthy(u, "deleted") {
                    return Ok(s(u, "id").to_string());
                }
            }
            cursor = r.pointer("/response_metadata/next_cursor").and_then(Value::as_str).unwrap_or("").to_string();
            if cursor.is_empty() {
                return Err(runtime(format!("no slack user matches {}", repr(who))));
            }
        }
    }
}

#[async_trait]
impl Connector for SlackUser {
    fn platform(&self) -> &str {
        "slack"
    }

    fn account(&self) -> &AccountConfig {
        &self.cfg
    }

    async fn start(self: Arc<Self>) -> anyhow::Result<()> {
        if self.session && self.cfg.creds_cmd.as_deref().is_some_and(|c| !c.is_empty()) {
            if let Err(e) = self.api.fetch_creds().await {
                anyhow::bail!("slack[{}] creds_cmd failed; account down: {}", self.cfg.name, e.message);
            }
        }
        let auth = match self.api.call("auth.test", &[]).await {
            Ok(a) => a,
            Err(e) => anyhow::bail!("slack[{}] auth_test failed; account down: {e}", self.cfg.name),
        };
        if let Ok(mut id) = self.self_id.lock() {
            *id = s(&auth, "user_id").into();
        }
        if let Ok(mut t) = self.api.team_id.lock() {
            *t = s(&auth, "team_id").into();
        }
        let team = self.api.team_name(s(&auth, "team")).await;
        if let Ok(mut t) = self.team.lock() {
            *t = team;
        }
        tracing::info!("slack[{}] authenticated ({} mode)", self.cfg.name, if self.session { "session" } else { "api" });
        if self.session {
            self.run_poll().await;
            return Ok(());
        }
        match self.cfg.app_token.clone().filter(|t| !t.is_empty()) {
            Some(app) => self.run_socket(&app).await,
            None => {
                tracing::warn!("slack[{}] has no app_token (xapp-); Socket Mode receive is disabled", self.cfg.name);
                self.shutdown.wait().await;
            }
        }
        Ok(())
    }

    async fn fetch(&self, channel: &str, limit: usize, before: Option<&str>) -> Result<Vec<Inbound>> {
        // `latest` is the upper bound, the one that pages backwards.
        let r = self
            .api
            .call("conversations.history", &params(&[("channel", Some(channel.into())), ("limit", Some(limit.to_string())), ("latest", before.map(str::to_string))]))
            .await
            .map_err(api_err)?;
        Ok(r.get("messages").and_then(Value::as_array).into_iter().flatten().rev().filter(|m| s(m, "subtype").is_empty()).map(|m| self.inbound(channel, m)).collect())
    }

    async fn search(&self, query: &str, limit: usize, channel: Option<&str>) -> Result<Vec<Inbound>> {
        let q = match channel {
            Some(c) if !c.is_empty() => format!("{query} in:{c}").trim().to_string(),
            _ => query.to_string(),
        };
        let r = match self.api.call("search.messages", &[("query", q), ("count", limit.to_string())]).await {
            Ok(r) => r,
            Err(e) if e.error == "not_allowed_token_type" || e.error == "missing_scope" => {
                return Err(runtime(format!(
                    "slack[{}] search needs a user token with search:read (got {}); fetch a channel directly instead",
                    self.cfg.name,
                    repr(&e.error)
                )))
            }
            Err(e) => return Err(e.verb()),
        };
        let mut out = vec![];
        for m in r.pointer("/messages/matches").and_then(Value::as_array).into_iter().flatten() {
            let ch = m.get("channel").cloned().unwrap_or(json!({}));
            let sender = Some(s(m, "user")).filter(|u| !u.is_empty()).unwrap_or(s(m, "username"));
            out.push(Inbound {
                account: self.cfg.name.clone(),
                platform: "slack".into(),
                channel_id: s(&ch, "id").into(),
                channel_name: s(&ch, "name").into(),
                text: slack_text(m),
                sender_id: s(m, "user").into(),
                sender_name: sender.into(),
                message_id: s(m, "ts").into(),
                ts: s(m, "ts").into(),
                attachments: slack_attachments(m),
                to_holder: true,
                ..Default::default()
            });
        }
        Ok(out)
    }

    async fn download_attachments(&self, channel: &str, message_id: &str, idx: Option<i64>) -> Result<Vec<Downloaded>> {
        let r = self
            .api
            .call("conversations.history", &[("channel", channel.into()), ("latest", message_id.into()), ("oldest", message_id.into()), ("inclusive", "true".into()), ("limit", "1".into())])
            .await
            .map_err(api_err)?;
        let Some(m) = r.pointer("/messages/0") else {
            return Err(runtime(format!("slack[{}] message {} not found in {}", self.cfg.name, repr(message_id), repr(channel))));
        };
        let mut out = vec![];
        for (i, f) in m.get("files").and_then(Value::as_array).into_iter().flatten().enumerate() {
            if idx.is_some_and(|x| x != i as i64) {
                continue;
            }
            let url = Some(s(f, "url_private_download")).filter(|u| !u.is_empty()).unwrap_or(s(f, "url_private"));
            if url.is_empty() {
                continue;
            }
            let fail = |e: reqwest::Error| runtime(format!("slack[{}] file GET: {}", self.cfg.name, e.without_url()));
            let resp = self.api.get_file(url).await.map_err(fail)?;
            if resp.status().as_u16() >= 400 {
                return Err(runtime(format!("slack[{}] file GET {}", self.cfg.name, resp.status().as_u16())));
            }
            let data = resp.bytes().await.map_err(fail)?;
            let filename = [s(f, "name"), s(f, "title")].into_iter().find(|n| !n.is_empty()).map(str::to_string).unwrap_or_else(|| format!("attachment-{i}"));
            out.push(Downloaded { filename, mime: s(f, "mimetype").into(), data: data.to_vec() });
        }
        Ok(out)
    }

    async fn list_channels(&self, include_dms: bool) -> Result<Vec<Channel>> {
        let r = if include_dms {
            // users.conversations, not conversations.list, returns the caller's own DMs.
            self.api.call("users.conversations", &[("types", CONV_TYPES.into()), ("exclude_archived", "true".into()), ("limit", "200".into())]).await
        } else {
            self.api.call("conversations.list", &[("types", "public_channel,private_channel".into()), ("limit", "200".into())]).await
        }
        .map_err(api_err)?;
        Ok(r.get("channels")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|ch| Channel {
                id: s(ch, "id").into(),
                // A DM has no name: its peer's user id keeps the row identifiable.
                name: Some(s(ch, "name")).filter(|n| !n.is_empty()).unwrap_or(s(ch, "user")).into(),
                kind: chat_kind(ch).into(),
            })
            .collect())
    }

    async fn open_dm(&self, user: &str) -> Result<Channel> {
        let uid = self.resolve_user_id(user).await?;
        let r = self.api.call("conversations.open", &[("users", uid)]).await.map_err(api_err)?;
        Ok(Channel { id: r.pointer("/channel/id").and_then(Value::as_str).unwrap_or("").into(), name: user.into(), kind: "dm".into() })
    }

    async fn identity(&self) -> Result<Identity> {
        let auth = self.api.call("auth.test", &[]).await.map_err(api_err)?;
        Ok(Identity { id: s(&auth, "user_id").into(), name: s(&auth, "user").into() })
    }

    fn live(&self) -> bool {
        self.live.load(Ordering::SeqCst)
    }

    async fn close(&self) {
        self.shutdown.close();
        self.live.store(false, Ordering::SeqCst);
    }
}
