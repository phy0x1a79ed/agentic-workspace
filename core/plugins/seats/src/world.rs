//! The seats' world in production: this node's reflection and scope, and the outbox.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::push::{self, Outcome, Pusher};
use awm_plugin_sdk::{CallOpts, Hub, HubError};
use serde_json::{json, Value};

use crate::engine::{Agent, Agents, Started, World};
use crate::link::Link;

pub struct Live {
    pub hub: Hub,
    pub here: String,
    pub link: Arc<Link>,
    pub pusher: Pusher,
}

fn opts(secs: u64) -> CallOpts {
    CallOpts { timeout: Duration::from_secs(secs), ..CallOpts::default() }
}

fn reason(v: &Value) -> String {
    ["reason", "error", "code"].iter().find_map(|k| v.get(*k).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string)).unwrap_or_else(|| v.to_string())
}

/// The reply of a verb that answered ok, or why it did not.
fn acked(reply: Result<Value, HubError>) -> Result<Value, String> {
    match reply {
        Ok(v) if v.get("ok") != Some(&json!(false)) => Ok(v),
        Ok(v) => Err(reason(&v)),
        Err(e) => Err(e.to_string()),
    }
}

/// The session a start or resume answered, by its canonical id.
fn started(reply: &Result<Value, String>) -> Started {
    match reply {
        Ok(v) => match v.get("session_id").and_then(Value::as_str).filter(|j| !j.is_empty()) {
            Some(session) => Started::Session(session.to_string()),
            None => Started::Refused(reason(v)),
        },
        Err(why) => Started::Refused(why.clone()),
    }
}

#[async_trait]
impl World for Live {
    fn now(&self) -> f64 {
        crate::now()
    }

    async fn sleep(&self, secs: f64) {
        tokio::time::sleep(Duration::from_secs_f64(secs)).await;
    }

    async fn start(&self, spec: Value) -> Started {
        started(&acked(self.hub.call_with("reflection", "start_other", spec, &opts(240)).await))
    }

    async fn resume(&self, session: &str, prompt: &str, remote_control: bool, name: &str) -> Started {
        let reply = acked(self.hub.call_with("reflection", "resume_other", json!({"to": {"id": session}, "prompt": prompt}), &opts(240)).await);
        let out = started(&reply);
        let linked = reply.ok().and_then(|v| v.get("link").and_then(Value::as_str).map(|l| !l.is_empty())).unwrap_or(false);
        if let Started::Session(new) = &out {
            if linked != remote_control {
                if let Err(why) = self.remote_control(new, remote_control, name).await {
                    tracing::info!("seats: Remote Control {} for the resumed {new} failed: {why}", if remote_control { "on" } else { "off" });
                }
            }
        }
        out
    }

    async fn stop(&self, session: &str) -> Result<(), String> {
        acked(self.hub.call_with("reflection", "stop_other", json!({"to": {"id": session}}), &opts(60)).await).map(drop)
    }

    async fn push(&self, session: &str, line: &str) -> Outcome {
        push::reflect(&self.hub, &self.here, session, line).await
    }

    async fn live(&self) -> Option<Agents> {
        let rows = push::live_rows(&self.hub, &self.here).await?;
        let text = |r: &Value, k: &str| r.get(k).and_then(Value::as_str).unwrap_or("").to_owned();
        let agent = |r: Value| Agent {
            link: text(&r, "rc_url"),
            busy: r.get("state").and_then(Value::as_str) == Some("working"),
            active_at: chrono::DateTime::parse_from_rfc3339(&text(&r, "active_at")).map_or(0.0, |t| t.timestamp_millis() as f64 / 1000.0),
        };
        Some(rows.into_iter().map(|(session, r)| (session, agent(r))).collect())
    }

    async fn scope_ready(&self, project: &str, scope: &str) -> Option<bool> {
        let v = self.hub.call_with("scope", "resolve", json!({"project": project, "scope": scope}), &opts(30)).await.ok()?;
        v.get("exists").and_then(Value::as_bool)
    }

    async fn remote_control(&self, session: &str, on: bool, name: &str) -> Result<(), String> {
        acked(self.hub.call_with("reflection", "remote_control", json!({"to": {"id": session}, "on": on, "name": name}), &opts(60)).await).map(drop)
    }

    fn hand(&self, session: &str, line: &str) {
        self.pusher.push(&self.here, session, line);
    }

    async fn up(&self, kind: &str, body: Value) {
        self.link.enqueue(kind, &body).await;
    }
}
