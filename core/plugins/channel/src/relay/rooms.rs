//! The tether relay's bearer and room routes, as the host side uses them on loopback.
//!
//! The bearer gates `/issue` and `POST /room`, and it never leaves this host: the edge forwards
//! neither route, and a member node asks this host's `relay` for each slot (channels.md R147).
//! A room's link names the public edge, which carries `/room/<name>` beside `/tether`.

use std::time::Duration;

use serde_json::{json, Value};

pub const DEFAULT_LINK: &str = "https://nexus.tony-xy-liu.com";
pub const BEARER_ENV: &str = "AWM_TETHER_ISSUE_TOKEN";
const BASE_ENV: &str = "AWM_ROOM_RELAY";
const LINK_ENV: &str = "AWM_ROOM_LINK";
/// How one receive waits at the relay. The relay caps it at 25 s.
const WAIT_S: u64 = 25;

#[derive(Debug, Clone)]
pub enum RoomsError {
    /// The relay no longer has this room.
    Gone(String),
    /// The relay refused in a way that is not about one room.
    Refused(String),
    /// The relay could not be reached.
    Unreachable(String),
}

pub fn default_base() -> String {
    format!("http://127.0.0.1:{}", std::env::var("AWM_TETHER_PORT").ok().filter(|p| !p.trim().is_empty()).unwrap_or_else(|| "12520".into()))
}

pub struct Rooms {
    pub base: String,
    pub link_base: String,
    bearer: String,
    client: reqwest::Client,
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty())
}

impl Rooms {
    pub fn new(base: Option<String>, bearer: Option<String>, link: Option<String>) -> Self {
        let client = reqwest::Client::builder().no_proxy().connect_timeout(Duration::from_secs(10)).timeout(Duration::from_secs(WAIT_S + 15)).build().unwrap_or_default();
        Self {
            base: base.or_else(|| env(BASE_ENV)).unwrap_or_else(default_base).trim_end_matches('/').to_string(),
            link_base: link.or_else(|| env(LINK_ENV)).unwrap_or_else(|| DEFAULT_LINK.into()).trim_end_matches('/').to_string(),
            bearer: bearer.or_else(|| awm_plugin_sdk::config::secret(BEARER_ENV)).unwrap_or_default(),
            client,
        }
    }

    pub fn bearer(&self) -> Option<String> {
        Some(self.bearer.clone()).filter(|b| !b.is_empty())
    }

    pub fn link(&self, name: &str) -> String {
        format!("{}/room/{name}", self.link_base)
    }

    fn unreachable(&self, e: reqwest::Error) -> RoomsError {
        RoomsError::Unreachable(format!("the tether relay at {} is unreachable: {e}", self.base))
    }

    /// A new slot as `{slot, token, expires_in}`, or `restore`'s held slot put back:
    /// `{restored: true}`, or `{refused: "taken"}` or `{refused: "ended"}`.
    pub async fn issue(&self, restore: Option<&Value>) -> Result<Value, RoomsError> {
        if self.bearer.is_empty() {
            return Err(RoomsError::Refused(format!("{BEARER_ENV} is not set on this node, so it cannot issue a slot")));
        }
        let mut req = self.client.post(format!("{}/issue", self.base)).bearer_auth(&self.bearer);
        if let Some(r) = restore {
            req = req.json(r);
        }
        let resp = req.send().await.map_err(|e| self.unreachable(e))?;
        let status = resp.status().as_u16();
        match status {
            503 => return Err(RoomsError::Refused("the relay has no slot free right now; try again later".into())),
            409 | 410 if restore.is_some() => return Ok(json!({"refused": if status == 409 { "taken" } else { "ended" }})),
            400 if restore.is_some() => return Err(RoomsError::Refused("the relay refused the restore: it names a slot, a seat token and an owner token".into())),
            200 => {}
            _ => return Err(RoomsError::Refused(format!("the relay refused to issue a slot ({status}); check {BASE_ENV} and {BEARER_ENV}"))),
        }
        if restore.is_some() {
            return Ok(json!({"restored": true}));
        }
        let b: Value = resp.json().await.map_err(|e| self.unreachable(e))?;
        Ok(json!({"slot": b["slot"], "token": b["token"], "expires_in": b["expires_in"]}))
    }

    /// Open a room: its name and our host token.
    pub async fn open(&self, lasts: &str) -> Result<(String, String), RoomsError> {
        if self.bearer.is_empty() {
            return Err(RoomsError::Refused(format!("{BEARER_ENV} is not set on this node, so it cannot open a room")));
        }
        let resp = self.client.post(format!("{}/room", self.base)).bearer_auth(&self.bearer).json(&json!({"kind": lasts})).send().await.map_err(|e| self.unreachable(e))?;
        match resp.status().as_u16() {
            503 => Err(RoomsError::Refused("the relay has no room free right now; try again later".into())),
            200 => {
                let b: Value = resp.json().await.map_err(|e| self.unreachable(e))?;
                Ok((b["name"].as_str().unwrap_or_default().to_string(), b["host"].as_str().unwrap_or_default().to_string()))
            }
            s => Err(RoomsError::Refused(format!("the relay refused to open a room ({s}); check {BASE_ENV} and {BEARER_ENV}"))),
        }
    }

    pub async fn send(&self, name: &str, token: &str, message: &Value) -> Result<i64, RoomsError> {
        let resp = self.client.post(format!("{}/room/{name}/send", self.base)).bearer_auth(token).json(message).send().await.map_err(|e| self.unreachable(e))?;
        match resp.status().as_u16() {
            404 => Err(RoomsError::Gone(name.into())),
            410 => {
                let b: Value = resp.json().await.unwrap_or(json!({}));
                Err(RoomsError::Gone(b["ended"].as_str().unwrap_or(name).to_string()))
            }
            200 => {
                let b: Value = resp.json().await.map_err(|e| self.unreachable(e))?;
                Ok(b["seq"].as_i64().unwrap_or(0))
            }
            s => {
                let text = resp.text().await.unwrap_or_default();
                Err(RoomsError::Refused(format!("the relay refused the message ({s}): {}", text.trim())))
            }
        }
    }

    pub async fn recv(&self, name: &str, token: &str, after: i64) -> Result<Value, RoomsError> {
        let resp = self
            .client
            .get(format!("{}/room/{name}/recv", self.base))
            .query(&[("after", after.to_string()), ("wait", WAIT_S.to_string())])
            .bearer_auth(token)
            .send()
            .await
            .map_err(|e| self.unreachable(e))?;
        match resp.status().as_u16() {
            404 => Err(RoomsError::Gone(name.into())),
            200 => resp.json().await.map_err(|e| self.unreachable(e)),
            s => Err(RoomsError::Unreachable(format!("the relay answered {s} to a receive"))),
        }
    }

    /// Leave as host. An unreachable relay ends the room itself once we go quiet.
    pub async fn leave(&self, name: &str, token: &str) {
        let _ = self.client.post(format!("{}/room/{name}/leave", self.base)).bearer_auth(token).send().await;
    }
}
