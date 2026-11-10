//! The seats' world in production: this node's reflection and scope, and the outbox.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::push::{self, Outcome, Pusher};
use awm_plugin_sdk::{CallOpts, Hub};
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

/// A start's or resume's job, and whether its session came up with a Remote Control link.
fn started(reply: Result<Value, awm_plugin_sdk::HubError>) -> (Started, bool) {
    match reply {
        Ok(v) if v.get("ok") != Some(&json!(false)) && v.get("job").and_then(Value::as_str).is_some_and(|j| !j.is_empty()) => {
            (Started::Job(v["job"].as_str().unwrap().to_string()), v.get("link").and_then(Value::as_str).is_some_and(|l| !l.is_empty()))
        }
        Ok(v) => (Started::Refused(reason(&v)), false),
        Err(e) => (Started::Refused(e.to_string()), false),
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
        started(self.hub.call_with("reflection", "start_other", spec, &opts(240)).await).0
    }

    async fn resume(&self, job: &str, prompt: &str, remote_control: bool, name: &str) -> Started {
        let (out, link) = started(self.hub.call_with("reflection", "resume_other", json!({"to": job, "prompt": prompt}), &opts(240)).await);
        if let Started::Job(new) = &out {
            if link != remote_control {
                if let Err(why) = self.remote_control(new, remote_control, name).await {
                    tracing::info!("seats: Remote Control {} for the resumed {new} failed: {why}", if remote_control { "on" } else { "off" });
                }
            }
        }
        out
    }

    async fn stop(&self, job: &str) -> Result<(), String> {
        match self.hub.call_with("reflection", "stop_other", json!({"job": job}), &opts(60)).await {
            Ok(v) if v.get("ok") != Some(&json!(false)) => Ok(()),
            Ok(v) => Err(reason(&v)),
            Err(e) => Err(e.to_string()),
        }
    }

    async fn push(&self, job: &str, line: &str) -> Outcome {
        push::reflect(&self.hub, &self.here, job, line).await
    }

    async fn live(&self) -> Option<Agents> {
        let rows = push::live_rows(&self.hub, &self.here).await?;
        let text = |r: &Value, k: &str| r.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let agent = |r: &Value| Agent {
            link: text(r, "rc_url"),
            busy: text(r, "state") == "working",
            active_at: chrono::DateTime::parse_from_rfc3339(&text(r, "active_at")).map_or(0.0, |t| t.timestamp_millis() as f64 / 1000.0),
        };
        Some(rows.iter().map(|(job, r)| (job.clone(), agent(r))).collect())
    }

    async fn scope_ready(&self, project: &str, scope: &str) -> Option<bool> {
        let v = self.hub.call_with("scope", "resolve", json!({"project": project, "scope": scope}), &opts(30)).await.ok()?;
        v.get("exists").and_then(Value::as_bool)
    }

    async fn transcript(&self, job: &str) -> Option<String> {
        let v = self.hub.call_with("reflection", "transcript", json!({"job": job}), &opts(30)).await.ok()?;
        v.get("path").and_then(Value::as_str).map(str::to_string)
    }

    async fn remote_control(&self, job: &str, on: bool, name: &str) -> Result<(), String> {
        match self.hub.call_with("reflection", "remote_control", json!({"job": job, "on": on, "name": name}), &opts(60)).await {
            Ok(v) if v.get("ok") != Some(&json!(false)) => Ok(()),
            Ok(v) => Err(reason(&v)),
            Err(e) => Err(e.to_string()),
        }
    }

    fn hand(&self, job: &str, line: &str) {
        self.pusher.push(&self.here, job, line);
    }

    async fn up(&self, kind: &str, body: Value) {
        self.link.enqueue(kind, &body).await;
    }
}
