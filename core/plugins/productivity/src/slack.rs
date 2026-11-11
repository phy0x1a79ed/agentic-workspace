//! Tony's own Slack account, read only (`slack_conn.py`). There is no send and no react.
//!
//! API mode takes an `xoxp-` token. Session mode acts as the web client does: an `xoxc-` token
//! with the `d` cookie, or a `creds_cmd` that prints both. Session creds are short-lived, so the
//! client runs `creds_cmd` again on a stale-credential error.

use async_trait::async_trait;
use awm_chat::slack::api::{params, Client};
use awm_chat::slack::{api_err, attachments, chat_kind, repr, s};
use awm_chat::blocks::slack_text;
use awm_chat::{runtime, Channel, Downloaded, Identity, Inbound, Result};
use serde_json::{json, Value};

use crate::config::AccountConfig;
use crate::Account;

const CONV_TYPES: &str = "im,mpim,private_channel,public_channel";

pub struct SlackUser {
    cfg: AccountConfig,
    api: Client,
}

impl SlackUser {
    pub fn new(cfg: AccountConfig, base: &str) -> Self {
        let session = cfg.creds_cmd.is_some() || cfg.token.starts_with("xoxc-");
        let cookie = if session { cfg.cookie.clone().unwrap_or_default() } else { String::new() };
        Self { api: Client::new(base, &cfg.token, &cookie, cfg.creds_cmd.clone()), cfg }
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
            attachments: attachments(m),
            to_holder: true,
            ..Default::default()
        }
    }

}

#[async_trait]
impl Account for SlackUser {
    fn platform(&self) -> &str {
        "slack"
    }

    async fn probe(&self) -> Result<()> {
        self.identity().await.map(|_| ())
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
                attachments: attachments(m),
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

    async fn identity(&self) -> Result<Identity> {
        let auth = self.api.call("auth.test", &[]).await.map_err(api_err)?;
        Ok(Identity { id: s(&auth, "user_id").into(), name: s(&auth, "user").into() })
    }
}
