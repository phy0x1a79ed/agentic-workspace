//! Tony's approval of his profile for one trusted session on a node outside `profile_nodes`
//! (fleet.md R102 item 6). The browser fetches each grant from the approver and checks its
//! signature itself, so no caller can hand one in.

use std::collections::HashMap;
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;

use awm_trust::grant::{load_public_key, verify_grant, Expect};
use awm_trust::requests::{file_request, Request};
use serde_json::{json, Value};
use tokio::sync::Mutex;

pub const KIND: &str = "browser_profile";
const MINUTES: u32 = 480;

pub type Reply<'a> = Pin<Box<dyn Future<Output = anyhow::Result<Value>> + Send + 'a>>;

/// The approver, reached through the `operations` service.
pub trait Approver: Send + Sync {
    fn call<'a>(&'a self, verb: &'a str, args: Value) -> Reply<'a>;
}

impl Approver for awm_plugin_sdk::Hub {
    fn call<'a>(&'a self, verb: &'a str, args: Value) -> Reply<'a> {
        Box::pin(async move { Ok(awm_plugin_sdk::Hub::call(self, "operations", verb, args, None).await?) })
    }
}

pub enum Grant {
    Live,
    Pending(String),
    None,
}

/// Each session's last request id, keyed `node/session` as seats are. The approver keeps the
/// request and its grant, so the browser asks it again on each check.
pub struct Grants {
    approver: Arc<dyn Approver>,
    store: Option<PathBuf>,
    requests: Mutex<HashMap<String, String>>,
}

pub(crate) fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs() as i64)
}

impl Grants {
    pub fn load(approver: Arc<dyn Approver>, store: Option<PathBuf>) -> Self {
        let requests = store.as_ref().and_then(|p| std::fs::read(p).ok()).and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        Grants { approver, store, requests: Mutex::new(requests) }
    }

    fn save(&self, requests: &HashMap<String, String>) {
        let Some(path) = &self.store else { return };
        if let Err(e) = std::fs::write(path, serde_json::to_vec(requests).expect("grants serialise")) {
            tracing::warn!(error = %e, path = %path.display(), "could not record the profile requests");
        }
    }

    async fn verified(&self, envelope: &Value, node: &str, payload: &Value) -> anyhow::Result<bool> {
        let keys = self.approver.call("keys", json!({})).await?;
        let key = keys["grant_pub"].as_str().ok_or_else(|| anyhow::anyhow!("the approver named no grant key, so no approval can be checked"))?;
        let expect = Expect { kind: Some(KIND), node: Some(node), subject: None, payload: Some(payload) };
        Ok(verify_grant(envelope, &load_public_key(key)?, &expect, now()).is_ok())
    }

    /// Whether Tony's approval for `session` on `node` is live. With `ask`, file a request when
    /// none is live or pending.
    pub async fn check(&self, node: &str, session: &str, ask: bool) -> anyhow::Result<Grant> {
        let key = format!("{node}/{session}");
        let payload = json!({"node": node, "session_id": session});
        let mut requests = self.requests.lock().await;
        if let Some(rid) = requests.get(&key) {
            let got = self.approver.call("approval_get", json!({"request_id": rid})).await?;
            match got["status"].as_str() {
                Some("pending") => return Ok(Grant::Pending(rid.clone())),
                Some("approved") if self.verified(&got["grant"], node, &payload).await? => return Ok(Grant::Live),
                _ => {}
            }
        }
        if !ask {
            return Ok(Grant::None);
        }
        let reason = format!("{session} on {node} asks for your browser profile, with your sign-ins, for {} h", MINUTES / 60);
        let req = Request { kind: KIND, node, reason: &reason, minutes: MINUTES, payload };
        let rid = file_request(&req, |verb, args| async move { self.approver.call(verb, args).await.map_err(|e| e.to_string()) }).await?;
        requests.insert(key, rid.clone());
        self.save(&requests);
        Ok(Grant::Pending(rid))
    }
}
