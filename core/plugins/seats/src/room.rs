//! The channel room of each room seat, held by this service over channel's direct socket
//! (channels.md R72 item 5), one socket per seat.
//!
//! `channel attach key=<project>/<scope>` gives the seat's room and a one-use token. The room is
//! channel's and outlives the socket, so the holder attaches again whenever it drops, and the
//! link stays the same. Each guest message is queued as a seat line before it is acked, so a
//! drop or a restart loses none: channel replays everything past the last ack.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_plugin_sdk::{Backoff, CallOpts, Hub};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::{mpsc, oneshot, watch};
use tokio_tungstenite::tungstenite::Message;

use crate::engine::Seats;
use crate::store::Seat;

type Key = (String, String);
type Request = (Value, oneshot::Sender<Result<Value, String>>);

/// How long a write waits for its answer, attached or not.
const WRITE_WAIT: Duration = Duration::from_secs(30);

struct Room {
    link: watch::Sender<Option<String>>,
    out: mpsc::UnboundedSender<Request>,
}

pub struct Rooms {
    hub: Hub,
    seats: Arc<Seats>,
    held: Mutex<HashMap<Key, Arc<Room>>>,
}

/// The seat line of one room message.
pub fn line(scope: &str, event: &Value) -> String {
    let field = |k: &str| event[k].as_str().unwrap_or("").to_string();
    let file = event["file"]["name"].as_str().map(|n| format!(" [file {n} not carried; ask for it as text]")).unwrap_or_default();
    format!("[room/{scope}/{}, untrusted, msg {}] {}{file}", field("who"), event["seq"].as_i64().unwrap_or(0), field("text"))
}

impl Rooms {
    pub fn new(hub: Hub, seats: Arc<Seats>) -> Arc<Self> {
        Arc::new(Self { hub, seats, held: Mutex::default() })
    }

    /// Hold the seat's room, from the first call on, for as long as the service runs.
    fn hold(self: &Arc<Self>, seat: &Seat) -> Arc<Room> {
        let mut held = self.held.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(room) = held.get(&seat.key()) {
            return room.clone();
        }
        let (out, requests) = mpsc::unbounded_channel();
        let room = Arc::new(Room { link: watch::channel(None).0, out });
        held.insert(seat.key(), room.clone());
        let attach_key = format!("{}/{}", seat.decl.project, seat.decl.scope);
        tokio::spawn(self.clone().run(seat.key(), attach_key, seat.decl.scope.clone(), room.clone(), requests));
        room
    }

    /// Hold the seat's room from now on.
    pub fn start(self: &Arc<Self>, seat: &Seat) {
        self.hold(seat);
    }

    /// The room's link once attached, waiting at most `wait`.
    pub async fn link(self: &Arc<Self>, seat: &Seat, wait: Duration) -> Option<String> {
        let mut rx = self.hold(seat).link.subscribe();
        let link = tokio::time::timeout(wait, rx.wait_for(Option::is_some)).await.ok()?.ok()?;
        link.clone()
    }

    /// Write `text` into the seat's room as its host.
    pub async fn write(self: &Arc<Self>, seat: &Seat, text: &str) -> Result<Value, String> {
        let (tx, rx) = oneshot::channel();
        self.hold(seat).out.send((json!({"op": "write", "text": text}), tx)).map_err(|_| "the room's holder stopped".to_string())?;
        match tokio::time::timeout(WRITE_WAIT, rx).await {
            Ok(Ok(answer)) => answer,
            Ok(Err(_)) => Err("the socket to channel dropped with no answer, so the room may not have it. `channel` shows nothing of it; send it again only if the guest says it never came".into()),
            Err(_) => Err(format!("channel did not answer within {} s. The reply goes out when the socket is back. Do not send it again.", WRITE_WAIT.as_secs())),
        }
    }

    async fn run(self: Arc<Self>, key: Key, attach_key: String, scope: String, room: Arc<Room>, mut requests: mpsc::UnboundedReceiver<Request>) {
        let mut backoff = Backoff::doubling(1.0, 60.0);
        loop {
            if self.seats.store.get(&key.0, &key.1).await.is_none() {
                self.held.lock().unwrap_or_else(|p| p.into_inner()).remove(&key);
                return;
            }
            if let Err(e) = self.session(&key, &attach_key, &scope, &room, &mut requests, &mut backoff).await {
                tracing::warn!("room {attach_key}: the socket to channel ended: {e}");
            }
            room.link.send_replace(None);
            tokio::time::sleep(backoff.wait()).await;
        }
    }

    async fn session(&self, key: &Key, attach_key: &str, scope: &str, room: &Room, requests: &mut mpsc::UnboundedReceiver<Request>, backoff: &mut Backoff) -> anyhow::Result<()> {
        let opts = CallOpts { timeout: Duration::from_secs(90), ..CallOpts::default() };
        let door = self.hub.call_with("channel", "attach", json!({"key": attach_key}), &opts).await?;
        let link = door["link"].as_str().unwrap_or_default().to_string();
        let (ws, _) = tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{}", door["port"])).await?;
        let (mut tx, mut rx) = ws.split();
        tx.send(Message::Text(json!({"token": door["token"]}).to_string().into())).await?;
        match rx.next().await {
            Some(Ok(Message::Text(t))) if serde_json::from_str::<Value>(&t)?["hello"]["name"].is_string() => {}
            other => anyhow::bail!("channel refused the socket: {other:?}"),
        }
        self.seats.set_room_link(key, &link).await;
        room.link.send_replace(Some(link));
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
                        if !self.seats.take_room(key, &line(scope, event)).await {
                            anyhow::bail!("the seat is gone");
                        }
                        tx.send(Message::Text(json!({"ack": cursor}).to_string().into())).await?;
                    } else if let Some(waiter) = f["id"].as_u64().and_then(|id| pending.remove(&id)) {
                        let _ = waiter.send(if f["ok"] == true { Ok(f["result"].clone()) } else { Err(f["error"].as_str().unwrap_or("failed").to_string()) });
                    } else if f.get("closed").is_some() {
                        anyhow::bail!("channel says the room closed: {f}");
                    }
                }
                request = requests.recv() => {
                    let Some((mut op, waiter)) = request else { return Ok(()) };
                    next += 1;
                    op["id"] = json!(next);
                    pending.insert(next, waiter);
                    tx.send(Message::Text(op.to_string().into())).await?;
                }
            }
        }
    }
}
