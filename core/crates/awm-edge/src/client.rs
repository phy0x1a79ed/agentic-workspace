//! The calling side: one warm channel per peer.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use awm_proto::Caller;
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::{oneshot, Mutex};

use crate::frame::{Request, Response};

#[derive(Debug, thiserror::Error)]
pub enum EdgeError {
    #[error("{peer} is unreachable: {reason}")]
    Unreachable { peer: String, reason: String },
    #[error("{peer} did not answer within {0:?}", .timeout)]
    Timeout { peer: String, timeout: Duration },
    #[error("{0}")]
    Remote(String),
}

/// How to open the channel to a peer. The default is ssh with a shared master
/// connection (`ControlMaster`, `ControlPersist`) and a keep-alive. `AWM_EDGE_SSH`
/// names another program to run in its place, with the same arguments.
#[derive(Debug, Clone)]
pub struct SshCommand {
    pub program: String,
    pub args: Vec<String>,
}

impl SshCommand {
    pub fn ssh(peer: &str) -> Self {
        let opts = [
            "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/cm-%C",
            "-o", "ControlPersist=10m", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        ];
        let mut args: Vec<String> = opts.iter().map(|s| s.to_string()).collect();
        args.extend([peer.to_string(), "awm".into(), "edge-serve".into()]);
        let program = std::env::var("AWM_EDGE_SSH").ok().filter(|p| !p.trim().is_empty()).unwrap_or_else(|| "ssh".into());
        Self { program, args }
    }
}

type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Response>>>>;

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
                        if let Some(tx) = p.lock().await.remove(&resp.id) {
                            let _ = tx.send(resp);
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

pub struct EdgeClient {
    open: Opener,
    channels: Mutex<HashMap<String, Arc<Channel>>>,
    next_id: AtomicU64,
    pub timeout: Duration,
}

impl EdgeClient {
    /// A client that reaches each peer by `ssh <peer> awm edge-serve`.
    pub fn ssh() -> Self {
        Self::with_opener(Arc::new(|peer: &str| {
            let cmd = SshCommand::ssh(peer);
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

    /// Call tool `name` on `peer` with `args`, as `caller`.
    pub async fn invoke(&self, peer: &str, name: &str, args: Value, caller: Caller) -> Result<Value, EdgeError> {
        let ch = self.channel(peer).await?;
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        ch.pending.lock().await.insert(id, tx);
        let mut line = serde_json::to_vec(&Request { id, name: name.into(), args, caller }).expect("json");
        line.push(b'\n');
        let sent = {
            let mut w = ch.writer.lock().await;
            match w.write_all(&line).await {
                Ok(()) => w.flush().await,
                Err(e) => Err(e),
            }
        };
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
