//! Calls from this plugin to another one, through the gateway (contract § Calling another
//! plugin, § Streams and sessions).

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use awm_proto::WsMsg;
use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::Message;

pub(crate) type WsStream = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

const DEFAULT_HUB_URL: &str = "http://127.0.0.1:7819";
const TOKEN_HEADER: &str = "X-Awm-Service-Token";
/// The keepalive of every subscription socket: the only detector of a half-open connection.
const PING_EVERY: Duration = Duration::from_secs(20);
const PING_TIMEOUT: Duration = Duration::from_secs(20);

/// Why a call through the gateway failed.
#[derive(Debug, Clone, thiserror::Error)]
pub enum HubError {
    /// 404: no plugin serves the tool, or the tool has no such verb.
    #[error("{what}: not installed: {detail}")]
    NotInstalled { what: String, detail: String, body: Value },
    /// 503: the table holds the tool, but it did not come up in time.
    #[error("{what}: not up: {detail}")]
    NotUp { what: String, detail: String, body: Value },
    /// The verb ran and failed.
    #[error("{what}: {error}")]
    Verb { what: String, error: String, error_class: Option<String>, body: Value },
    /// Any other non-2xx answer: a refusal (400, 401, 403) or a gateway fault (502, 504).
    #[error("{what}: HTTP {status}: {detail}")]
    Http { what: String, status: u16, detail: String, body: Value },
    /// The gateway could not be reached, or its answer could not be read.
    #[error("{what}: {reason}")]
    Transport { what: String, reason: String },
}

impl HubError {
    /// The HTTP status the gateway answered with. A verb error is 500. A transport error has none.
    pub fn status(&self) -> Option<u16> {
        match self {
            HubError::NotInstalled { .. } => Some(404),
            HubError::NotUp { .. } => Some(503),
            HubError::Verb { .. } => Some(500),
            HubError::Http { status, .. } => Some(*status),
            HubError::Transport { .. } => None,
        }
    }

    /// The JSON body the gateway answered with, `{"detail": ...}` for its own refusals.
    pub fn body(&self) -> Option<&Value> {
        match self {
            HubError::NotInstalled { body, .. } | HubError::NotUp { body, .. } | HubError::Verb { body, .. } | HubError::Http { body, .. } => Some(body),
            HubError::Transport { .. } => None,
        }
    }

    fn transport(what: &str, e: impl std::fmt::Display) -> Self {
        HubError::Transport { what: what.into(), reason: e.to_string() }
    }

    fn from_status(what: &str, status: u16, body: &Value) -> Self {
        let detail = body.get("detail").unwrap_or(body);
        let text = detail.as_str().map(str::to_string).unwrap_or_else(|| detail.to_string());
        match status {
            404 => HubError::NotInstalled { what: what.into(), detail: text, body: body.clone() },
            503 => HubError::NotUp { what: what.into(), detail: text, body: body.clone() },
            500 if detail.get("error").is_some() => HubError::Verb {
                what: what.into(),
                error: detail["error"].as_str().map(str::to_string).unwrap_or_else(|| detail["error"].to_string()),
                error_class: detail.get("error_class").and_then(Value::as_str).map(str::to_string),
                body: body.clone(),
            },
            _ => HubError::Http { what: what.into(), status, detail: text, body: body.clone() },
        }
    }
}

/// How one call runs: on which node, for whom, and how long it may take.
#[derive(Debug, Clone)]
pub struct CallOpts {
    /// The node the verb runs on. None or `local` runs it here.
    pub peer: Option<String>,
    /// The principal the call acts for, as `X-Awm-As`.
    pub as_: Option<String>,
    /// The session the call acts for: the gateway stamps that session's trust and mode.
    pub caller_pid: Option<u32>,
    pub timeout: Duration,
}

impl Default for CallOpts {
    fn default() -> Self {
        Self { peer: None, as_: None, caller_pid: None, timeout: Duration::from_secs(30) }
    }
}

/// How a lease or a direct session opens.
#[derive(Debug, Clone)]
pub struct LeaseOpts {
    pub kind: String,
    pub peer: Option<String>,
    pub as_: Option<String>,
    /// The node that holds the lease, echoed in the verdict.
    pub node: Option<String>,
    pub timeout: Duration,
}

impl Default for LeaseOpts {
    fn default() -> Self {
        Self { kind: "lease".into(), peer: None, as_: None, node: None, timeout: Duration::from_secs(10) }
    }
}

/// The gateway, as a client sees it. Cloning shares the connection pool.
#[derive(Clone)]
pub struct Hub {
    base: String,
    token_file: Option<PathBuf>,
    http: reqwest::Client,
}

fn remote(peer: Option<&str>) -> Option<&str> {
    peer.map(str::trim).filter(|p| !p.is_empty() && *p != "local")
}

fn quote(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

impl Hub {
    /// The gateway at `AWM_HUB_URL`, naming this service by the token in `AWM_HUB_TOKEN_FILE`.
    pub fn from_env() -> Self {
        let base = std::env::var("AWM_HUB_URL").ok().filter(|u| !u.trim().is_empty()).unwrap_or_else(|| DEFAULT_HUB_URL.into());
        let token_file = std::env::var_os("AWM_HUB_TOKEN_FILE").filter(|p| !p.is_empty()).map(PathBuf::from);
        Self::new(&base, token_file)
    }

    /// The gateway at `base`. The token file is reread on each request, since each spawn gets a fresh one.
    pub fn new(base: &str, token_file: Option<PathBuf>) -> Self {
        let http = reqwest::Client::builder().no_proxy().build().expect("reqwest client builds");
        Self { base: base.trim().trim_end_matches('/').to_string(), token_file, http }
    }

    pub fn base_url(&self) -> &str {
        &self.base
    }

    fn ws_base(&self) -> String {
        self.base.replacen("https://", "wss://", 1).replacen("http://", "ws://", 1)
    }

    /// The file that holds this service's token, to hand to a child process as `AWM_HUB_TOKEN_FILE`.
    pub fn token_file(&self) -> Option<&std::path::Path> {
        self.token_file.as_deref()
    }

    /// This service's token, or None outside a spawn.
    pub fn token(&self) -> Option<String> {
        let path = self.token_file.as_ref()?;
        std::fs::read_to_string(path).ok().map(|t| t.trim().to_string()).filter(|t| !t.is_empty())
    }

    fn post(&self, path: &str) -> reqwest::RequestBuilder {
        let req = self.http.post(format!("{}{path}", self.base));
        match self.token() {
            Some(t) => req.header(TOKEN_HEADER, t),
            None => req,
        }
    }

    fn get(&self, path: &str) -> reqwest::RequestBuilder {
        let req = self.http.get(format!("{}{path}", self.base));
        match self.token() {
            Some(t) => req.header(TOKEN_HEADER, t),
            None => req,
        }
    }

    /// Call `tool`'s `verb` and return its decoded result. A null result reads as `{}`.
    pub async fn call(&self, tool: &str, verb: &str, args: Value, peer: Option<&str>) -> Result<Value, HubError> {
        self.call_with(tool, verb, args, &CallOpts { peer: peer.map(str::to_string), ..CallOpts::default() }).await
    }

    /// [`Hub::call`] with every option.
    pub async fn call_with(&self, tool: &str, verb: &str, args: Value, opts: &CallOpts) -> Result<Value, HubError> {
        let mut inner = json!({"verb": verb, "args": if args.is_null() { json!({}) } else { args }});
        if let Some(peer) = remote(opts.peer.as_deref()) {
            inner["peer"] = json!(peer);
        }
        let what = format!("{tool}.{verb}");
        let (status, body) = self.invoke_raw(&json!({"name": tool, "args": inner}), opts).await?;
        if !(200..300).contains(&status) {
            return Err(HubError::from_status(&what, status, &body));
        }
        let result = match body {
            Value::Object(mut m) if m.contains_key("result") => m.remove("result").unwrap_or(Value::Null),
            other => other,
        };
        Ok(match result {
            Value::String(s) => serde_json::from_str(&s).unwrap_or(Value::String(s)),
            Value::Null => json!({}),
            other => other,
        })
    }

    /// `POST /invoke` with `body` as sent, answering the status and the JSON body as received.
    pub async fn invoke_raw(&self, body: &Value, opts: &CallOpts) -> Result<(u16, Value), HubError> {
        let what = body.get("name").and_then(Value::as_str).unwrap_or("invoke").to_string();
        let mut req = self.post("/invoke").timeout(opts.timeout).json(body);
        if let Some(as_) = &opts.as_ {
            req = req.header("X-Awm-As", as_);
        }
        if let Some(pid) = opts.caller_pid {
            req = req.header("X-Awm-Caller-Pid", pid.to_string());
        }
        let resp = req.send().await.map_err(|e| HubError::transport(&what, e))?;
        let status = resp.status().as_u16();
        let text = resp.text().await.map_err(|e| HubError::transport(&what, e))?;
        let body = if text.is_empty() { Value::Null } else { serde_json::from_str(&text).unwrap_or(Value::String(text)) };
        Ok((status, body))
    }

    /// The owner of a tool, `{name, major, plugin, peer}`, or None when no plugin serves it.
    pub async fn owner(&self, tool: &str) -> Result<Option<Value>, HubError> {
        let what = format!("hub/tool/{tool}");
        let resp = self.get(&format!("/hub/tool/{}", quote(tool))).timeout(Duration::from_secs(10)).send().await.map_err(|e| HubError::transport(&what, e))?;
        if resp.status().as_u16() == 404 {
            return Ok(None);
        }
        read_json(&what, resp).await.map(Some)
    }

    /// Every provider of `contract` at `major` now, as `[{name, major, plugin, peer}]`.
    pub async fn providers(&self, contract: &str, major: u32) -> Result<Vec<Value>, HubError> {
        let what = format!("hub/contract/{contract}@{major}");
        let resp = self
            .get(&format!("/hub/contract/{}@{major}", quote(contract)))
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .map_err(|e| HubError::transport(&what, e))?;
        match read_json(&what, resp).await? {
            Value::Array(a) => Ok(a),
            _ => Ok(vec![]),
        }
    }

    /// Whether a plugin on this node or a peer provides `contract` at `major`.
    pub async fn provided(&self, contract: &str, major: u32) -> Result<bool, HubError> {
        Ok(!self.providers(contract, major).await?.is_empty())
    }

    /// Return once `contract` at `major` has a provider, or the gateway cannot say.
    pub async fn until_provided(&self, contract: &str, major: u32, idle: Duration) {
        let mut told = false;
        loop {
            match self.provided(contract, major).await {
                Ok(true) | Err(_) => return,
                Ok(false) => {}
            }
            if !told {
                tracing::info!("no plugin provides {contract}@{major} here; checking every {}s", idle.as_secs());
                told = true;
            }
            tokio::time::sleep(idle).await;
        }
    }

    /// `POST /usage`: count one use the gateway cannot see. It fills `v` and `ts`.
    pub async fn post_usage(&self, event: &awm_proto::UsageEvent) -> Result<(), HubError> {
        let resp = self.post("/usage").timeout(Duration::from_secs(2)).json(event).send().await.map_err(|e| HubError::transport("usage", e))?;
        read_json("usage", resp).await.map(|_| ())
    }

    async fn connect(&self, what: &str, path: &str, as_: Option<&str>, timeout: Duration) -> Result<WsStream, HubError> {
        let url = format!("{}{path}", self.ws_base());
        let mut req = url.as_str().into_client_request().map_err(|e| HubError::transport(what, e))?;
        if let Some(t) = self.token() {
            req.headers_mut().insert(TOKEN_HEADER, t.parse().map_err(|_| HubError::transport(what, "bad token"))?);
        }
        if let Some(a) = as_ {
            req.headers_mut().insert("X-Awm-As", a.parse().map_err(|_| HubError::transport(what, "bad X-Awm-As"))?);
        }
        connect_ws(what, req, timeout).await
    }

    /// One connection's worth of a service's emit topic. A dropped socket ends it; the caller
    /// reconnects, or uses [`crate::SupervisedSubscription`].
    pub async fn subscribe(&self, service: &str, topic: &str, peer: Option<&str>, as_: Option<&str>) -> Result<Subscription, HubError> {
        let what = format!("emit {service}/{topic}");
        let path = format!("/hub/emit/{}/{}{}", quote(service), quote(topic), peer_query(peer));
        let ws = self.connect(&what, &path, as_, Duration::from_secs(10)).await?;
        Ok(Subscription { ws, last_rx: Instant::now(), pinged: false })
    }

    /// Open a direct session of `kind` on `service`, sending `init` as its first frame.
    pub async fn open_session(&self, service: &str, kind: &str, init: &Value, peer: Option<&str>, as_: Option<&str>, timeout: Duration) -> Result<Bridge, HubError> {
        let what = format!("session {service}/{kind}");
        let path = format!("/hub/session/{}/{}{}", quote(service), quote(kind), peer_query(peer));
        let ws = self.connect(&what, &path, as_, timeout).await?;
        let mut bridge = Bridge { ws, what };
        bridge.send_json(init).await?;
        Ok(bridge)
    }

    /// Hold a direct-session slot on `service` for `host`: the open socket is the lease.
    pub async fn acquire_lease(&self, service: &str, host: &str, opts: &LeaseOpts) -> Result<Lease, HubError> {
        let mut init = json!({"host": host});
        if let Some(node) = &opts.node {
            init["node"] = json!(node);
        }
        let mut bridge = self.open_session(service, &opts.kind, &init, opts.peer.as_deref(), opts.as_.as_deref(), opts.timeout).await?;
        let first = tokio::time::timeout(Duration::from_secs(10), bridge.recv()).await;
        let frame = match first {
            Ok(Some(WsMsg::Text(t))) => serde_json::from_str::<Value>(&t).unwrap_or(Value::Null),
            Ok(Some(WsMsg::Close(reason))) => {
                let detail = format!("lease grant not received: {}", reason.unwrap_or_default());
                return Err(HubError::Http { what: bridge.what.clone(), status: 502, body: json!({"detail": detail}), detail })
            }
            Ok(_) => Value::Null,
            Err(_) => {
                let detail = "lease grant not received: timed out".to_string();
                return Err(HubError::Http { what: bridge.what.clone(), status: 502, body: json!({"detail": detail}), detail });
            }
        };
        Ok(Lease {
            status: frame.get("lease").and_then(Value::as_str).unwrap_or("error").to_string(),
            reason: frame.get("reason").and_then(Value::as_str).map(str::to_string),
            node: opts.node.clone(),
            bridge: Some(bridge),
        })
    }
}

fn peer_query(peer: Option<&str>) -> String {
    remote(peer).map(|p| format!("?peer={}", quote(p))).unwrap_or_default()
}

async fn read_json(what: &str, resp: reqwest::Response) -> Result<Value, HubError> {
    let status = resp.status().as_u16();
    let text = resp.text().await.map_err(|e| HubError::transport(what, e))?;
    let body: Value = if text.is_empty() { Value::Null } else { serde_json::from_str(&text).unwrap_or(Value::String(text)) };
    if (200..300).contains(&status) {
        Ok(body)
    } else {
        Err(HubError::from_status(what, status, &body))
    }
}

pub(crate) async fn connect_ws(what: &str, req: tokio_tungstenite::tungstenite::handshake::client::Request, timeout: Duration) -> Result<WsStream, HubError> {
    use tokio_tungstenite::tungstenite::Error;
    match tokio::time::timeout(timeout, tokio_tungstenite::connect_async(req)).await {
        Err(_) => Err(HubError::transport(what, "WebSocket open timed out")),
        Ok(Ok((ws, _))) => Ok(ws),
        Ok(Err(Error::Http(resp))) => {
            let status = resp.status().as_u16();
            let body = resp.body().as_deref().map(|b| String::from_utf8_lossy(b).into_owned()).unwrap_or_default();
            let body = serde_json::from_str(&body).unwrap_or(Value::String(body));
            Err(HubError::from_status(what, status, &body))
        }
        Ok(Err(e)) => Err(HubError::transport(what, e)),
    }
}

/// One connection to an emit topic. Each [`Subscription::next`] is one payload.
pub struct Subscription {
    ws: WsStream,
    last_rx: Instant,
    pinged: bool,
}

impl Subscription {
    /// The next payload, decoded from JSON (a frame that is not JSON stays a string). None once
    /// the socket closes or stops answering pings.
    pub async fn next(&mut self) -> Option<Value> {
        loop {
            let wait = if self.pinged { PING_TIMEOUT } else { PING_EVERY };
            match tokio::time::timeout(wait.saturating_sub(self.last_rx.elapsed()), self.ws.next()).await {
                Err(_) if self.pinged => return None,
                Err(_) => {
                    self.ws.send(Message::Ping(Vec::new().into())).await.ok()?;
                    self.pinged = true;
                    self.last_rx = Instant::now();
                }
                Ok(None) | Ok(Some(Err(_))) => return None,
                Ok(Some(Ok(msg))) => {
                    self.last_rx = Instant::now();
                    self.pinged = false;
                    match msg {
                        Message::Text(t) => return Some(serde_json::from_str(&t).unwrap_or_else(|_| Value::String(t.to_string()))),
                        Message::Close(_) => return None,
                        _ => {}
                    }
                }
            }
        }
    }
}

/// One direct session's socket: a client's end from [`Hub::open_session`], or a service's end
/// from [`crate::Session::open_bridge`].
pub struct Bridge {
    ws: WsStream,
    what: String,
}

impl Bridge {
    pub(crate) fn new(ws: WsStream, what: String) -> Self {
        Self { ws, what }
    }

    pub async fn send(&mut self, msg: WsMsg) -> Result<(), HubError> {
        let m = match msg {
            WsMsg::Text(t) => Message::Text(t.into()),
            WsMsg::Binary(b) => Message::Binary(b.into()),
            WsMsg::Close(_) => Message::Close(None),
        };
        self.ws.send(m).await.map_err(|e| HubError::transport(&self.what, e))
    }

    pub async fn send_json(&mut self, value: &impl Serialize) -> Result<(), HubError> {
        let text = serde_json::to_string(value).map_err(|e| HubError::transport(&self.what, e))?;
        self.send(WsMsg::Text(text)).await
    }

    /// The next text or binary message, or `Close` with the peer's reason. None after the socket ends.
    pub async fn recv(&mut self) -> Option<WsMsg> {
        loop {
            match self.ws.next().await? {
                Ok(Message::Text(t)) => return Some(WsMsg::Text(t.to_string())),
                Ok(Message::Binary(b)) => return Some(WsMsg::Binary(b.to_vec())),
                Ok(Message::Close(frame)) => return Some(WsMsg::Close(frame.map(|f| f.reason.to_string()))),
                Ok(_) => {}
                Err(_) => return None,
            }
        }
    }

    pub async fn close(mut self) {
        let _ = self.ws.close(None).await;
    }

    /// The raw socket, to split it for a duplex relay.
    pub fn into_inner(self) -> tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>> {
        self.ws
    }
}

/// A held direct-session slot. Report a [`Lease::verdict`], or drop it: the owning service
/// reads a drop without a verdict as a failure.
pub struct Lease {
    /// `granted`, `busy`, `locked` or `error`: the owning service's first frame.
    pub status: String,
    pub reason: Option<String>,
    node: Option<String>,
    bridge: Option<Bridge>,
}

impl Lease {
    pub fn granted(&self) -> bool {
        self.status == "granted"
    }

    /// Report the outcome on the held socket, then close it: a clean release.
    pub async fn verdict(&mut self, ok: bool, reason: &str) {
        let Some(mut bridge) = self.bridge.take() else { return };
        let mut frame = json!({"verdict": if ok { "ok" } else { "fail" }, "reason": reason});
        if let Some(node) = &self.node {
            frame["node"] = json!(node);
        }
        let _ = bridge.send_json(&frame).await;
        bridge.close().await;
    }

    /// Drop the socket without a verdict.
    pub async fn close(&mut self) {
        if let Some(bridge) = self.bridge.take() {
            bridge.close().await;
        }
    }
}

/// A short-lived, positive-only cache over [`Hub::call`], for validating a reference by
/// calling its owner. A falsy result (null, false, empty) is returned but never kept.
pub struct RefCache {
    ttl: Duration,
    store: Mutex<HashMap<String, (Instant, Value)>>,
}

impl RefCache {
    pub fn new(ttl: Duration) -> Self {
        Self { ttl, store: Mutex::new(HashMap::new()) }
    }

    fn key(tool: &str, verb: &str, args: &Value, peer: Option<&str>) -> String {
        format!("{}\u{0}{tool}\u{0}{verb}\u{0}{args}", peer.unwrap_or(""))
    }

    /// A cached positive result within the TTL, else a fresh call.
    pub async fn validate(&self, hub: &Hub, tool: &str, verb: &str, args: Value, peer: Option<&str>) -> Result<Value, HubError> {
        let key = Self::key(tool, verb, &args, peer);
        if let Some((at, v)) = self.store.lock().unwrap_or_else(|p| p.into_inner()).get(&key) {
            if at.elapsed() < self.ttl {
                return Ok(v.clone());
            }
        }
        let result = hub.call(tool, verb, args, peer).await?;
        if truthy(&result) {
            self.store.lock().unwrap_or_else(|p| p.into_inner()).insert(key, (Instant::now(), result.clone()));
        }
        Ok(result)
    }

    /// Drop every entry, or only those of `tool`.
    pub fn invalidate(&self, tool: Option<&str>) {
        let mut store = self.store.lock().unwrap_or_else(|p| p.into_inner());
        match tool {
            None => store.clear(),
            Some(t) => store.retain(|k, _| k.split('\u{0}').nth(1) != Some(t)),
        }
    }
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn errors_classify_by_status() {
        let e = HubError::from_status("x.y", 404, &json!({"detail": "x not installed"}));
        assert!(matches!(e, HubError::NotInstalled { .. }) && e.status() == Some(404));
        assert!(matches!(HubError::from_status("x.y", 503, &json!({"detail": "not up"})), HubError::NotUp { .. }));
        let v = HubError::from_status("x.y", 500, &json!({"detail": {"error": "boom", "error_class": "ValueError"}}));
        match v {
            HubError::Verb { error, error_class, .. } => assert_eq!((error.as_str(), error_class.as_deref()), ("boom", Some("ValueError"))),
            other => panic!("{other:?}"),
        }
        let refused = HubError::from_status("x.y", 403, &json!({"detail": "no"}));
        assert_eq!((refused.status(), refused.body()), (Some(403), Some(&json!({"detail": "no"}))));
    }

    #[test]
    fn quoting_and_peers() {
        assert_eq!(quote("a b/c"), "a%20b%2Fc");
        assert_eq!(peer_query(Some("local")), "");
        assert_eq!(peer_query(Some("mira")), "?peer=mira");
        assert!(!truthy(&json!({})) && truthy(&json!({"ok": true})));
    }
}
