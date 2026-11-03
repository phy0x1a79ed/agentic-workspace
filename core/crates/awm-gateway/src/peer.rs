//! How a call with `peer` reaches another node. The trust lane's `awm-edge` implements
//! [`PeerTransport`] over ssh. [`HttpPeers`] posts to a peer gateway's `/invoke`, for tests.

use std::collections::BTreeMap;
use std::future::Future;
use std::pin::Pin;

use awm_proto::CallContext;
use serde_json::Value;

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

    /// The tools `peer` serves, for UUID resolution at load time.
    fn tools<'a>(&'a self, peer: &'a str) -> BoxFuture<'a, Result<Value, PeerError>>;
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
            let url = format!("{}/hub/uuids", self.url(peer)?);
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
}
