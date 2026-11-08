//! The seats' world in production: reflection, scope, operations and journal through the
//! gateway, and the chats through the connectors.

use std::collections::HashSet;
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::push::{self, Outcome};
use awm_plugin_sdk::{CallOpts, Context, Hub};
use serde_json::{json, Value};

use super::{Started, World};
use crate::app::Conns;

pub struct Live {
    pub hub: Hub,
    pub here: String,
    pub conns: Conns,
    /// The service's context, for `social/seats`. None in a test.
    pub ctx: Option<Context>,
}

impl Live {
    fn opts(&self, node: &str, secs: u64) -> CallOpts {
        CallOpts { peer: (node != self.here).then(|| node.to_string()), timeout: Duration::from_secs(secs), ..CallOpts::default() }
    }
}

fn reason(v: &Value) -> String {
    ["reason", "error", "code"].iter().find_map(|k| v.get(*k).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string)).unwrap_or_else(|| v.to_string())
}

#[async_trait]
impl World for Live {
    fn now(&self) -> f64 {
        crate::db::now()
    }

    async fn sleep(&self, secs: f64) {
        tokio::time::sleep(Duration::from_secs_f64(secs)).await;
    }

    async fn start(&self, node: &str, spec: Value) -> Started {
        match self.hub.call_with("reflection", "start_other", spec, &self.opts(node, 240)).await {
            Ok(v) if v.get("ok") != Some(&json!(false)) && v.get("job").and_then(Value::as_str).is_some_and(|j| !j.is_empty()) => Started::Job(v["job"].as_str().unwrap().to_string()),
            Ok(v) => Started::Refused(reason(&v)),
            Err(e) => Started::Refused(e.to_string()),
        }
    }

    async fn push(&self, node: &str, job: &str, line: &str) -> Outcome {
        push::reflect(&self.hub, node, job, line).await
    }

    async fn live(&self, node: &str) -> Option<HashSet<String>> {
        push::live_jobs(&self.hub, node).await
    }

    async fn scope_ready(&self, node: &str, project: &str, scope: &str) -> Option<bool> {
        let v = self.hub.call_with("scope", "resolve", json!({"project": project, "scope": scope}), &self.opts(node, 30)).await.ok()?;
        v.get("exists").and_then(Value::as_bool)
    }

    async fn transcript(&self, node: &str, job: &str) -> Option<String> {
        let v = self.hub.call_with("reflection", "transcript", json!({"job": job}), &self.opts(node, 30)).await.ok()?;
        v.get("path").and_then(Value::as_str).map(str::to_string)
    }

    async fn react(&self, account: &str, channel: &str, message_id: &str, emoji: &str) {
        let conn = self.conns.read().unwrap().get(account).cloned();
        if let Some(c) = conn {
            if let Err(e) = c.react(channel, message_id, emoji, false).await {
                tracing::info!("seat react on {account}/{channel} failed: {}", e.message);
            }
        }
    }

    async fn say(&self, account: &str, channel: &str, thread: Option<&str>, text: &str) -> bool {
        let conn = self.conns.read().unwrap().get(account).cloned();
        let Some(c) = conn else { return false };
        match c.send(channel, text, thread, &[]).await {
            Ok(_) => true,
            Err(e) => {
                tracing::warn!("seat notice to {account}/{channel} failed: {}", e.message);
                false
            }
        }
    }

    async fn ask(&self, kind: &str, text: &str) {
        let args = if kind == "review" { json!({"kind": kind, "summary": text}) } else { json!({"kind": kind, "reason": text}) };
        if let Err(e) = self.hub.call_with("operations", "request_human", args, &self.opts(&self.here, 30)).await {
            tracing::warn!("request_human ({kind}) failed: {e}");
        }
    }

    async fn tag(&self, name: &str, description: &str) {
        let _ = self.hub.call_with("journal", "tag_add", json!({"name": name, "description": description}), &self.opts(&self.here, 30)).await;
    }

    fn emit(&self, event: Value) {
        if let Some(ctx) = &self.ctx {
            ctx.emit("seats", event);
        }
    }

    async fn remote_control(&self, node: &str, job: &str, on: bool, name: &str) -> Result<Option<String>, String> {
        match self.hub.call_with("reflection", "remote_control", json!({"job": job, "on": on, "name": name}), &self.opts(node, 60)).await {
            Ok(v) if v.get("ok") != Some(&json!(false)) => Ok(v.get("rc_url").and_then(Value::as_str).filter(|u| !u.is_empty()).map(str::to_string)),
            Ok(v) => Err(reason(&v)),
            Err(e) => Err(e.to_string()),
        }
    }

    async fn rc_url(&self, node: &str, job: &str) -> Option<String> {
        let v = self.hub.call_with("reflection", "list_other", json!({}), &self.opts(node, 30)).await.ok()?;
        let row = v.get("sessions")?.as_array()?.iter().find(|r| r.get("job").and_then(Value::as_str) == Some(job))?;
        row.get("rc_url").and_then(Value::as_str).filter(|u| !u.is_empty()).map(str::to_string)
    }
}
