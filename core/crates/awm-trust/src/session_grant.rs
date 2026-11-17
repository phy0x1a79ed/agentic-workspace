//! Tony's approval of one trusted session for something its node does not get by default,
//! such as his browser profile (fleet.md R102 item 6) or his mail. The service fetches each
//! grant from the approver and checks its signature itself, so no caller can hand one in.

use std::collections::HashMap;
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;

use serde_json::{json, Value};
use tokio::sync::Mutex;

use crate::grant::{load_public_key, verify_grant, Expect};
use crate::requests::{file_request, Request};

pub type Reply<'a> = Pin<Box<dyn Future<Output = anyhow::Result<Value>> + Send + 'a>>;

/// The approver, reached through the `operations` service.
pub trait Approver: Send + Sync {
    fn call<'a>(&'a self, verb: &'a str, args: Value) -> Reply<'a>;
}

pub enum Approval {
    Live,
    Pending(String),
    None,
}

/// What a grant opens: its request kind, how long it lasts and how Tony's card names it.
pub struct Kind {
    pub kind: &'static str,
    pub minutes: u32,
    /// Finishes "<session> on <node> asks for …".
    pub what: &'static str,
}

/// Each session's last request id for one kind, keyed `node/session` as seats are. The
/// approver keeps the request and its grant, so each check asks it again.
pub struct SessionGrants {
    kind: Kind,
    approver: Arc<dyn Approver>,
    store: Option<PathBuf>,
    requests: Mutex<HashMap<String, String>>,
}

pub fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs() as i64)
}

impl SessionGrants {
    pub fn load(kind: Kind, approver: Arc<dyn Approver>, store: Option<PathBuf>) -> Self {
        let requests = store.as_ref().and_then(|p| std::fs::read(p).ok()).and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        SessionGrants { kind, approver, store, requests: Mutex::new(requests) }
    }

    fn save(&self, requests: &HashMap<String, String>) {
        let Some(path) = &self.store else { return };
        if let Err(e) = std::fs::write(path, serde_json::to_vec(requests).expect("grants serialise")) {
            tracing::warn!(error = %e, path = %path.display(), kind = self.kind.kind, "could not record the grant requests");
        }
    }

    async fn verified(&self, envelope: &Value, node: &str, payload: &Value) -> anyhow::Result<bool> {
        let keys = self.approver.call("keys", json!({})).await?;
        let key = keys["grant_pub"].as_str().ok_or_else(|| anyhow::anyhow!("the approver named no grant key, so no approval can be checked"))?;
        let expect = Expect { kind: Some(self.kind.kind), node: Some(node), subject: None, payload: Some(payload) };
        Ok(verify_grant(envelope, &load_public_key(key)?, &expect, now()).is_ok())
    }

    /// Whether Tony's approval for `session` on `node` is live. With `ask`, file a request when
    /// none is live or pending.
    pub async fn check(&self, node: &str, session: &str, ask: bool) -> anyhow::Result<Approval> {
        let key = format!("{node}/{session}");
        let payload = json!({"node": node, "session_id": session});
        let mut requests = self.requests.lock().await;
        if let Some(rid) = requests.get(&key) {
            let got = self.approver.call("approval_get", json!({"request_id": rid})).await?;
            match got["status"].as_str() {
                Some("pending") => return Ok(Approval::Pending(rid.clone())),
                Some("approved") if self.verified(&got["grant"], node, &payload).await? => return Ok(Approval::Live),
                _ => {}
            }
        }
        if !ask {
            return Ok(Approval::None);
        }
        let Kind { kind, minutes, what } = self.kind;
        let reason = format!("{session} on {node} asks for {what}, for {} h", minutes / 60);
        let req = Request { kind, node, reason: &reason, minutes, payload };
        let rid = file_request(&req, |verb, mut args| async move {
            args["session_id"] = json!(session);
            self.approver.call(verb, args).await.map_err(|e| e.to_string())
        }).await?;
        requests.insert(key, rid.clone());
        self.save(&requests);
        Ok(Approval::Pending(rid))
    }
}

/// The approver as operations answers it, with a fixed grant key. `approve` signs a grant.
#[cfg(feature = "testing")]
pub mod testing {
    use std::collections::HashMap;
    use std::sync::Mutex;

    use serde_json::{json, Value};

    use super::{now, Approver, Reply};
    use crate::grant::{b64u, payload_digest, sign_envelope};

    const SIGNER: [u8; 32] = [7; 32];

    #[derive(Default)]
    pub struct FakeApprover {
        pub requests: Mutex<Vec<(String, Value)>>,
        decided: Mutex<HashMap<String, (String, Value)>>,
    }

    impl FakeApprover {
        pub fn approve(&self, rid: &str, kind: &str, node: &str, session: &str, minutes: i64) {
            let now = now();
            let body = json!({"grant_id": "g1", "request_id": rid, "kind": kind, "node": node, "subject": "tony",
                "payload_sha256": payload_digest(&json!({"node": node, "session_id": session})),
                "not_before": now - 60, "not_after": now + minutes * 60, "approved_by_device": "d"});
            let env = sign_envelope(&ed25519_dalek::SigningKey::from_bytes(&SIGNER), "grant", body);
            self.decided.lock().unwrap().insert(rid.into(), ("approved".into(), env));
        }
    }

    impl Approver for FakeApprover {
        fn call<'a>(&'a self, verb: &'a str, args: Value) -> Reply<'a> {
            Box::pin(async move {
                Ok(match verb {
                    "keys" => json!({"grant_pub": b64u(ed25519_dalek::SigningKey::from_bytes(&SIGNER).verifying_key().as_bytes())}),
                    "approval_request" => {
                        let mut r = self.requests.lock().unwrap();
                        let rid = format!("q{}", r.len());
                        r.push((rid.clone(), args));
                        json!({"request_id": rid})
                    }
                    "approval_get" => match self.decided.lock().unwrap().get(args["request_id"].as_str().unwrap_or_default()) {
                        Some((status, grant)) => json!({"status": status, "grant": grant}),
                        None => json!({"status": "pending", "grant": null}),
                    },
                    _ => anyhow::bail!("no verb {verb}"),
                })
            })
        }
    }
}
