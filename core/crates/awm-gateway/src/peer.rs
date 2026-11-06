//! How a call with `peer` reaches another node. The trust lane's `awm-edge` implements
//! [`PeerTransport`] over ssh. [`HttpPeers`] posts to a peer gateway's `/invoke`, for tests.
//!
//! A stream (an emit subscription or a session) crosses as a [`WsPipe`]: the far node opens
//! the same `/hub/emit/...` or `/hub/session/...` WebSocket on its own gateway, and each
//! message travels as a [`WsMsg`].

use std::collections::BTreeMap;
use std::future::Future;
use std::pin::Pin;

use awm_proto::{CallContext, WsMsg};
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::sync::mpsc;

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

#[derive(Debug, thiserror::Error)]
pub enum PeerError {
    #[error("unknown peer {0:?}")]
    Unknown(String),
    #[error("peer {peer} is unreachable: {reason}")]
    Unreachable { peer: String, reason: String },
    /// The peer answered with an error status and body.
    #[error("peer {peer} refused the call ({status}): {body}")]
    Refused { peer: String, status: u16, body: String },
}

pub trait PeerTransport: Send + Sync {
    /// The peers this transport can reach.
    fn peers(&self) -> Vec<String>;

    /// Run `name` with `args` (the `{verb, args}` envelope) on `peer`. Returns the result string.
    fn invoke<'a>(
        &'a self,
        peer: &'a str,
        name: &'a str,
        args: Value,
        ctx: &'a CallContext,
    ) -> BoxFuture<'a, Result<String, PeerError>>;

    /// The tools `peer` serves: its `/hub/table`, for this node's name table.
    fn tools<'a>(&'a self, peer: &'a str) -> BoxFuture<'a, Result<Value, PeerError>>;

    /// Tell `peer` that this node's table changed, so it reads `tools` again (R117 item 3).
    /// A transport that cannot deliver it leaves the peer on its cached table until it restarts.
    fn table_changed<'a>(&'a self, _peer: &'a str, _node: &'a str) -> BoxFuture<'a, Result<(), PeerError>> {
        Box::pin(async { Ok(()) })
    }

    /// Open the WebSocket at `path` (a `/hub/emit/...` or `/hub/session/...` path with its
    /// query) on `peer`'s gateway, as this call's caller.
    fn open_ws<'a>(&'a self, peer: &'a str, _path: &'a str, _ctx: &'a CallContext) -> BoxFuture<'a, Result<WsPipe, PeerError>> {
        Box::pin(async move { Err(PeerError::Unreachable { peer: peer.into(), reason: "this transport carries no streams".into() }) })
    }
}

/// One end of a stream: `tx` sends toward the far end, `rx` yields what it sends back. The
/// stream ends when either side drops its half or sends `WsMsg::Close`.
pub struct WsPipe {
    pub tx: mpsc::UnboundedSender<WsMsg>,
    pub rx: mpsc::UnboundedReceiver<WsMsg>,
}

impl WsPipe {
    /// Two pipes joined back to back: what one sends, the other receives.
    pub fn pair() -> (WsPipe, WsPipe) {
        let (a_tx, b_rx) = mpsc::unbounded_channel();
        let (b_tx, a_rx) = mpsc::unbounded_channel();
        (WsPipe { tx: a_tx, rx: a_rx }, WsPipe { tx: b_tx, rx: b_rx })
    }
}

/// Paths a stream may open on a peer: never the control socket or a further hop.
pub fn stream_path_allowed(path: &str) -> bool {
    (path.starts_with("/hub/emit/") || path.starts_with("/hub/session/")) && !path.contains("peer=") && !path.contains("..")
}

/// Drive a client WebSocket from a pipe, until either side ends.
pub fn pipe_from_tungstenite<S>(ws: tokio_tungstenite::WebSocketStream<S>) -> WsPipe
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    use tokio_tungstenite::tungstenite::protocol::{frame::coding::CloseCode, CloseFrame};
    use tokio_tungstenite::tungstenite::Message;
    let (mine, theirs) = WsPipe::pair();
    let WsPipe { tx, mut rx } = theirs;
    let (mut sink, mut stream) = ws.split();
    tokio::spawn(async move {
        loop {
            tokio::select! {
                out = rx.recv() => {
                    let msg = match out {
                        Some(WsMsg::Text(t)) => Message::Text(t.into()),
                        Some(WsMsg::Binary(b)) => Message::Binary(b.into()),
                        Some(WsMsg::Close(reason)) => {
                            let frame = reason.map(|r| CloseFrame { code: CloseCode::Error, reason: r.into() });
                            let _ = sink.send(Message::Close(frame)).await;
                            break;
                        }
                        None => { let _ = sink.close().await; break; }
                    };
                    if sink.send(msg).await.is_err() { break; }
                }
                inc = stream.next() => {
                    let msg = match inc {
                        Some(Ok(Message::Text(t))) => WsMsg::Text(t.to_string()),
                        Some(Ok(Message::Binary(b))) => WsMsg::Binary(b.to_vec()),
                        Some(Ok(Message::Close(f))) => {
                            let _ = tx.send(WsMsg::Close(f.map(|f| f.reason.to_string()).filter(|r| !r.is_empty())));
                            break;
                        }
                        Some(Ok(_)) => continue,
                        Some(Err(e)) => { let _ = tx.send(WsMsg::Close(Some(e.to_string()))); break; }
                        None => break,
                    };
                    if tx.send(msg).is_err() { break; }
                }
            }
        }
    });
    mine
}

/// Connect to a WebSocket with extra headers and return its pipe.
pub async fn connect_ws(url: &str, headers: &[(&str, String)]) -> Result<WsPipe, String> {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    let mut req = url.into_client_request().map_err(|e| e.to_string())?;
    for (k, v) in headers {
        let name = tokio_tungstenite::tungstenite::http::HeaderName::from_bytes(k.as_bytes()).map_err(|e| e.to_string())?;
        req.headers_mut().insert(name, v.parse().map_err(|_| format!("bad header {k}"))?);
    }
    let (ws, _) = tokio_tungstenite::connect_async(req).await.map_err(|e| e.to_string())?;
    Ok(pipe_from_tungstenite(ws))
}

pub struct HttpPeers {
    urls: BTreeMap<String, String>,
    client: reqwest::Client,
}

impl HttpPeers {
    pub fn new(urls: BTreeMap<String, String>) -> Self {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(300))
            .build()
            .expect("reqwest client builds");
        Self { urls, client }
    }

    fn url(&self, peer: &str) -> Result<&str, PeerError> {
        self.urls.get(peer).map(|u| u.trim_end_matches('/')).ok_or_else(|| PeerError::Unknown(peer.into()))
    }
}

impl PeerTransport for HttpPeers {
    fn peers(&self) -> Vec<String> {
        self.urls.keys().cloned().collect()
    }

    fn invoke<'a>(
        &'a self,
        peer: &'a str,
        name: &'a str,
        args: Value,
        ctx: &'a CallContext,
    ) -> BoxFuture<'a, Result<String, PeerError>> {
        Box::pin(async move {
            let url = format!("{}/invoke", self.url(peer)?);
            let mut req = self
                .client
                .post(&url)
                .header("X-Awm-Surface", ctx.surface.as_str())
                .header("X-Awm-Caller-Node", &ctx.caller.node)
                .json(&serde_json::json!({"name": name, "args": args}));
            if let Some(as_) = &ctx.as_ {
                req = req.header("X-Awm-As", as_);
            }
            let resp = req
                .send()
                .await
                .map_err(|e| PeerError::Unreachable { peer: peer.into(), reason: e.to_string() })?;
            let status = resp.status().as_u16();
            let body = resp.text().await.unwrap_or_default();
            if !(200..300).contains(&status) {
                return Err(PeerError::Refused { peer: peer.into(), status, body });
            }
            let v: Value = serde_json::from_str(&body)
                .map_err(|e| PeerError::Unreachable { peer: peer.into(), reason: format!("bad reply: {e}") })?;
            Ok(v.get("result").and_then(Value::as_str).unwrap_or_default().to_string())
        })
    }

    fn tools<'a>(&'a self, peer: &'a str) -> BoxFuture<'a, Result<Value, PeerError>> {
        Box::pin(async move {
            let url = format!("{}/hub/table", self.url(peer)?);
            let resp = self
                .client
                .get(&url)
                .timeout(std::time::Duration::from_secs(2))
                .send()
                .await
                .map_err(|e| PeerError::Unreachable { peer: peer.into(), reason: e.to_string() })?;
            resp.json()
                .await
                .map_err(|e| PeerError::Unreachable { peer: peer.into(), reason: e.to_string() })
        })
    }

    fn open_ws<'a>(&'a self, peer: &'a str, path: &'a str, ctx: &'a CallContext) -> BoxFuture<'a, Result<WsPipe, PeerError>> {
        Box::pin(async move {
            if !stream_path_allowed(path) {
                return Err(PeerError::Refused { peer: peer.into(), status: 403, body: format!("{path} is not a stream path") });
            }
            let url = format!("{}{path}", self.url(peer)?.replacen("http", "ws", 1));
            connect_ws(&url, &[("x-awm-caller-node", ctx.caller.node.clone())])
                .await
                .map_err(|reason| PeerError::Unreachable { peer: peer.into(), reason })
        })
    }

    fn table_changed<'a>(&'a self, peer: &'a str, node: &'a str) -> BoxFuture<'a, Result<(), PeerError>> {
        Box::pin(async move {
            let url = format!("{}/hub/table/changed", self.url(peer)?);
            let resp = self
                .client
                .post(&url)
                .timeout(std::time::Duration::from_secs(5))
                .json(&serde_json::json!({"node": node}))
                .send()
                .await
                .map_err(|e| PeerError::Unreachable { peer: peer.into(), reason: e.to_string() })?;
            let status = resp.status().as_u16();
            if !(200..300).contains(&status) {
                return Err(PeerError::Refused { peer: peer.into(), status, body: resp.text().await.unwrap_or_default() });
            }
            Ok(())
        })
    }
}
