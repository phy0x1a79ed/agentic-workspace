//! The direct socket a holding service keeps to channel (channels.md R72 item 5).
//!
//! The gateway introduces the two once: `attach` answers the service's room, this node's
//! loopback port and a one-use token. The service then holds a websocket straight to channel,
//! and every room message and every write crosses it, never the gateway. Its room belongs to
//! channel and outlives the socket: what arrives while the service is away waits in the room's
//! inbox file, and channel replays it from the service's last ack when it attaches again.
//!
//! Frames, one JSON object each. The service sends `{token}` first, then `{ack: <cursor>}` and
//! `{id, op: "write", text}` or `{id, op: "send", b64, filename, text}`. Channel sends
//! `{hello: {name, link}}`, then `{event, cursor}` per inbox entry and `{id, ok, result|error}`
//! per request.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::Message;

use super::{by_service, crypto, refuse, text, Channels, Holder, Res, Write};

pub struct Door {
    port: u16,
    listener: Mutex<Option<std::net::TcpListener>>,
    tokens: Mutex<HashMap<String, Holder>>,
    /// The live socket of each holding service: a new attach replaces the old one.
    held: Mutex<HashMap<String, tokio::task::AbortHandle>>,
}

impl Door {
    /// Bound when channel starts, so `attach` always has a port to give.
    pub fn bind() -> std::io::Result<Self> {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0))?;
        listener.set_nonblocking(true)?;
        Ok(Self { port: listener.local_addr()?.port(), listener: Mutex::new(Some(listener)), tokens: Mutex::default(), held: Mutex::default() })
    }
}

impl Channels {
    /// The service's room, opened the first time, and how to reach this node's channel directly.
    pub async fn attach(self: &Arc<Self>, holder: &Holder) -> Res<Value> {
        if !by_service(&holder.session) {
            return Err(refuse("only a plugin service attaches; a session holds its channels through its prompt"));
        }
        let port = self.door.port;
        let held = || self.store.of_holder(&holder.node, &holder.session, true).into_iter().find(|r| r.kind == "room");
        let fresh = held().is_none();
        if fresh {
            self.open(holder, "room", None, true).await?;
        }
        let row = held().ok_or_else(|| refuse("the room did not open"))?;
        let token = crypto::b64(&rand::random::<[u8; 24]>());
        self.door.tokens.lock().unwrap_or_else(|p| p.into_inner()).insert(token.clone(), holder.clone());
        Ok(json!({"port": port, "token": token, "name": row.name, "link": row.link, "fresh": fresh}))
    }

    /// Serve holding services' sockets on a loopback port until channel stops.
    pub async fn serve_door(self: Arc<Self>) -> anyhow::Result<()> {
        let bound = self.door.listener.lock().unwrap_or_else(|p| p.into_inner()).take();
        let listener = TcpListener::from_std(bound.ok_or_else(|| anyhow::anyhow!("the door is served already"))?)?;
        loop {
            let (tcp, _) = listener.accept().await?;
            let me = self.clone();
            tokio::spawn(async move {
                if let Err(e) = me.attached(tcp).await {
                    tracing::info!("an attached service's socket ended: {e}");
                }
            });
        }
    }

    async fn attached(self: Arc<Self>, tcp: TcpStream) -> anyhow::Result<()> {
        let mut ws = tokio_tungstenite::accept_async(tcp).await?;
        let first = match ws.next().await {
            Some(Ok(Message::Text(t))) => serde_json::from_str::<Value>(&t).unwrap_or(Value::Null),
            _ => anyhow::bail!("no token frame"),
        };
        let holder = self.door.tokens.lock().unwrap_or_else(|p| p.into_inner()).remove(&text(&first, "token"));
        let Some(holder) = holder else {
            let _ = ws.send(Message::Text(json!({"error": "unknown or used token; call `channel attach` again"}).to_string().into())).await;
            anyhow::bail!("a socket with an unknown token");
        };
        let me = self.clone();
        let key = holder.session.clone();
        let task = tokio::spawn(async move { me.hold(holder, ws).await });
        if let Some(old) = self.door.held.lock().unwrap_or_else(|p| p.into_inner()).insert(key, task.abort_handle()) {
            old.abort();
        }
        task.await.map_err(anyhow::Error::from)?
    }

    async fn hold(self: Arc<Self>, holder: Holder, ws: tokio_tungstenite::WebSocketStream<TcpStream>) -> anyhow::Result<()> {
        let (mut tx, mut rx) = ws.split();
        let Some(row) = self.store.of_holder(&holder.node, &holder.session, true).into_iter().find(|r| r.kind == "room") else {
            tx.send(Message::Text(json!({"error": "you hold no room; call `channel attach` again"}).to_string().into())).await?;
            return Ok(());
        };
        let name = row.name.clone();
        tx.send(Message::Text(json!({"hello": {"name": name, "link": row.link}}).to_string().into())).await?;
        let (out, mut outbox) = tokio::sync::mpsc::unbounded_channel::<Value>();
        let mut bumped = self.inbox.subscribe();
        let mut sent = row.log_cursor.max(0) as u64;
        loop {
            for (mut event, cursor) in self.transcripts.read_inbox(&name, sent) {
                with_bytes(&mut event);
                tx.send(Message::Text(json!({"event": event, "cursor": cursor}).to_string().into())).await?;
                sent = cursor;
            }
            if !self.store.get(&name).is_some_and(|r| r.open) {
                tx.send(Message::Text(json!({"closed": name}).to_string().into())).await?;
                return Ok(());
            }
            tokio::select! {
                changed = bumped.changed() => changed?,
                reply = outbox.recv() => {
                    let Some(reply) = reply else { continue };
                    tx.send(Message::Text(reply.to_string().into())).await?;
                }
                frame = rx.next() => {
                    let frame = match frame {
                        Some(Ok(Message::Text(t))) => serde_json::from_str::<Value>(&t).unwrap_or(Value::Null),
                        Some(Ok(Message::Close(_))) | None => return Ok(()),
                        Some(Ok(_)) => continue,
                        Some(Err(e)) => return Err(e.into()),
                    };
                    if let Some(cursor) = frame["ack"].as_u64() {
                        self.store.set_log(&name, cursor as i64, None);
                        continue;
                    }
                    let (me, holder, name, out) = (self.clone(), holder.clone(), name.clone(), out.clone());
                    tokio::spawn(async move {
                        let id = frame["id"].clone();
                        let reply = match me.request(&holder, &name, &frame).await {
                            Ok(result) => json!({"id": id, "ok": true, "result": result}),
                            Err(e) => json!({"id": id, "ok": false, "error": e.message}),
                        };
                        let _ = out.send(reply);
                    });
                }
            }
        }
    }

    async fn request(self: &Arc<Self>, holder: &Holder, name: &str, frame: &Value) -> Res<Value> {
        let said = text(frame, "text");
        match frame["op"].as_str() {
            Some("write") => self.write(holder, &said, Some(name), Write { target: "say".into(), ..Write::default() }).await,
            Some("send") => self.send(holder, "", frame["b64"].as_str(), &said, Some(name), frame["filename"].as_str()).await,
            other => Err(refuse(format!("op is write or send, not {other:?}"))),
        }
    }
}

/// A service shares no files with channel, so an entry's saved file crosses as its bytes.
fn with_bytes(event: &mut Value) {
    let Some(file) = event.get_mut("file").filter(|f| f["path"].is_string()) else { return };
    match std::fs::read(text(file, "path")) {
        Ok(bytes) => file["b64"] = json!(crypto::b64(&bytes)),
        Err(e) => file["error"] = json!(e.to_string()),
    }
}
