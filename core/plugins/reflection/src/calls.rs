//! The plain calls to scope, journal and operations, each of which degrades when it fails, and
//! the scope a directory names.

use futures_util::future::join_all;
use std::path::PathBuf;
use std::time::Duration;

use awm_plugin_sdk::config;
use serde_json::{json, Value};

use crate::app::{is_ok, text, App};
use crate::gateway::{Call, Gateway};

pub const SCOPE_TIMEOUT: Duration = Duration::from_secs(10);
pub const BRIEF_TIMEOUT: Duration = Duration::from_secs(5);
pub const TASK_TIMEOUT: Duration = Duration::from_secs(5);
pub const PARENT_TIMEOUT: Duration = Duration::from_secs(30);

impl<G: Gateway> App<G> {
    /// `(project, scope)` of a directory under the workspace's `projects/`. The scope is the
    /// deepest enclosing worktree, as the gateway stamps it, so a subfolder of a worktree names
    /// the worktree's scope.
    pub async fn scope_of(&self, cwd: Option<&str>) -> Option<(String, String)> {
        let cwd = PathBuf::from(cwd.filter(|c| !c.is_empty())?);
        let projects = self.node.projects_dir();
        let found = tokio::task::spawn_blocking(move || config::scope_of(&cwd, &projects)).await.ok()??;
        match found {
            (project, Some(scope)) if !project.starts_with('.') => Some((project, scope)),
            _ => None,
        }
    }

    /// The scope tool's answer, or None when it cannot answer.
    pub async fn scopes_call(&self, verb: &str, args: Value) -> Option<Value> {
        match self.gw.call(Call::new("scope", verb, args, SCOPE_TIMEOUT)).await {
            Ok(out @ Value::Object(_)) => Some(out),
            Ok(_) => None,
            Err(e) => {
                tracing::info!("reflection: scope {verb} unavailable: {e}");
                None
            }
        }
    }

    /// End every scope claim `session_id` holds. A lost release never fails the stop.
    pub async fn release_claims(&self, session_id: &str) {
        let listing = self.scopes_call("claims", json!({})).await.unwrap_or_default();
        let rows = listing.get("claims").and_then(Value::as_array).cloned().unwrap_or_default();
        let held = rows.iter().filter(|r| r.get("holder").and_then(|h| h.get("session_id")).and_then(Value::as_str) == Some(session_id));
        join_all(held.map(|r| self.scopes_call("release", json!({"project": r.get("project"), "scope": r.get("scope"), "session_id": session_id})))).await;
    }

    /// The live session that holds the scope, or None when it is free or claims are unavailable.
    pub async fn held_by(&self, project: &str, scope: &str) -> Option<Value> {
        let listing = self.scopes_call("claims", json!({"project": project, "scope": scope})).await?;
        listing
            .get("claims")?
            .as_array()?
            .iter()
            .find(|row| row.get("scope").and_then(Value::as_str) == Some(scope) && row.get("holder").is_some_and(|h| crate::app::truthy(Some(h))))
            .and_then(|row| row.get("holder").cloned())
    }

    /// The line a move's follow-up carries: the brief of the scope it enters (scopes.md R143).
    pub async fn brief_line(&self, project: &str, scope: &str) -> String {
        let args = json!({"project": project, "scope": scope});
        match self.gw.call(Call::new("journal", "brief_read", args, BRIEF_TIMEOUT)).await {
            Ok(got) => got.get("brief").map(|b| text(b, "line").unwrap_or_default().to_string()).unwrap_or_default(),
            Err(e) => {
                tracing::info!("reflection: no brief for {project}/{scope}: {e}");
                String::new()
            }
        }
    }

    /// Give the journal a parent link (sessions.md R13, R16). A lost link never fails the call.
    pub async fn record_parent(&self, session_id: &str, parent: &str, how: &str) -> bool {
        let args = json!({"session_id": session_id, "parent": parent, "how": how});
        match self.gw.call(Call::new("journal", "session_parent", args, PARENT_TIMEOUT)).await {
            Ok(out) => out.is_object() && out.get("ok").is_none_or(|_| is_ok(&out)),
            Err(e) => {
                tracing::info!("reflection: parent link {parent} -> {session_id} ({how}) not recorded: {e}");
                false
            }
        }
    }

    /// The board task of a session, from operations. A missing operations is no task.
    pub async fn task_of(&self, session_id: &str) -> Value {
        let args = json!({"session_id": session_id});
        match self.gw.call(Call::new("operations", "task_for_session", args, TASK_TIMEOUT)).await {
            Ok(out) => out.get("task_id").cloned().unwrap_or(Value::Null),
            Err(_) => Value::Null,
        }
    }
}
