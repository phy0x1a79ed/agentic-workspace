//! A Chrome DevTools Protocol client shared by the browser plugins.
//!
//! [`Connection`] is one DevTools websocket: id-matched replies, flat sessions through
//! `sessionId`, and unsolicited events on an optional channel. [`Browser`] holds the
//! browser-level connection of one endpoint and dials it again once Chrome restarts.

use std::collections::HashMap;
use std::net::TcpListener;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::Message;

pub const CALL_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, thiserror::Error)]
pub enum CdpError {
    /// The method answered with an `error` object.
    #[error("{method}: {message}")]
    Method { method: String, message: String, error: Value },
    #[error("the browser connection closed")]
    Closed,
    #[error("{method}: no answer in {secs}s")]
    Timeout { method: String, secs: u64 },
    #[error("{0}")]
    Endpoint(String),
}

type Waiting = Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>;

/// One DevTools websocket.
pub struct Connection {
    out: mpsc::UnboundedSender<Message>,
    waiting: Waiting,
    ids: AtomicU64,
    closed: Arc<std::sync::atomic::AtomicBool>,
}

fn http() -> reqwest::Client {
    reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(10)).build().expect("reqwest client builds")
}

fn endpoint(e: impl std::fmt::Display) -> CdpError {
    CdpError::Endpoint(e.to_string())
}

impl Connection {
    /// Dial `ws_url`, dropping unsolicited events.
    pub async fn connect(ws_url: &str) -> Result<Self, CdpError> {
        Ok(Self::connect_with_events(ws_url).await?.0)
    }

    /// Dial `ws_url`. Each frame that carries a `method` and answers no call goes to the receiver.
    pub async fn connect_with_events(ws_url: &str) -> Result<(Self, mpsc::UnboundedReceiver<Value>), CdpError> {
        let mut cfg = WebSocketConfig::default();
        cfg.max_message_size = None;
        cfg.max_frame_size = None;
        let dial = tokio_tungstenite::connect_async_with_config(ws_url, Some(cfg), false);
        let (ws, _) = tokio::time::timeout(Duration::from_secs(15), dial)
            .await
            .map_err(|_| CdpError::Endpoint(format!("{ws_url}: no websocket in 15s")))?
            .map_err(endpoint)?;
        let (mut sink, mut stream) = ws.split();
        let (out, mut out_rx) = mpsc::unbounded_channel::<Message>();
        let (events, events_rx) = mpsc::unbounded_channel();
        let waiting: Waiting = Arc::default();
        let closed = Arc::new(std::sync::atomic::AtomicBool::new(false));
        tokio::spawn(async move {
            while let Some(m) = out_rx.recv().await {
                if sink.send(m).await.is_err() {
                    break;
                }
            }
            let _ = sink.close().await;
        });
        let (w, c) = (waiting.clone(), closed.clone());
        tokio::spawn(async move {
            while let Some(Ok(frame)) = stream.next().await {
                let text = match frame {
                    Message::Text(t) => t.to_string(),
                    Message::Binary(b) => String::from_utf8_lossy(&b).into_owned(),
                    Message::Close(_) => break,
                    _ => continue,
                };
                let Ok(msg) = serde_json::from_str::<Value>(&text) else { continue };
                let reply = msg.get("id").and_then(Value::as_u64).and_then(|id| w.lock().unwrap().remove(&id));
                match reply {
                    Some(tx) => {
                        let _ = tx.send(msg);
                    }
                    None if msg.get("method").is_some() => {
                        let _ = events.send(msg);
                    }
                    None => {}
                }
            }
            c.store(true, Ordering::SeqCst);
            w.lock().unwrap().clear();
        });
        Ok((Connection { out, waiting, ids: AtomicU64::new(1), closed }, events_rx))
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst) || self.out.is_closed()
    }

    /// Run `method`, on the flat `session` when one is given, and return its `result`.
    pub async fn send(&self, method: &str, params: Value, session: Option<&str>) -> Result<Value, CdpError> {
        self.send_timeout(method, params, session, CALL_TIMEOUT).await
    }

    pub async fn send_timeout(&self, method: &str, params: Value, session: Option<&str>, timeout: Duration) -> Result<Value, CdpError> {
        if self.is_closed() {
            return Err(CdpError::Closed);
        }
        let id = self.ids.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        self.waiting.lock().unwrap().insert(id, tx);
        let params = if params.is_null() { json!({}) } else { params };
        let mut frame = json!({"id": id, "method": method, "params": params});
        if let Some(s) = session {
            frame["sessionId"] = json!(s);
        }
        if self.out.send(Message::Text(frame.to_string().into())).is_err() {
            self.waiting.lock().unwrap().remove(&id);
            return Err(CdpError::Closed);
        }
        let msg = match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(m)) => m,
            Ok(Err(_)) => return Err(CdpError::Closed),
            Err(_) => {
                self.waiting.lock().unwrap().remove(&id);
                return Err(CdpError::Timeout { method: method.into(), secs: timeout.as_secs() });
            }
        };
        if let Some(err) = msg.get("error") {
            let message = err.get("message").and_then(Value::as_str).unwrap_or("error").to_string();
            return Err(CdpError::Method { method: method.into(), message, error: err.clone() });
        }
        Ok(msg.get("result").cloned().unwrap_or_else(|| json!({})))
    }

    pub fn close(&self) {
        let _ = self.out.send(Message::Close(None));
    }
}

impl Drop for Connection {
    fn drop(&mut self) {
        self.close();
    }
}

/// `GET <base>/json/version`.
pub async fn version(base: &str) -> Result<Value, CdpError> {
    get_json(base, "/json/version").await
}

/// `GET <base>/json/protocol`: the running Chrome's own command catalog.
pub async fn protocol(base: &str) -> Result<Value, CdpError> {
    get_json(base, "/json/protocol").await
}

async fn get_json(base: &str, path: &str) -> Result<Value, CdpError> {
    let url = format!("{}{path}", base.trim_end_matches('/'));
    let resp = http().get(&url).send().await.map_err(endpoint)?;
    if !resp.status().is_success() {
        return Err(CdpError::Endpoint(format!("{url}: HTTP {}", resp.status())));
    }
    resp.json().await.map_err(endpoint)
}

/// Poll `/json/version` until Chrome answers.
pub async fn wait_ready(base: &str, timeout: Duration) -> Result<(), CdpError> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if version(base).await.is_ok() {
            return Ok(());
        }
        if tokio::time::Instant::now() > deadline {
            return Err(CdpError::Endpoint(format!("Chrome at {base} not ready in {}s", timeout.as_secs())));
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

/// `ws://<host:port of base><path>`. Chrome advertises the port it bound, which behind a
/// container's bridge is not the published one: only the path is authoritative.
pub fn rewrite_ws(base: &str, advertised: &str) -> String {
    let authority = base.split("://").nth(1).unwrap_or(base).split('/').next().unwrap_or("");
    let path = advertised.split("://").nth(1).and_then(|rest| rest.find('/').map(|i| &rest[i..])).unwrap_or("/");
    format!("ws://{authority}{path}")
}

/// The browser-level websocket of `base`.
pub async fn browser_ws(base: &str) -> Result<String, CdpError> {
    let v = version(base).await?;
    let adv = v.get("webSocketDebuggerUrl").and_then(Value::as_str).ok_or_else(|| CdpError::Endpoint(format!("{base}: /json/version names no webSocketDebuggerUrl")))?;
    Ok(rewrite_ws(base, adv))
}

/// The page-level websocket of tab `target` at `base`.
pub fn page_ws(base: &str, target: &str) -> String {
    rewrite_ws(base, &format!("ws://x/devtools/page/{target}"))
}

/// The browser-level connection of one endpoint, dialled again after Chrome restarts.
pub struct Browser {
    base: String,
    conn: tokio::sync::Mutex<Option<Arc<Connection>>>,
    generation: AtomicU64,
}

impl Browser {
    pub fn new(base: &str) -> Self {
        Browser { base: base.trim_end_matches('/').to_string(), conn: tokio::sync::Mutex::new(None), generation: AtomicU64::new(0) }
    }

    pub fn base(&self) -> &str {
        &self.base
    }

    /// How many times the connection was dialled. Flat session ids die with each one.
    pub fn generation(&self) -> u64 {
        self.generation.load(Ordering::SeqCst)
    }

    pub async fn connection(&self) -> Result<Arc<Connection>, CdpError> {
        let mut slot = self.conn.lock().await;
        if let Some(c) = slot.as_ref().filter(|c| !c.is_closed()) {
            return Ok(c.clone());
        }
        let c = Arc::new(Connection::connect(&browser_ws(&self.base).await?).await?);
        self.generation.fetch_add(1, Ordering::SeqCst);
        tracing::info!(base = %self.base, "dialled the browser's DevTools socket");
        *slot = Some(c.clone());
        Ok(c)
    }

    pub async fn send(&self, method: &str, params: Value, session: Option<&str>) -> Result<Value, CdpError> {
        self.connection().await?.send(method, params, session).await
    }
}

/// A free TCP port on loopback. Racy by nature: a collision fails the launch.
pub fn free_port() -> std::io::Result<u16> {
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?.port())
}

/// The Chrome binary: `RLM_CHROME_BIN`, else the first of the usual names on `PATH`.
pub fn find_chrome() -> Option<std::path::PathBuf> {
    if let Some(p) = std::env::var_os("RLM_CHROME_BIN").filter(|p| !p.is_empty()) {
        return Some(p.into());
    }
    let path = std::env::var_os("PATH")?;
    ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
        .iter()
        .flat_map(|name| std::env::split_paths(&path).map(move |d| d.join(name)))
        .find(|p| is_executable(p))
}

fn is_executable(p: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

/// The `docker` CLI.
#[derive(Debug, Clone)]
pub struct Docker {
    pub bin: String,
}

impl Default for Docker {
    fn default() -> Self {
        Docker { bin: "docker".into() }
    }
}

impl Docker {
    /// Run `docker <args>` and return its trimmed stdout. A non-zero exit is an error carrying stderr.
    pub async fn run(&self, args: &[&str]) -> anyhow::Result<String> {
        let out = tokio::process::Command::new(&self.bin).args(args).kill_on_drop(true).output().await?;
        if !out.status.success() {
            anyhow::bail!("{} {}: {}", self.bin, args.first().unwrap_or(&""), String::from_utf8_lossy(&out.stderr).trim());
        }
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    }

    /// `docker rm -f <name>`, ignoring a container that is already gone.
    pub async fn remove(&self, name: &str) {
        if let Err(e) = self.run(&["rm", "-f", name]).await {
            tracing::debug!(%name, error = %e, "docker rm");
        }
    }

    /// The names of the containers, running or not, whose name starts with `prefix`.
    pub async fn names(&self, prefix: &str) -> anyhow::Result<Vec<String>> {
        let out = self.run(&["ps", "-a", "--format", "{{.Names}}", "--filter", &format!("name=^{prefix}")]).await?;
        Ok(out.lines().map(str::trim).filter(|l| l.starts_with(prefix)).map(str::to_string).collect())
    }
}

#[cfg(test)]
mod tests;
