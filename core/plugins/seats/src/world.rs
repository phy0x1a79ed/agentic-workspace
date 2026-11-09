//! The seats' world in production: this node's reflection and scope, and the outbox.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::push::{self, Outcome, Pusher};
use awm_plugin_sdk::{holders, CallOpts, Hub};
use serde_json::{json, Value};

use crate::engine::{Started, World};
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

#[async_trait]
impl World for Live {
    fn now(&self) -> f64 {
        crate::now()
    }

    async fn sleep(&self, secs: f64) {
        tokio::time::sleep(Duration::from_secs_f64(secs)).await;
    }

    async fn start(&self, spec: Value) -> Started {
        match self.hub.call_with("reflection", "start_other", spec, &opts(240)).await {
            Ok(v) if v.get("ok") != Some(&json!(false)) && v.get("job").and_then(Value::as_str).is_some_and(|j| !j.is_empty()) => Started::Job(v["job"].as_str().unwrap().to_string()),
            Ok(v) => Started::Refused(reason(&v)),
            Err(e) => Started::Refused(e.to_string()),
        }
    }

    async fn push(&self, job: &str, line: &str) -> Outcome {
        push::reflect(&self.hub, &self.here, job, line).await
    }

    async fn live(&self) -> Option<HashSet<String>> {
        push::live_jobs(&self.hub, &self.here).await
    }

    async fn scope_ready(&self, project: &str, scope: &str) -> Option<bool> {
        let v = self.hub.call_with("scope", "resolve", json!({"project": project, "scope": scope}), &opts(30)).await.ok()?;
        v.get("exists").and_then(Value::as_bool)
    }

    async fn transcript(&self, job: &str) -> Option<String> {
        let v = self.hub.call_with("reflection", "transcript", json!({"job": job}), &opts(30)).await.ok()?;
        v.get("path").and_then(Value::as_str).map(str::to_string)
    }

    async fn remote_control(&self, job: &str, on: bool, name: &str) -> Result<Option<String>, String> {
        match self.hub.call_with("reflection", "remote_control", json!({"job": job, "on": on, "name": name}), &opts(60)).await {
            Ok(v) if v.get("ok") != Some(&json!(false)) => Ok(v.get("rc_url").and_then(Value::as_str).filter(|u| !u.is_empty()).map(str::to_string)),
            Ok(v) => Err(reason(&v)),
            Err(e) => Err(e.to_string()),
        }
    }

    async fn rc_url(&self, job: &str) -> Option<String> {
        let rows = holders::session_rows(&self.hub, false).await?;
        let row = rows.iter().find(|r| r.get("job").and_then(Value::as_str) == Some(job))?;
        row.get("rc_url").and_then(Value::as_str).filter(|u| !u.is_empty()).map(str::to_string)
    }

    fn hand(&self, job: &str, line: &str) {
        self.pusher.push(&self.here, job, line);
    }

    async fn up(&self, kind: &str, body: Value) {
        self.link.enqueue(kind, &body).await;
    }
}
