//! The calling side: one warm channel per peer.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use awm_gateway::peer::WsPipe;
use awm_proto::{Caller, WsMsg};
use awm_trust::edges::{EdgeKey, EdgeList};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::{mpsc, oneshot, Mutex};

use crate::frame::{Request, Response, WS_CLOSE, WS_OPEN, WS_SEND};

#[derive(Debug, thiserror::Error)]
pub enum EdgeError {
    #[error("{peer} is unreachable: {reason}")]
    Unreachable { peer: String, reason: String },
    #[error("{peer} did not answer within {0:?}", .timeout)]
    Timeout { peer: String, timeout: Duration },
    #[error("{0}")]
    Remote(String),
}

/// How to open the channel to a peer: plain ssh, so `ssh <peer>` needs no setting of its own
/// (fleet.md R92). The default adds a shared master connection (`ControlMaster`,
/// `ControlPersist`) and a keep-alive. `AWM_EDGE_SSH` names another program to run in its
/// place, with the same arguments.
#[derive(Debug, Clone)]
pub struct SshCommand {
    pub program: String,
    pub args: Vec<String>,
}

impl SshCommand {
    /// `ssh <peer> AWM_EDGE_FROM=<here> awm edge-serve`. A service-key edge logs in with this node's
    /// service key only, and the far `authorized_keys` forces its command, so what it
    /// sends is ignored there. Each key keeps its own master: a master reuses its first login's
    /// key for every channel, so a user master must never carry a service edge.
    pub fn ssh(peer: &str, here: &str, key: EdgeKey) -> Self {
        let kind = match key {
            EdgeKey::User => "user",
            EdgeKey::Service => "service",
        };
        let control = format!("ControlPath=~/.ssh/cm-%C-{kind}");
        let opts = [
            "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=auto", "-o", control.as_str(),
            "-o", "ControlPersist=10m", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        ];
        let mut args: Vec<String> = opts.iter().map(|s| s.to_string()).collect();
        if key == EdgeKey::Service {
            args.extend(["-i".into(), awm_gateway::door::service_key_path(), "-o".into(), "IdentitiesOnly=yes".into()]);
        }
        // A non-interactive ssh shell often lacks ~/.local/bin, where the core binary lives.
        // A node on an older release refuses `--from` and ignores an environment prefix.
        args.extend([peer.to_string(), "PATH=\"$HOME/.local/bin:$PATH\"".into(), format!("AWM_EDGE_FROM={here}"), "awm".into(), "edge-serve".into()]);
        let program = std::env::var("AWM_EDGE_SSH").ok().filter(|p| !p.trim().is_empty()).unwrap_or_else(|| "ssh".into());
        Self { program, args }
    }
}

enum Waiter {
    Call(oneshot::Sender<Response>),
    Stream(mpsc::UnboundedSender<WsMsg>),
}

type Pending = Arc<Mutex<HashMap<u64, Waiter>>>;

struct Channel {
    writer: Mutex<Box<dyn AsyncWrite + Send + Unpin>>,
    pending: Pending,
    alive: Arc<AtomicBool>,
    _child: Option<tokio::process::Child>,
}

impl Channel {
    fn start(reader: impl AsyncRead + Send + Unpin + 'static, writer: impl AsyncWrite + Send + Unpin + 'static,
             child: Option<tokio::process::Child>) -> Arc<Self> {
        let pending: Pending = Arc::default();
        let alive = Arc::new(AtomicBool::new(true));
        let (p, a) = (pending.clone(), alive.clone());
        tokio::spawn(async move {
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                match serde_json::from_str::<Response>(&line) {
                    Ok(resp) => {
                        let mut map = p.lock().await;
                        match map.remove(&resp.id) {
                            Some(Waiter::Call(tx)) => {
                                let _ = tx.send(resp);
                            }
                            Some(Waiter::Stream(tx)) if resp.more => {
                                let msg = resp.result.and_then(|r| serde_json::from_value::<WsMsg>(r).ok());
                                if msg.is_none_or(|m| tx.send(m).is_ok()) {
                                    map.insert(resp.id, Waiter::Stream(tx));
                                }
                            }
                            Some(Waiter::Stream(tx)) => {
                                let _ = tx.send(WsMsg::Close(resp.error));
                            }
                            None => {}
                        }
                    }
                    Err(e) => tracing::warn!("edge: unreadable reply line: {e}"),
                }
            }
            a.store(false, Ordering::SeqCst);
            p.lock().await.clear(); // drops every sender: the waiting calls fail
        });
        Arc::new(Self { writer: Mutex::new(Box::new(writer)), pending, alive, _child: child })
    }
}

/// Opens a channel's two ends. Swapped out in tests.
pub type Opener = Arc<dyn Fn(&str) -> std::io::Result<(Box<dyn AsyncRead + Send + Unpin>, Box<dyn AsyncWrite + Send + Unpin>, Option<tokio::process::Child>)> + Send + Sync>;

async fn send_line(ch: &Channel, req: &Request) -> std::io::Result<()> {
    let mut line = serde_json::to_vec(req).expect("json");
    line.push(b'\n');
    let mut w = ch.writer.lock().await;
    w.write_all(&line).await?;
    w.flush().await
}

pub struct EdgeClient {
    open: Opener,
    channels: Mutex<HashMap<String, Arc<Channel>>>,
    next_id: AtomicU64,
    pub timeout: Duration,
}

impl EdgeClient {
    /// A client that reaches each peer by `ssh <peer> AWM_EDGE_FROM=<here> awm edge-serve`, with the
    /// key that `edges`' edge from `here` to the peer names.
    pub fn ssh(here: &str, edges: &EdgeList) -> Self {
        let here = here.to_string();
        let keys: HashMap<String, EdgeKey> =
            edges.edges.get(&here).map(|t| t.iter().map(|(p, e)| (p.clone(), e.key)).collect()).unwrap_or_default();
        Self::with_opener(Arc::new(move |peer: &str| {
            let cmd = SshCommand::ssh(peer, &here, keys.get(peer).copied().unwrap_or_default());
            let mut child = Command::new(&cmd.program)
                .args(&cmd.args)
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::null())
                .kill_on_drop(true)
                .spawn()?;
            let out = child.stdout.take().expect("piped");
            let inp = child.stdin.take().expect("piped");
            Ok((Box::new(out) as _, Box::new(inp) as _, Some(child)))
        }))
    }

    pub fn with_opener(open: Opener) -> Self {
        Self { open, channels: Mutex::default(), next_id: AtomicU64::new(1), timeout: Duration::from_secs(60) }
    }

    async fn channel(&self, peer: &str) -> Result<Arc<Channel>, EdgeError> {
        let mut map = self.channels.lock().await;
        if let Some(ch) = map.get(peer) {
            if ch.alive.load(Ordering::SeqCst) {
                return Ok(ch.clone());
            }
        }
        let (r, w, child) = (self.open)(peer)
            .map_err(|e| EdgeError::Unreachable { peer: peer.into(), reason: e.to_string() })?;
        let ch = Channel::start(r, w, child);
        map.insert(peer.into(), ch.clone());
        Ok(ch)
    }

    /// Open the stream `path` on `peer`'s gateway, as `caller`.
    pub async fn open_ws(&self, peer: &str, path: &str, caller: Caller) -> Result<WsPipe, EdgeError> {
        let ch = self.channel(peer).await?;
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (mine, theirs) = WsPipe::pair();
        let WsPipe { tx: to_me, rx: mut from_me } = theirs;
        ch.pending.lock().await.insert(id, Waiter::Stream(to_me.clone()));
        let open = Request { id, name: WS_OPEN.into(), args: serde_json::json!({"path": path}), caller: caller.clone() };
        if let Err(e) = send_line(&ch, &open).await {
            ch.alive.store(false, Ordering::SeqCst);
            ch.pending.lock().await.remove(&id);
            return Err(EdgeError::Unreachable { peer: peer.into(), reason: e.to_string() });
        }
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    // A local socket can send its last frame and close at once: send the frame first.
                    biased;
                    msg = from_me.recv() => match msg {
                        Some(WsMsg::Close(_)) | None => break,
                        Some(m) => {
                            let req = Request { id, name: WS_SEND.into(), args: serde_json::to_value(&m).unwrap_or_default(), caller: caller.clone() };
                            if send_line(&ch, &req).await.is_err() { break; }
                        }
                    },
                    _ = to_me.closed() => break,
                }
            }
            ch.pending.lock().await.remove(&id);
            let _ = send_line(&ch, &Request { id, name: WS_CLOSE.into(), args: Value::Null, caller }).await;
        });
        Ok(mine)
    }

    /// Call tool `name` on `peer` with `args`, as `caller`.
    pub async fn invoke(&self, peer: &str, name: &str, args: Value, caller: Caller) -> Result<Value, EdgeError> {
        let ch = self.channel(peer).await?;
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        ch.pending.lock().await.insert(id, Waiter::Call(tx));
        let sent = send_line(&ch, &Request { id, name: name.into(), args, caller }).await;
        if let Err(e) = sent {
            ch.alive.store(false, Ordering::SeqCst);
            ch.pending.lock().await.remove(&id);
            return Err(EdgeError::Unreachable { peer: peer.into(), reason: e.to_string() });
        }
        match tokio::time::timeout(self.timeout, rx).await {
            Err(_) => {
                ch.pending.lock().await.remove(&id);
                Err(EdgeError::Timeout { peer: peer.into(), timeout: self.timeout })
            }
            Ok(Err(_)) => Err(EdgeError::Unreachable { peer: peer.into(), reason: "the link dropped".into() }),
            Ok(Ok(resp)) if resp.ok => Ok(resp.result.unwrap_or(Value::Null)),
            Ok(Ok(resp)) => Err(EdgeError::Remote(resp.error.unwrap_or_default())),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_user_edge_names_this_node_and_keeps_a_user_master() {
        let cmd = SshCommand::ssh("alcor", "mizar", EdgeKey::User);
        let n = cmd.args.len();
        assert_eq!(cmd.args[n - 5..], ["alcor", "PATH=\"$HOME/.local/bin:$PATH\"", "AWM_EDGE_FROM=mizar", "awm", "edge-serve"]);
        assert!(cmd.args.iter().any(|a| a == "ControlPath=~/.ssh/cm-%C-user"));
        assert!(!cmd.args.iter().any(|a| a == "-i" || a == "IdentitiesOnly=yes"), "a user edge uses the login's own keys");
    }

    #[test]
    fn a_service_edge_logs_in_with_the_service_key_only() {
        let cmd = SshCommand::ssh("alcor", "altair", EdgeKey::Service);
        let at = cmd.args.iter().position(|a| a == "-i").expect("-i");
        assert!(cmd.args[at + 1].ends_with(awm_gateway::door::SERVICE_KEY));
        assert!(cmd.args.iter().any(|a| a == "IdentitiesOnly=yes"));
        assert!(cmd.args.iter().any(|a| a == "ControlPath=~/.ssh/cm-%C-service"), "a service master never shares a user master's socket");
        let peer = cmd.args.iter().position(|a| a == "alcor").unwrap();
        assert!(at < peer, "the options precede the host");
        assert_eq!(cmd.args[cmd.args.len() - 3..], ["AWM_EDGE_FROM=altair", "awm", "edge-serve"]);
    }
}
