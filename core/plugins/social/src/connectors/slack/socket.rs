//! One Socket Mode connection: `apps.connections.open` with the app token, then the websocket.
//!
//! Every envelope is acked the moment it is read, before anything handles it, so a slow handler
//! never makes Slack redeliver: the events go on a queue that one task drains in order.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::mpsc::UnboundedSender;
use tokio_tungstenite::tungstenite::Message;

use awm_chat::slack::api::Client;

pub enum Signal {
    /// The socket is up: whatever arrived while it was down needs a sync.
    Hello,
    Event(Value),
}

const IDLE: Duration = Duration::from_secs(30);

/// Run one connection until Slack disconnects it or it goes quiet for three idle periods.
pub async fn run_once(api: &Client, app_token: &str, live: &AtomicBool, tx: &UnboundedSender<Signal>) -> anyhow::Result<()> {
    let opened = api.call_as("apps.connections.open", Some(app_token), &[]).await?;
    let url = opened.get("url").and_then(Value::as_str).ok_or_else(|| anyhow::anyhow!("apps.connections.open gave no url"))?;
    let (mut ws, _) = tokio_tungstenite::connect_async(url).await?;
    live.store(true, Ordering::SeqCst);
    let out = pump(&mut ws, tx).await;
    live.store(false, Ordering::SeqCst);
    let _ = ws.close(None).await;
    out
}

async fn pump<S>(ws: &mut tokio_tungstenite::WebSocketStream<S>, tx: &UnboundedSender<Signal>) -> anyhow::Result<()>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    let mut idle = 0;
    loop {
        let frame = match tokio::time::timeout(IDLE, ws.next()).await {
            Err(_) => {
                idle += 1;
                if idle >= 3 {
                    anyhow::bail!("socket quiet for {}s", IDLE.as_secs() * 3);
                }
                ws.send(Message::Ping(Vec::new().into())).await?;
                continue;
            }
            Ok(None) => return Ok(()),
            Ok(Some(f)) => f?,
        };
        idle = 0;
        let text = match frame {
            Message::Text(t) => t,
            Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => continue,
            Message::Binary(_) | Message::Close(_) => return Ok(()),
        };
        let Ok(env) = serde_json::from_str::<Value>(&text) else { continue };
        if let Some(id) = env.get("envelope_id").and_then(Value::as_str).filter(|i| !i.is_empty()) {
            ws.send(Message::text(json!({"envelope_id": id}).to_string())).await?;
        }
        match env.get("type").and_then(Value::as_str).unwrap_or("") {
            "hello" => {
                tracing::info!("slack Socket Mode connected");
                let _ = tx.send(Signal::Hello);
            }
            "disconnect" => return Ok(()),
            "events_api" => {
                let event = env.pointer("/payload/event").cloned().unwrap_or(Value::Null);
                let _ = tx.send(Signal::Event(event));
            }
            _ => {}
        }
    }
}
