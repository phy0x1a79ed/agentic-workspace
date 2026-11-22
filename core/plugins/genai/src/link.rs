//! genai's end of channel's direct socket (channels.md R72 item 5).
//!
//! `channel attach` through the gateway gives the room and a loopback port and token once per
//! connection; every room message and every write then crosses one websocket straight to
//! channel. The room is channel's and outlives the socket, so the link attaches again whenever
//! the socket drops. What Muse sent meanwhile waits at channel, which replays everything past
//! the last ack: an entry is acked only once a job has taken it.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_plugin_sdk::hub::Hub;
use awm_plugin_sdk::Backoff;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::{mpsc, oneshot, watch};
use tokio_tungstenite::tungstenite::Message;

use crate::provider::call;

/// Room entries no job has taken, oldest dropped first: a stray reply must not grow without end.
const MAIL_MAX: usize = 200;

#[derive(Clone, Debug, PartialEq)]
pub struct Room {
    pub name: String,
    pub link: String,
}

enum Out {
    Request(Value, oneshot::Sender<Result<Value, String>>),
    Ack(u64),
}

pub struct Link {
    hub: Hub,
    room: watch::Sender<Option<Room>>,
    mail: Mutex<VecDeque<(Value, u64)>>,
    arrived: watch::Sender<u64>,
    out: mpsc::UnboundedSender<Out>,
    out_rx: Mutex<Option<mpsc::UnboundedReceiver<Out>>>,
}

impl Link {
    pub fn new(hub: Hub) -> Arc<Self> {
        let (out, out_rx) = mpsc::unbounded_channel();
        Arc::new(Self {
            hub,
            room: watch::channel(None).0,
            mail: Mutex::default(),
            arrived: watch::channel(0).0,
            out,
            out_rx: Mutex::new(Some(out_rx)),
        })
    }

    /// The room while the socket is up.
    pub fn room(&self) -> Option<Room> {
        self.room.borrow().clone()
    }

    pub async fn attached(&self) -> Room {
        let mut rx = self.room.subscribe();
        let room = rx.wait_for(Option::is_some).await.expect("the link outlives its waiters");
        room.clone().expect("waited for a room")
    }

    /// One `write` or `send` over the socket. A request made while detached goes on the next socket.
    pub async fn request(&self, op: Value, timeout: Duration) -> Result<Value, String> {
        let (tx, rx) = oneshot::channel();
        let what = op["op"].as_str().unwrap_or("request").to_string();
        self.out.send(Out::Request(op, tx)).map_err(|_| "the link to channel stopped".to_string())?;
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(answer)) => answer.map_err(|e| format!("channel {what}: {e}")),
            Ok(Err(_)) => Err(format!("channel {what}: the socket to channel dropped")),
            Err(_) => Err(format!("channel {what}: no answer within {} s", timeout.as_secs())),
        }
    }

    /// The first room entry `pick` takes, before `until`. Taking it acks it and everything before.
    pub async fn take<T>(&self, pick: impl Fn(&Value) -> Option<T>, until: tokio::time::Instant) -> Option<T> {
        let mut arrived = self.arrived.subscribe();
        loop {
            let taken = {
                let mut mail = self.mail.lock().unwrap_or_else(|p| p.into_inner());
                let found = mail.iter().enumerate().find_map(|(i, (e, c))| pick(e).map(|t| (i, *c, t)));
                found.map(|(i, cursor, t)| {
                    mail.drain(..=i);
                    (cursor, t)
                })
            };
            if let Some((cursor, t)) = taken {
                let _ = self.out.send(Out::Ack(cursor));
                return Some(t);
            }
            if tokio::time::timeout_at(until, arrived.changed()).await.is_err() {
                return None;
            }
        }
    }

    /// Hold the socket for as long as genai runs, attaching again after each drop.
    pub async fn run(self: Arc<Self>) -> anyhow::Result<()> {
        let mut out = self.out_rx.lock().unwrap_or_else(|p| p.into_inner()).take().ok_or_else(|| anyhow::anyhow!("the link runs once"))?;
        let mut backoff = Backoff::doubling(1.0, 30.0);
        loop {
            if let Err(e) = self.session(&mut out, &mut backoff).await {
                tracing::warn!("the socket to channel ended: {e}");
            }
            self.room.send_replace(None);
            tokio::time::sleep(backoff.wait()).await;
        }
    }

    async fn session(&self, out: &mut mpsc::UnboundedReceiver<Out>, backoff: &mut Backoff) -> anyhow::Result<()> {
        let door = call(&self.hub, "channel", "attach", json!({}), Duration::from_secs(90)).await.map_err(anyhow::Error::msg)?;
        let link = door["link"].as_str().unwrap_or_default().to_string();
        if door["fresh"] == true {
            tracing::warn!("channel opened a new Muse room: give Muse its link in the \"awm genai link\" side chat: {link}");
        }
        let (ws, _) = tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{}", door["port"])).await?;
        let (mut tx, mut rx) = ws.split();
        tx.send(Message::Text(json!({"token": door["token"]}).to_string().into())).await?;
        let hello = match rx.next().await {
            Some(Ok(Message::Text(t))) => serde_json::from_str::<Value>(&t)?,
            other => anyhow::bail!("no hello from channel: {other:?}"),
        };
        let Some(name) = hello["hello"]["name"].as_str() else { anyhow::bail!("channel refused the socket: {hello}") };
        self.mail.lock().unwrap_or_else(|p| p.into_inner()).clear();
        self.room.send_replace(Some(Room { name: name.to_string(), link }));
        backoff.reset();
        let mut pending: HashMap<u64, oneshot::Sender<Result<Value, String>>> = HashMap::new();
        let mut next = 0u64;
        loop {
            tokio::select! {
                frame = rx.next() => {
                    let f: Value = match frame {
                        Some(Ok(Message::Text(t))) => serde_json::from_str(&t)?,
                        Some(Ok(Message::Close(_))) | None => anyhow::bail!("channel closed the socket"),
                        Some(Ok(_)) => continue,
                        Some(Err(e)) => return Err(e.into()),
                    };
                    if let (Some(event), Some(cursor)) = (f.get("event"), f["cursor"].as_u64()) {
                        let mut mail = self.mail.lock().unwrap_or_else(|p| p.into_inner());
                        mail.push_back((event.clone(), cursor));
                        while mail.len() > MAIL_MAX {
                            mail.pop_front();
                        }
                        drop(mail);
                        self.arrived.send_modify(|n| *n += 1);
                    } else if let Some(waiter) = f["id"].as_u64().and_then(|id| pending.remove(&id)) {
                        let answer = if f["ok"] == true { Ok(f["result"].clone()) } else { Err(f["error"].as_str().unwrap_or("failed").to_string()) };
                        let _ = waiter.send(answer);
                    } else if f.get("closed").is_some() {
                        anyhow::bail!("channel says the room closed: {f}");
                    }
                }
                sent = out.recv() => match sent {
                    Some(Out::Request(mut op, waiter)) => {
                        next += 1;
                        op["id"] = json!(next);
                        pending.insert(next, waiter);
                        tx.send(Message::Text(op.to_string().into())).await?;
                    }
                    Some(Out::Ack(cursor)) => tx.send(Message::Text(json!({"ack": cursor}).to_string().into())).await?,
                    None => return Ok(()),
                },
            }
        }
    }
}
