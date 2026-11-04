//! The serving side: `awm edge-serve`, run by sshd for an edge's user key.

use std::future::Future;
use std::sync::Arc;

use awm_proto::{Caller, Lane};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::Mutex;

use crate::frame::{Request, Response};

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

/// Serve requests from `reader` until it closes. Each request runs as `from`
/// at the edge lane, at the trust level the caller's node vouched for.
pub async fn serve<R, W, F, Fut>(reader: R, writer: W, from: String, invoke: F)
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin + Send + 'static,
    F: Fn(String, Value, Caller) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<Value, String>> + Send + 'static,
{
    let writer = Arc::new(Mutex::new(writer));
    let invoke = Arc::new(invoke);
    let mut lines = BufReader::new(reader).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let Ok(req) = serde_json::from_str::<Request>(&line) else { continue };
        let caller = Caller { node: from.clone(), lane: Lane::Edge, session: req.caller.session, trust: req.caller.trust,
                              scope: req.caller.scope };
        let (writer, invoke) = (writer.clone(), invoke.clone());
        tokio::spawn(async move {
            let resp = match invoke(req.name, req.args, caller).await {
                Ok(v) => Response { id: req.id, ok: true, result: Some(v), error: None },
                Err(e) => Response { id: req.id, ok: false, result: None, error: Some(e) },
            };
            let mut out = serde_json::to_vec(&resp).expect("json");
            out.push(b'\n');
            let mut w = writer.lock().await;
            let _ = w.write_all(&out).await;
            let _ = w.flush().await;
        });
    }
}
