//! Arbutus admin approvals in Slack (fleet.md R91, users.md R123): Ryan's way, and Tony's
//! when the passkey is out of reach, to approve the one grant that changes Arbutus.
//!
//! The agent opens an `admin_ssh` request for shogun with `approval_request`, then names it
//! in `request_human kind=approval`. The front posts it to the approval channel. An admin's
//! `approve` or `deny` in that post's thread decides it on the approver, as a passkey tap
//! would, and the agent fetches the certificate with `approval_get`. No verb grants: only a
//! message event from an admin's own hand does, and social flags as `relayed` any message it
//! sent for a person. A Slack approval never grants anything but admin@shogun.

use std::path::PathBuf;
use std::sync::Arc;

use serde_json::{json, Value};

use crate::relay::async_trait_free::BoxFut;

pub const DEFAULT_APPROVAL_CHANNEL: &str = "slack-bot:C0C9XNR77ML";
pub const DEFAULT_APPROVAL_ADMINS: &str = "U01Q4CDSTFX=tony,UC7RUF67P=ryan";
/// The one node a Slack approval reaches. Its admin account holds what changes Arbutus.
pub const SLACK_NODE: &str = "shogun";

fn env(k: &str) -> Option<String> {
    std::env::var(k).ok()
}

/// `AWM_APPROVAL_CHANNEL` is `account:channel`. Unset, #arbutus; set empty, none.
pub fn approval_channel() -> Option<(String, String)> {
    let raw = env("AWM_APPROVAL_CHANNEL").map(|s| s.trim().to_string()).unwrap_or_else(|| DEFAULT_APPROVAL_CHANNEL.into());
    let (a, c) = raw.split_once(':')?;
    (!a.is_empty() && !c.is_empty()).then(|| (a.to_string(), c.to_string()))
}

/// Platform user id -> admin name, from `AWM_APPROVAL_ADMINS` (`id=name,id=name`).
pub fn approval_admins() -> Vec<(String, String)> {
    let raw = env("AWM_APPROVAL_ADMINS").unwrap_or_else(|| DEFAULT_APPROVAL_ADMINS.into());
    raw.replace(',', " ")
        .split_whitespace()
        .filter_map(|p| p.split_once('=').map(|(u, n)| (u.to_string(), n.to_string())))
        .filter(|(u, n)| !u.is_empty() && !n.is_empty())
        .collect()
}

/// Where the posted requests are recorded, in the service's own state dir.
pub fn approval_pending_dir() -> PathBuf {
    PathBuf::from(env("HOME").unwrap_or_else(|| "/".into())).join(".awm/operations/arbutus")
}

/// Whether an admin's Slack reply may grant this request, as `approval_get` returns it.
pub fn slack_grantable(req: &Value) -> Result<(), String> {
    if req["status"] != "pending" {
        return Err(format!("the request is {}", req["status"].as_str().unwrap_or("unknown")));
    }
    if req["kind"] != "admin_ssh" || req["payload"]["nodes"] != json!([SLACK_NODE]) {
        return Err(format!("a Slack approval grants only admin_ssh for {SLACK_NODE} alone"));
    }
    Ok(())
}

fn slack(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

pub fn request_text(rec: &Value, req: &Value) -> String {
    let s = |v: &Value| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string());
    [
        format!("*Admin approval request* `{}` from {} on {}", s(&rec["ref"]), slack(&s(&rec["agent"])), s(&rec["node"])),
        format!("> {}", slack(&s(&req["reason"]))),
        format!("Admin on {SLACK_NODE} for {} minutes: Terraform and Ansible on Arbutus, and {SLACK_NODE} itself.", s(&req["minutes"])),
        "Tony or Ryan: reply `approve` or `deny` in this thread.".into(),
    ]
    .join("\n")
}

/// The first word of a reply, lowercased and stripped to letters.
fn first_word(text: &str) -> String {
    text.split_whitespace().next().map(|w| w.to_lowercase().chars().filter(|c| c.is_ascii_lowercase()).collect()).unwrap_or_default()
}

/// The approval path's ways out: the Slack post, the approver and the session line.
pub trait ArbutusIo: Send + Sync {
    fn post<'a>(&'a self, account: &'a str, channel: &'a str, text: &'a str, thread: Option<&'a str>, quiet: bool) -> BoxFut<'a, Result<Value, String>>;
    /// The request as `approval_get` returns it.
    fn request<'a>(&'a self, request_id: &'a str) -> BoxFut<'a, Result<Value, String>>;
    /// Grant or deny the request on the approver, in the named admin's name.
    fn decide<'a>(&'a self, request_id: &'a str, admin: &'a str, approve: bool) -> BoxFut<'a, Result<Value, String>>;
    fn tell<'a>(&'a self, session: &'a str, line: &'a str) -> BoxFut<'a, Result<(), String>>;
}

pub struct Approvals {
    io: Arc<dyn ArbutusIo>,
    lock: tokio::sync::Mutex<()>,
    pending_dir: PathBuf,
}

impl Approvals {
    pub fn new(io: Arc<dyn ArbutusIo>) -> Approvals {
        Approvals::with_dir(io, approval_pending_dir())
    }

    pub fn with_dir(io: Arc<dyn ArbutusIo>, pending_dir: PathBuf) -> Approvals {
        Approvals { io, lock: tokio::sync::Mutex::new(()), pending_dir }
    }

    fn path(&self, r: &str) -> PathBuf {
        self.pending_dir.join(format!("{r}.json"))
    }

    fn save(&self, rec: &Value) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.pending_dir)?;
        let path = self.path(rec["ref"].as_str().unwrap_or_default());
        let tmp = path.with_extension("tmp");
        std::fs::write(&tmp, serde_json::to_string_pretty(rec).unwrap_or_default())?;
        std::fs::rename(tmp, path)
    }

    fn records(&self) -> Vec<Value> {
        let Ok(rd) = std::fs::read_dir(&self.pending_dir) else { return vec![] };
        rd.filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "json"))
            .filter_map(|p| match std::fs::read_to_string(&p).map(|s| serde_json::from_str::<Value>(&s)) {
                Ok(Ok(v)) => Some(v),
                _ => {
                    tracing::warn!("operations: unreadable approval record {}", p.display());
                    None
                }
            })
            .collect()
    }

    /// Post the caller's own pending request for admins to decide. Grants nothing.
    pub async fn open(&self, request_id: &str, agent: &str, node: &str, session: &str) -> Result<Value, String> {
        let Some((account, channel)) = approval_channel() else { return Err("this node has no approval channel: set AWM_APPROVAL_CHANNEL".into()) };
        let req = self.io.request(request_id).await?;
        if session.is_empty() || req["requester_session"].as_str() != Some(session) {
            return Err("only the session that opened the request posts it".into());
        }
        slack_grantable(&req)?;
        if self.path(request_id).exists() {
            return Err(format!("{request_id} is already posted"));
        }
        let mut rec = json!({
            "ref": request_id, "agent": agent, "node": node, "session": session,
            "account": account, "channel": channel, "post_ts": "", "state": "pending",
        });
        let sent = self.io.post(&account, &channel, &request_text(&rec, &req), None, true).await?;
        let pick = |a: &str, b: &str| sent.get(a).or(sent.get(b)).map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string()));
        rec["post_ts"] = json!(pick("ts", "message_id").unwrap_or_default());
        rec["channel"] = json!(sent.get("channel_id").and_then(Value::as_str).unwrap_or(&channel));
        self.save(&rec).map_err(|e| e.to_string())?;
        Ok(rec)
    }

    /// One social message event: decide the request whose thread it answers, if any.
    pub async fn handle(&self, event: &Value) {
        let Some(thread) = event.get("thread_id").and_then(Value::as_str).filter(|s| !s.is_empty()) else { return };
        let word = first_word(event.get("text").and_then(Value::as_str).unwrap_or_default());
        let approve = ["approve", "approved"].contains(&word.as_str());
        if !approve && !["deny", "denied", "reject"].contains(&word.as_str()) {
            return;
        }
        let (rec, text) = {
            let _g = self.lock.lock().await;
            let Some(mut rec) = self.records().into_iter().find(|r| {
                r["post_ts"].as_str() == Some(thread) && r["channel"] == event["channel_id"] && r["account"] == event["account"]
            }) else {
                return;
            };
            let text = self.decide(&mut rec, event, approve).await;
            (rec, text)
        };
        if let Some(t) = text {
            let (a, c, ts) = (rec["account"].as_str().unwrap_or_default(), rec["channel"].as_str().unwrap_or_default(), rec["post_ts"].as_str().unwrap_or_default());
            if let Err(e) = self.io.post(a, c, &t, Some(ts), false).await {
                tracing::warn!("operations: could not answer approval {} in its thread: {e}", rec["ref"]);
            }
        }
    }

    async fn decide(&self, rec: &mut Value, event: &Value, approve: bool) -> Option<String> {
        if event.get("relayed") == Some(&json!(true)) || event.get("sender_bot") == Some(&json!(true)) {
            tracing::warn!("operations: ignored a relayed or bot reply on approval {}", rec["ref"]);
            return None;
        }
        let sender = event.get("sender_id").map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())).unwrap_or_default();
        let Some(admin) = approval_admins().into_iter().find(|(u, _)| *u == sender).map(|(_, n)| n) else {
            return Some(format!("Only an Arbutus admin can decide this. {} (`{sender}`) is not on the list.",
                                slack(event.get("sender_name").and_then(Value::as_str).unwrap_or_default())));
        };
        let r = rec["ref"].as_str().unwrap_or_default().to_string();
        if rec["state"] != "pending" {
            return Some(format!("`{r}` is already {}.", rec["state"].as_str().unwrap_or_default()));
        }
        let (state, text) = match (self.io.decide(&r, &admin, approve).await, approve) {
            (Ok(_), true) => ("approved", format!("Approved by {admin}. The agent holds admin on {SLACK_NODE} now.")),
            (Ok(_), false) => ("denied", format!("Denied by {admin}.")),
            (Err(e), _) => ("failed", format!("Nothing was decided: {}", slack(&e))),
        };
        Some(self.close(rec, state, &admin, &text).await)
    }

    async fn close(&self, rec: &mut Value, state: &str, admin: &str, text: &str) -> String {
        rec["state"] = json!(state);
        rec["decided_by"] = json!(admin);
        rec["decided_at"] = json!(crate::now());
        if let Err(e) = self.save(rec) {
            tracing::warn!("operations: could not record approval {}: {e}", rec["ref"]);
        }
        let r = rec["ref"].as_str().unwrap_or_default();
        let line = if state == "approved" {
            format!("Admin approval {r} is approved by {admin} in Slack. approval_get returns its ssh certificate.")
        } else {
            format!("Admin approval {r} is {state}, by {admin} in Slack. Nothing was granted.")
        };
        if let Some(session) = rec["session"].as_str().filter(|j| !j.is_empty()) {
            if let Err(e) = self.io.tell(session, &line).await {
                tracing::info!("operations: {session} did not take the approval line: {e}");
            }
        }
        text.to_string()
    }
}
