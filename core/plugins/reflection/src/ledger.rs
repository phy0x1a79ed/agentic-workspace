//! The ledger of owed follow-ups, and the alarm for a lost one.
//!
//! A harness plugin answers a command with a follow-up with a ticket, then sends `harness_event`
//! once the session took the follow-up up (`done`) or it gave up (`lost`). A ticket that its
//! plugin no longer holds, and that never got an event, is lost too: the sweep finds those.
//!
//! A lost follow-up leaves a session idle with nobody told. So the alarm leaves three traces: an
//! ERROR log line, a line in `lost.jsonl` that `pending` lists, and a journal message to the
//! session's scope, which copies onto its card. The harness plugin types the notice into a live
//! session itself. Each trace is best effort, and an alarm never fails.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use awm_pycompat::repr::str_repr;
use crate::app::{object, text, truthy, App};
use crate::gateway::{Call, Gateway};
use crate::store::File;

/// The newest lost entries `pending` shows.
pub const SHOWN: usize = 20;
/// A ticket younger than this is not swept: its plugin may not have recorded it yet.
pub const SETTLE_MS: i64 = 300_000;
/// A `.closed` marker older than this is removed by the sweep.
pub const CLOSED_KEPT_MS: i64 = 7 * 24 * 3600 * 1000;
const JOURNAL_TIMEOUT: Duration = Duration::from_secs(10);

/// One owed follow-up, as a provider's answer opened it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Ticket {
    pub ticket: String,
    pub session_id: String,
    pub tool: String,
    pub command: String,
    pub followup: String,
    pub name: String,
    pub cwd: Option<String>,
}

/// A ticket names a file in `tickets/`, so it may not name another path.
fn plain(ticket: &str) -> bool {
    !ticket.is_empty() && !ticket.contains(['/', '\0']) && ticket != "." && ticket != ".."
}

fn as_text(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

impl<G: Gateway> App<G> {
    /// Open `t` unless its event already closed it.
    pub async fn open_ticket(&self, t: &Ticket) {
        if !plain(&t.ticket) {
            return;
        }
        let dir = self.store.tickets();
        let _held = self.store.lock(File::Tickets).await;
        if tokio::fs::metadata(dir.join(format!("{}.closed", t.ticket))).await.is_ok() {
            return;
        }
        let rec = json!({"ticket": t.ticket, "session_id": t.session_id, "tool": t.tool, "command": t.command,
                         "followup": t.followup, "name": t.name, "cwd": t.cwd, "opened_ms": self.node.now_ms()});
        let written = async {
            tokio::fs::create_dir_all(&dir).await?;
            tokio::fs::write(dir.join(format!("{}.json", t.ticket)), rec.to_string()).await
        };
        if let Err(e) = written.await {
            tracing::warn!("reflection: could not record ticket {}: {e}", t.ticket);
        }
    }

    /// Every open ticket, by file name.
    pub async fn open_tickets(&self) -> Vec<Value> {
        let dir = self.store.tickets();
        let _held = self.store.lock(File::Tickets).await;
        let Ok(mut entries) = tokio::fs::read_dir(&dir).await else { return vec![] };
        let mut names = vec![];
        while let Ok(Some(e)) = entries.next_entry().await {
            let name = e.file_name().to_string_lossy().into_owned();
            if name.ends_with(".json") {
                names.push(name);
            }
        }
        names.sort();
        let mut out = vec![];
        for name in names {
            if let Some(v) = tokio::fs::read_to_string(dir.join(&name)).await.ok().and_then(|t| serde_json::from_str(&t).ok()) {
                out.push(v);
            }
        }
        out
    }

    /// The open record of `ticket`, removed. An event can beat its own ticket here, so a ticket
    /// closed before it opened leaves a marker that keeps it shut.
    async fn close_ticket(&self, ticket: &str) -> Option<Map<String, Value>> {
        if !plain(ticket) {
            return None;
        }
        let dir = self.store.tickets();
        let _held = self.store.lock(File::Tickets).await;
        let path = dir.join(format!("{ticket}.json"));
        if let Some(Value::Object(rec)) = tokio::fs::read_to_string(&path).await.ok().and_then(|t| serde_json::from_str(&t).ok()) {
            if tokio::fs::remove_file(&path).await.is_ok() {
                return Some(rec);
            }
        }
        let _ = tokio::fs::create_dir_all(&dir).await;
        let _ = tokio::fs::write(dir.join(format!("{ticket}.closed")), b"").await;
        None
    }

    /// Close the event's ticket, keep the event, and raise the alarm on `lost`.
    pub async fn record_event(&self, event: Map<String, Value>) -> Value {
        let ticket = as_text(event.get("ticket"));
        let opened = if truthy(event.get("ticket")) { self.close_ticket(&ticket).await } else { None };
        let mut full = opened.unwrap_or_default();
        for (k, v) in event {
            if !(v.is_null() || v.as_str() == Some("")) {
                full.insert(k, v);
            }
        }
        let mut line = full.clone();
        line.insert("received_ms".into(), json!(self.node.now_ms()));
        let kind = full.get("kind").cloned().unwrap_or(Value::Null);
        {
            let _held = self.store.lock(File::Events).await;
            if let Err(e) = self.store.append(File::Events, &Value::Object(line)).await {
                tracing::warn!("reflection: could not keep the {} event: {e}", as_text(Some(&kind)));
            }
        }
        match kind.as_str() {
            Some("lost") => json!({"ok": true, "alarm": self.raise_alarm(&full, None).await}),
            Some("done") => json!({"ok": true}),
            Some(k) => json!({"ok": false, "error": format!("unknown event kind {}", str_repr(k))}),
            None if kind.is_null() => json!({"ok": false, "error": "unknown event kind None"}),
            None => json!({"ok": false, "error": format!("unknown event kind {kind}")}),
        }
    }

    /// Record and announce a lost follow-up. `scope` names the journal to tell, else the
    /// event's `cwd` does. Answers which traces it left.
    pub async fn raise_alarm(&self, event: &Map<String, Value>, scope: Option<(String, String)>) -> Value {
        let ev = Value::Object(event.clone());
        let name = as_text(event.get("name"));
        let sid = as_text(event.get("session_id"));
        let who = if !name.is_empty() {
            name.clone()
        } else if !sid.is_empty() {
            sid.clone()
        } else {
            format!("pid {}", event.get("pid").filter(|p| !p.is_null()).map_or("None".into(), |p| as_text(Some(p))))
        };
        let (command, followup) = (as_text(event.get("command")), as_text(event.get("followup")));
        let reason = text(&ev, "reason").map_or_else(|| "no reason given".to_string(), str::to_string);
        // Never the command itself: a ticket's command is the caller's own typed line.
        tracing::error!("reflection: LOST the follow-up of ticket {} for session {who}: {reason}", as_text(event.get("ticket")));
        let at_ms = event.get("at_ms").filter(|v| truthy(Some(v))).cloned().unwrap_or_else(|| json!(self.node.now_ms()));
        let entry = json!({
            "at_ms": at_ms, "session": name, "session_id": sid, "pid": event.get("pid").cloned().unwrap_or(Value::Null),
            "command": command, "resume": followup, "ticket": as_text(event.get("ticket")),
            "injected_at_ms": event.get("injected_at_ms").cloned().unwrap_or(Value::Null),
            "reissues": event.get("reissues").filter(|v| truthy(Some(v))).cloned().unwrap_or(json!(0)),
            "told_session": truthy(event.get("told_session")), "reason": reason,
        });
        let recorded = {
            let _held = self.store.lock(File::Lost).await;
            match self.store.append(File::Lost, &entry).await {
                Ok(()) => true,
                Err(e) => {
                    tracing::warn!("reflection: could not record the lost resume: {e}");
                    false
                }
            }
        };
        let place = match scope {
            Some(s) => Some(s),
            None => self.scope_of(text(&ev, "cwd")).await,
        };
        let mut journal = false;
        if let Some((project, scope)) = place {
            let body = format!(
                "reflection lost `{command}` for session {who}: {reason}. The session was owed the follow-up \"{followup}\" and did not get it."
            );
            let post = json!({"project": project, "scope": scope, "author": "reflection",
                              "meta": {"alarm": "reflection-lost", "session_id": entry["session_id"], "command": command},
                              "body": body});
            match self.gw.call(Call::new("journal", "post", post, JOURNAL_TIMEOUT)).await {
                Ok(_) => journal = true,
                Err(e) => tracing::warn!("reflection: could not post the lost-resume alarm to {project}/{scope}: {e}"),
            }
        }
        json!({"logged": true, "recorded": recorded, "journal": journal})
    }

    /// The newest lost entries, newest first.
    pub async fn recent_lost(&self) -> Vec<Value> {
        let text = self.store.read_text(File::Lost).await;
        let lines: Vec<&str> = text.lines().collect();
        let from = lines.len().saturating_sub(SHOWN);
        lines[from..].iter().rev().filter_map(|l| serde_json::from_str(l).ok()).collect()
    }

    /// The tickets `tool` still holds, or None when it cannot be asked.
    pub async fn held(&self, tool: String) -> Option<Vec<String>> {
        let p = self.providers().await.into_iter().find(|p| p.tool == tool)?;
        let out = self.harness_call(&p, "pending", json!({}), None).await.ok()?;
        let rows = out.get("pending").and_then(Value::as_array).cloned().unwrap_or_default();
        Some(rows.iter().map(|r| as_text(r.get("ticket"))).collect())
    }

    /// Raise the alarm for every settled ticket its plugin no longer holds. `held(tool)` answers
    /// the tickets the plugin still holds, or None when it cannot be asked: a ticket is never
    /// declared lost on no answer.
    pub async fn sweep<H, F>(&self, held: H, now_ms: i64) -> Vec<String>
    where
        H: Fn(String) -> F,
        F: Future<Output = Option<Vec<String>>>,
    {
        let mut asked: HashMap<String, Option<HashSet<String>>> = HashMap::new();
        let mut lost = vec![];
        for rec in self.open_tickets().await {
            let opened = rec.get("opened_ms").filter(|v| truthy(Some(v))).and_then(Value::as_i64).unwrap_or(now_ms);
            if now_ms - opened < SETTLE_MS {
                continue;
            }
            let tool = as_text(rec.get("tool"));
            if !asked.contains_key(&tool) {
                let got = held(tool.clone()).await.map(|t| t.into_iter().collect());
                asked.insert(tool.clone(), got);
            }
            let ticket = as_text(rec.get("ticket"));
            match &asked[&tool] {
                Some(still) if !still.contains(&ticket) => {}
                _ => continue,
            }
            let mut event = object(&rec);
            event.insert("kind".into(), json!("lost"));
            event.insert("reason".into(), json!(format!("{tool} no longer holds the follow-up and never reported how it ended")));
            self.record_event(event).await;
            lost.push(ticket);
        }
        lost
    }

    /// Remove the `.closed` markers older than [`CLOSED_KEPT_MS`]. Answers how many went.
    pub async fn prune_closed(&self, now_ms: i64) -> usize {
        let dir = self.store.tickets();
        let _held = self.store.lock(File::Tickets).await;
        let Ok(mut entries) = tokio::fs::read_dir(&dir).await else { return 0 };
        let mut gone = 0;
        while let Ok(Some(e)) = entries.next_entry().await {
            if !e.file_name().to_string_lossy().ends_with(".closed") {
                continue;
            }
            let Ok(modified) = e.metadata().await.and_then(|m| m.modified()) else { continue };
            let at_ms = modified.duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0);
            if now_ms - at_ms > CLOSED_KEPT_MS && tokio::fs::remove_file(e.path()).await.is_ok() {
                gone += 1;
            }
        }
        gone
    }

    /// One sweep of the tickets and the markers, against the providers' `pending`.
    pub async fn sweep_now(&self) -> Vec<String> {
        let now = self.node.now_ms();
        let lost = self.sweep(|tool| self.held(tool), now).await;
        self.prune_closed(now).await;
        lost
    }

    /// Start a sweep in the background unless one runs. An event or the boot starts it; nothing
    /// polls.
    pub fn kick_sweep(self: &Arc<Self>) {
        if self.sweeping.swap(true, Ordering::SeqCst) {
            return;
        }
        let app = self.clone();
        self.detach(async move {
            app.sweep_now().await;
            app.sweeping.store(false, Ordering::SeqCst);
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_ticket_names_no_other_path() {
        assert!(plain("t-1") && plain("0e38-aa"));
        for bad in ["", "..", ".", "../x", "a/b"] {
            assert!(!plain(bad), "{bad}");
        }
    }
}
