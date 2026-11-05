//! The serving side: `awm edge-serve`, run by sshd for an edge's user key.

use std::future::Future;
use std::sync::Arc;

use std::collections::HashMap;

use awm_gateway::peer::WsPipe;
use awm_proto::{Caller, Lane, WsMsg};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, Mutex};

use crate::frame::{Request, Response, WS_CLOSE, WS_OPEN, WS_SEND};

/// The node whose user key opened this login, from sshd's `SSH_USER_AUTH` file
/// (needs `ExposeAuthInfo yes`) matched against `authorized_keys`, whose lines
/// end in a comment `awm:<node>:user`. `None` when no line matches.
pub fn caller_node(user_auth: &str, authorized_keys: &str) -> Option<String> {
    let used: Vec<&str> = user_auth
        .lines()
        .filter_map(|l| l.strip_prefix("publickey "))
        .filter_map(|k| k.split_whitespace().nth(1))
        .collect();
    authorized_keys.lines().find_map(|line| {
        let fields: Vec<&str> = line.split_whitespace().collect();
        let blob_at = fields.iter().position(|f| f.starts_with("ssh-") || f.starts_with("ecdsa-"))?;
        let blob = fields.get(blob_at + 1)?;
        let comment = fields.get(blob_at + 2)?;
        let mut parts = comment.split(':');
        match (parts.next(), parts.next(), parts.next()) {
            (Some("awm"), Some(node), Some("user")) if used.contains(blob) => Some(node.to_string()),
            _ => None,
        }
    })
}

async fn write_line<W: AsyncWrite + Unpin>(writer: &Mutex<W>, resp: &Response) {
    let mut out = serde_json::to_vec(resp).expect("json");
    out.push(b'\n');
    let mut w = writer.lock().await;
    let _ = w.write_all(&out).await;
    let _ = w.flush().await;
}

/// Serve requests from `reader` until it closes. Each request runs as `from`
/// at the edge lane, at the trust level the caller's node vouched for. `invoke`
/// runs a call; `open_ws` opens a stream path on this node's gateway.
pub async fn serve<R, W, F, Fut, O, OFut>(reader: R, writer: W, from: String, invoke: F, open_ws: O)
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin + Send + 'static,
    F: Fn(String, Value, Caller) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<Value, String>> + Send + 'static,
    O: Fn(String, Caller) -> OFut + Send + Sync + 'static,
    OFut: Future<Output = Result<WsPipe, String>> + Send + 'static,
{
    let writer = Arc::new(Mutex::new(writer));
    let invoke = Arc::new(invoke);
    let open_ws = Arc::new(open_ws);
    // Each open stream's sender into this node's socket, by stream id.
    let streams: Arc<Mutex<HashMap<u64, mpsc::UnboundedSender<WsMsg>>>> = Arc::default();
    let mut lines = BufReader::new(reader).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let Ok(req) = serde_json::from_str::<Request>(&line) else { continue };
        let caller = Caller { node: from.clone(), lane: Lane::Edge, session: req.caller.session, trust: req.caller.trust,
                              scope: req.caller.scope, origin: req.caller.origin.filter(|o| *o != from) };
        match req.name.as_str() {
            WS_SEND => {
                if let (Some(tx), Ok(msg)) = (streams.lock().await.get(&req.id), serde_json::from_value::<WsMsg>(req.args)) {
                    let _ = tx.send(msg);
                }
            }
            WS_CLOSE => {
                streams.lock().await.remove(&req.id);
            }
            WS_OPEN => {
                let (writer, open_ws, streams) = (writer.clone(), open_ws.clone(), streams.clone());
                let path = req.args.get("path").and_then(Value::as_str).unwrap_or_default().to_string();
                let id = req.id;
                // Messages may follow the open before the stream is up: hold them until it is.
                let (early_tx, mut early_rx) = mpsc::unbounded_channel();
                streams.lock().await.insert(id, early_tx);
                tokio::spawn(async move {
                    let pipe = match open_ws(path, caller).await {
                        Ok(p) => p,
                        Err(e) => {
                            streams.lock().await.remove(&id);
                            write_line(&writer, &Response { id, ok: false, result: None, error: Some(e), more: false }).await;
                            return;
                        }
                    };
                    // Only the forwarder holds the sender, so a WS_CLOSE ends the far socket.
                    let WsPipe { tx: into, rx: mut from_far } = pipe;
                    tokio::spawn(async move {
                        while let Some(m) = early_rx.recv().await {
                            if into.send(m).is_err() {
                                break;
                            }
                        }
                    });
                    let mut reason = None;
                    while let Some(msg) = from_far.recv().await {
                        if let WsMsg::Close(r) = msg {
                            reason = r;
                            break;
                        }
                        let result = serde_json::to_value(&msg).ok();
                        write_line(&writer, &Response { id, ok: true, result, error: None, more: true }).await;
                    }
                    streams.lock().await.remove(&id);
                    write_line(&writer, &Response { id, ok: true, result: None, error: reason, more: false }).await;
                });
            }
            _ => {
                let (writer, invoke) = (writer.clone(), invoke.clone());
                tokio::spawn(async move {
                    let resp = match invoke(req.name, req.args, caller).await {
                        Ok(v) => Response { id: req.id, ok: true, result: Some(v), error: None, more: false },
                        Err(e) => Response { id: req.id, ok: false, result: None, error: Some(e), more: false },
                    };
                    write_line(&writer, &resp).await;
                });
            }
        }
    }
}
