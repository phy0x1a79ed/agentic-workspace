//! Tony's approval of his profile for one trusted session on a node outside `profile_nodes`
//! (fleet.md R102 item 6). The browser fetches each grant from the approver and checks its
//! signature itself, so no caller can hand one in.

use std::collections::HashMap;
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;

use awm_trust::grant::{load_public_key, verify_grant, Expect};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::Mutex;

pub const KIND: &str = "browser_profile";
const MINUTES: i64 = 480;

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

#[derive(Clone, Default, Serialize, Deserialize)]
struct Entry {
    request_id: String,
    grant: Option<Value>,
}

/// Each session's last request and its grant, keyed `node/session` as seats are.
pub struct Grants {
    approver: Arc<dyn Approver>,
    store: Option<PathBuf>,
    entries: Mutex<HashMap<String, Entry>>,
}

fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs() as i64)
}

impl Grants {
    pub fn load(approver: Arc<dyn Approver>, store: Option<PathBuf>) -> Self {
        let entries = store.as_ref().and_then(|p| std::fs::read(p).ok()).and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        Grants { approver, store, entries: Mutex::new(entries) }
    }

    fn save(&self, entries: &HashMap<String, Entry>) {
        let Some(path) = &self.store else { return };
        if let Err(e) = std::fs::write(path, serde_json::to_vec(entries).expect("grants serialise")) {
            tracing::warn!(error = %e, path = %path.display(), "could not record the profile grants");
        }
    }

    async fn verified(&self, envelope: &Value, node: &str, payload: &Value) -> anyhow::Result<bool> {
        let keys = self.approver.call("keys", json!({})).await?;
        let key = keys["grant_pub"].as_str().ok_or_else(|| anyhow::anyhow!("the approver named no grant key, so no approval can be checked"))?;
        let key = load_public_key(key)?;
        let expect = Expect { kind: Some(KIND), node: Some(node), subject: None, payload: Some(payload) };
        Ok(verify_grant(envelope, &key, &expect, now()).is_ok())
    }

    /// Whether Tony's approval for `session` on `node` is live. With `ask`, file a request when
    /// none is live or pending.
    pub async fn check(&self, node: &str, session: &str, ask: bool) -> anyhow::Result<Grant> {
        let key = format!("{node}/{session}");
        let payload = json!({"node": node, "session_id": session});
        let mut entries = self.entries.lock().await;
        let entry = entries.get(&key).cloned().unwrap_or_default();
        if let Some(g) = &entry.grant {
            if self.verified(g, node, &payload).await? {
                return Ok(Grant::Live);
            }
        }
        if !entry.request_id.is_empty() {
            let got = self.approver.call("approval_get", json!({"request_id": entry.request_id})).await?;
            match got["status"].as_str() {
                Some("pending") => return Ok(Grant::Pending(entry.request_id)),
                Some("approved") if self.verified(&got["grant"], node, &payload).await? => {
                    entries.insert(key, Entry { grant: Some(got["grant"].clone()), ..entry });
                    self.save(&entries);
                    return Ok(Grant::Live);
                }
                _ => {}
            }
        }
        if !ask {
            return Ok(Grant::None);
        }
        let reason = format!("{session} on {node} asks for your browser profile, with your sign-ins, for {} h", MINUTES / 60);
        let filed = self.approver.call("approval_request", json!({"kind": KIND, "node": node, "reason": reason, "minutes": MINUTES, "payload": payload})).await?;
        let request_id = filed["request_id"].as_str().ok_or_else(|| anyhow::anyhow!("the approver filed no request: {filed}"))?.to_string();
        entries.insert(key, Entry { request_id: request_id.clone(), grant: None });
        self.save(&entries);
        Ok(Grant::Pending(request_id))
    }
}
