//! dsh's side of harness@1 (harness-contract.md § dsh): the fork's sessions.
//!
//! A fork session id is `session-<uuid>`, and its R9 id is `ds` plus the 32 hex digits of that
//! UUID. A verb the fork cannot serve answers `unsupported`.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use awm_harness::contract::{self, HarnessEvent, Kind, Reporter};
use awm_plugin_sdk::ids::{Name, Platform, SessionId};
use serde_json::{json, Map, Value};

use crate::fork::{Fork, ForkError};
use crate::lifecycle::Lifecycle;

pub const PLATFORM: &str = Platform::DeepSeek.code();
pub const NAME: &str = "DeepSeek harness";
pub const COMPACT_WAIT: Duration = Duration::from_secs(1800);
pub const POLL: Duration = Duration::from_secs(3);

/// What `resolve` reads of other processes. The live one is `/proc`; tests pass a fake.
pub trait Procs: Send + Sync {
    fn environ(&self, pid: u32) -> HashMap<String, String>;
    /// The parent pid, 0 when unreadable.
    fn ppid(&self, pid: u32) -> u32;
}

pub struct ProcFs;

impl Procs for ProcFs {
    fn environ(&self, pid: u32) -> HashMap<String, String> {
        awm_plugin_sdk::procfs::environ(pid).unwrap_or_default()
    }

    fn ppid(&self, pid: u32) -> u32 {
        awm_plugin_sdk::procfs::ppid(pid).unwrap_or(0)
    }
}

/// The R9 id of a fork id, or None when it holds no hex id.
pub fn r9(fork_id: &str) -> Option<String> {
    SessionId::canonical_of_native(Platform::DeepSeek, fork_id.strip_prefix("session-").unwrap_or(fork_id))
}

pub fn unsupported(what: &str) -> Value {
    contract::unsupported(format!("the DeepSeek harness cannot {what}"))
}

/// Python's `repr` of a string, as the error texts quote a ref.
fn repr(s: &str) -> String {
    format!("'{s}'")
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn nonempty(v: Option<&Value>) -> Option<&str> {
    v.and_then(Value::as_str).filter(|s| !s.is_empty())
}

/// `(project, scope)` of a cwd under the last `projects/` in it.
fn scope_of(cwd: &str) -> Option<(String, String)> {
    let parts: Vec<&str> = cwd.split('/').filter(|p| !p.is_empty() && *p != ".").collect();
    let at = parts.iter().rposition(|p| *p == "projects")?;
    let rest = &parts[at + 1..];
    (rest.len() >= 2).then(|| (rest[0].to_string(), rest[1..].join("/")))
}

/// A `session.list` item as a harness@1 row, or None for an item with no R9 id.
pub fn row(item: &Value) -> Option<Value> {
    let fork_id = item.get("sessionId").and_then(Value::as_str).unwrap_or("");
    let sid = r9(fork_id)?;
    let cwd = item.get("cwd").cloned().unwrap_or(Value::Null);
    let place = cwd.as_str().and_then(scope_of);
    let parent = nonempty(item.get("parentSessionId")).and_then(r9);
    Some(json!({
        "session_id": sid, "fork_session_id": fork_id,
        "name": nonempty(item.get("title")),
        "kind": "web", "live": true,
        "state": if truthy(item.get("running")) { "busy" } else { "idle" },
        "cwd": cwd,
        "project": place.as_ref().map(|p| &p.0), "scope": place.as_ref().map(|p| &p.1),
        "parent": parent,
        "preset": item.get("agentPreset").cloned().unwrap_or(Value::Null),
    }))
}

fn text_of(row: &Value, k: &str) -> String {
    row.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

pub struct Sessions {
    pub fork: Fork,
    pub procs: Box<dyn Procs>,
    pub reporter: Reporter,
    pub lifecycle: Arc<Lifecycle>,
    pub compact_wait: Duration,
    pub poll: Duration,
}

/// Every `session.list` item, page by page.
pub async fn list_all(fork: &Fork) -> Result<Vec<Value>, ForkError> {
    let (mut items, mut cursor) = (vec![], None::<String>);
    for _ in 0..50 {
        let payload = match &cursor {
            Some(c) => json!({"cursor": c}),
            None => json!({}),
        };
        let page = fork.rpc("session.list", payload).await?;
        items.extend(page.get("items").and_then(Value::as_array).cloned().unwrap_or_default());
        cursor = nonempty(page.get("nextCursor")).or_else(|| nonempty(page.get("cursor"))).map(str::to_string);
        if cursor.is_none() {
            break;
        }
    }
    Ok(items)
}

impl Sessions {
    /// Every item; each full listing also goes to the session-event diff.
    async fn all(&self) -> Result<Vec<Value>, ForkError> {
        let ticket = self.lifecycle.ticket();
        let items = list_all(&self.fork).await?;
        self.lifecycle.listed(ticket, &items);
        Ok(items)
    }

    pub async fn rows(&self) -> Result<Vec<Value>, ForkError> {
        Ok(self.all().await?.iter().filter_map(row).collect())
    }

    /// The one session `reference` names: its R9 id, fork id, an R9 prefix of 8 or more, or its
    /// exact title. Two hits are a refusal.
    /// The session whose canonical id is `session_id`.
    pub async fn by_id(&self, session_id: &str) -> Result<Option<Value>, ForkError> {
        Ok(self.rows().await?.into_iter().find(|row| text_of(row, "session_id") == session_id))
    }

    /// Every session whose whole title is `name`, any case.
    pub async fn named(&self, name: &str) -> Result<Vec<Value>, ForkError> {
        let Ok(name) = Name::new(name) else { return Ok(vec![]) };
        Ok(self.rows().await?.into_iter().filter(|row| name.matches(&text_of(row, "name"))).collect())
    }

    /// Whether `pid` is a fork session, walking up to the fork's node process.
    pub async fn resolve(&self, pid: Option<i64>, fork_pid: Option<u32>) -> Value {
        let Some(mut pid) = pid else { return json!({"ok": true, "mine": false, "error": "no pid"}) };
        let mut seen = 0;
        while pid > 1 && seen < 64 {
            let p = pid as u32;
            if fork_pid == Some(p) {
                return self.the_running_session(p).await;
            }
            if let Some(fork_id) = self.procs.environ(p).get("DSH_SESSION_ID").filter(|v| !v.is_empty()) {
                return json!({"ok": true, "mine": true, "session_id": r9(fork_id), "hosting": "web",
                              "fork_session_id": fork_id, "pid": p});
            }
            pid = self.procs.ppid(p) as i64;
            seen += 1;
        }
        json!({"ok": true, "mine": false})
    }

    /// The caller behind the fork's one MCP client: the only session with a turn running. The
    /// client carries no session id, so with none or several running dsh cannot tell.
    async fn the_running_session(&self, fork_pid: u32) -> Value {
        let (running, why) = match self.rows().await {
            Ok(rows) => {
                let running: Vec<Value> = rows.into_iter().filter(|r| r["state"] == "busy").collect();
                let why = if running.is_empty() {
                    "no session has a turn running".to_string()
                } else {
                    format!("{} sessions have a turn running", running.len())
                };
                (running, why)
            }
            Err(e) => (vec![], format!("the fork did not list its sessions: {e}")),
        };
        if let [row] = running.as_slice() {
            return json!({"ok": true, "mine": true, "session_id": row["session_id"], "hosting": "web",
                          "fork_session_id": row["fork_session_id"], "name": row["name"], "cwd": row["cwd"],
                          "pid": fork_pid});
        }
        json!({"ok": true, "mine": true, "session_id": null,
               "error": format!("the call came through the DeepSeek harness's one MCP client, which carries no session id, \
                                 and {why}, so dsh cannot tell which session called")})
    }

    /// The row a verb names by `session_id`, `ref` or the caller's `pid`.
    pub async fn target(&self, args: &Value, fork_pid: Option<u32>) -> Result<Value, ForkError> {
        let reference = contract::args::text(args.get("session_id"));
        let row = if let Some(r) = &reference {
            self.by_id(r).await?
        } else if truthy(args.get("pid")) {
            let who = self.resolve(pid_arg(args.get("pid")), fork_pid).await;
            let Some(sid) = who.get("session_id").and_then(Value::as_str) else {
                let why = who.get("error").and_then(Value::as_str).unwrap_or("the caller is no DeepSeek harness session");
                return Err(ForkError(why.to_string()));
            };
            self.by_id(sid).await?
        } else {
            return Err(ForkError("name a session with session_id".into()));
        };
        row.ok_or_else(|| {
            let shown = reference.as_deref().map(repr).unwrap_or_else(|| "None".into());
            ForkError(format!("no DeepSeek harness session matches {shown}"))
        })
    }

    pub async fn send(&self, row: &Value, text: &str) -> Result<Value, ForkError> {
        let fork_id = text_of(row, "fork_session_id");
        let confirmed = if text.starts_with('/') {
            self.fork.rpc("commands/execute", json!({"agentId": fork_id, "line": text})).await?;
            "executed"
        } else {
            self.fork
                .rpc("session.prompt", json!({"sessionId": fork_id, "mode": "queue", "content": [{"type": "text", "text": text}]}))
                .await?;
            "enqueued"
        };
        Ok(json!({"ok": true, "submitted": true, "confirmed": confirmed, "session": row["session_id"], "text": text}))
    }

    pub async fn interrupt(&self, row: &Value) -> Result<Value, ForkError> {
        self.fork.rpc("session.cancel", json!({"sessionId": row["fork_session_id"]})).await?;
        Ok(json!({"ok": true, "interrupted": true}))
    }

    pub async fn stop(&self, row: &Value) -> Result<Value, ForkError> {
        self.fork.rpc("session.cancel", json!({"sessionId": row["fork_session_id"]})).await?;
        Ok(json!({"ok": true, "stopped": true, "unloaded": false,
                  "note": "the fork has no route that unloads a session; its turn is cancelled"}))
    }

    pub async fn set(&self, row: &Value, args: &Value, provider: Option<String>) -> Result<Value, ForkError> {
        if truthy(args.get("cwd")) {
            return Ok(unsupported("move a session: its folder is fixed when it is created"));
        }
        let mut applied = Map::new();
        if truthy(args.get("model")) || truthy(args.get("effort")) {
            let mut payload = json!({"sessionId": row["fork_session_id"], "provider": provider,
                                     "model": args.get("model").cloned().unwrap_or(Value::Null)});
            if truthy(args.get("effort")) {
                payload["reasoningEffort"] = args["effort"].clone();
            }
            applied.insert("model".into(), self.fork.rpc("session.selectModel", payload).await?);
        }
        if truthy(args.get("name")) {
            let payload = json!({"sessionId": row["fork_session_id"], "title": args["name"]});
            applied.insert("name".into(), self.fork.rpc("session.rename", payload).await?);
        }
        if applied.is_empty() {
            return Ok(contract::refusal("set needs one of model, effort or name"));
        }
        Ok(json!({"ok": true, "applied": applied}))
    }

    pub async fn start(&self, args: &Value, workspace: &Path, provider: Option<String>) -> Result<Value, ForkError> {
        let (Some(project), Some(scope)) = (nonempty(args.get("project")), nonempty(args.get("scope"))) else {
            return Ok(json!({"ok": false, "reason": "start needs project and scope"}));
        };
        if truthy(args.get("pin")) || truthy(args.get("isolated")) {
            return Ok(unsupported("pin or isolate a session"));
        }
        let cwd = workspace.join("projects").join(project).join(scope);
        if !cwd.is_dir() {
            return Ok(json!({"ok": false, "reason": format!("no worktree at {}", cwd.display())}));
        }
        let fork_id = format!("session-{}", uuid::Uuid::new_v4());
        let mut payload = json!({"cwd": cwd, "sessionId": fork_id});
        if truthy(args.get("preset")) {
            payload["agentPreset"] = args["preset"].clone();
        }
        self.fork.rpc("session.create", payload).await?;
        let row = json!({"session_id": r9(&fork_id), "fork_session_id": fork_id});
        if ["name", "model", "effort"].iter().any(|k| truthy(args.get(*k))) {
            let wanted: Map<String, Value> =
                ["name", "model", "effort"].iter().map(|k| (k.to_string(), args.get(*k).cloned().unwrap_or(Value::Null))).collect();
            self.set(&row, &Value::Object(wanted), provider).await?;
        }
        if let Some(prompt) = nonempty(args.get("prompt")) {
            self.send(&row, prompt).await?;
        }
        Ok(json!({"ok": true, "session_id": row["session_id"], "fork_session_id": fork_id,
                  "name": args.get("name").cloned().unwrap_or(Value::Null), "cwd": cwd}))
    }

    pub async fn resume(&self, row: &Value, prompt: Option<&str>) -> Result<Value, ForkError> {
        if let Some(p) = prompt.filter(|p| !p.is_empty()) {
            self.send(row, p).await?;
        }
        Ok(json!({"ok": true, "session_id": row["session_id"], "live": true}))
    }

    async fn events_since(&self, fork_id: &str, seq: Option<i64>) -> Result<Vec<Value>, ForkError> {
        let page = self.fork.rpc("session.history", json!({"sessionId": fork_id, "maxMessages": 200})).await?;
        let events = page.get("events").and_then(Value::as_array).cloned().unwrap_or_default();
        Ok(events.into_iter().filter(|e| seq.is_none_or(|s| e.get("seq").and_then(Value::as_i64).unwrap_or(0) > s)).collect())
    }

    async fn last_seq(&self, fork_id: &str) -> Result<Option<i64>, ForkError> {
        Ok(self.events_since(fork_id, None).await?.iter().filter_map(|e| e.get("seq").and_then(Value::as_i64)).max())
    }

    /// Wait for `compaction/end`, queue the follow-up, and report to reflection how it ended.
    pub async fn watch_compaction(&self, row: &Value, followup: &str, ticket: &str, since: Option<i64>) -> Kind {
        let fork_id = text_of(row, "fork_session_id");
        let report = |kind: Kind, reason: Option<String>| {
            let mut e = HarnessEvent::new(text_of(row, "session_id"), kind);
            e.ticket = Some(ticket.into());
            e.command = Some("/compact".into());
            e.followup = Some(followup.into());
            e.cwd = row.get("cwd").and_then(Value::as_str).map(str::to_string);
            e.name = Some(text_of(row, "name"));
            e.reason = reason;
            e
        };
        let deadline = Instant::now() + self.compact_wait;
        let ended = loop {
            if Instant::now() >= deadline {
                let why = format!("the compaction did not end within {} minutes", self.compact_wait.as_secs() / 60);
                self.reporter.report(&report(Kind::Lost, Some(why))).await;
                return Kind::Lost;
            }
            match self.events_since(&fork_id, since).await {
                Ok(events) => {
                    let kind = |e: &Value| {
                        nonempty(e.get("type")).or_else(|| nonempty(e.get("kind"))).unwrap_or("").to_string()
                    };
                    if let Some(end) = events.into_iter().find(|e| kind(e) == "compaction/end") {
                        break end;
                    }
                }
                Err(e) => tracing::warn!("dsh: {e}"),
            }
            tokio::time::sleep(self.poll).await;
        };
        let body = ended.get("payload").filter(|p| truthy(Some(p))).unwrap_or(&ended);
        if let Some(error) = body.get("error").filter(|e| truthy(Some(e))) {
            let error = error.as_str().map(str::to_string).unwrap_or_else(|| error.to_string());
            self.reporter.report(&report(Kind::Lost, Some(format!("the compaction failed: {error}")))).await;
            return Kind::Lost;
        }
        if let Err(e) = self.send(row, followup).await {
            self.reporter.report(&report(Kind::Lost, Some(format!("the follow-up did not go in: {e}")))).await;
            return Kind::Lost;
        }
        self.reporter.report(&report(Kind::Done, None)).await;
        Kind::Done
    }

    /// Send `/compact`. With a follow-up, answer a ticket and hand back the watch to run.
    pub async fn compact(&self, row: &Value, followup: Option<&str>) -> Result<(Value, Option<(String, Option<i64>)>), ForkError> {
        if row.get("preset").and_then(Value::as_str) == Some("minimal") {
            return Ok((unsupported("compact a session of the minimal preset"), None));
        }
        let since = self.last_seq(&text_of(row, "fork_session_id")).await?;
        self.send(row, "/compact").await?;
        let mut out = json!({"ok": true, "submitted": true, "text": "/compact", "followup": followup});
        let watch = followup.filter(|f| !f.is_empty()).map(|_| {
            let ticket = uuid::Uuid::new_v4().simple().to_string();
            out["ticket"] = json!(ticket);
            out["followup_deferred"] = json!(true);
            (ticket, since)
        });
        Ok((out, watch))
    }
}

/// A `pid` argument as Python's `int()` reads it: an integer, an integer string or a bool.
pub fn pid_arg(v: Option<&Value>) -> Option<i64> {
    match v? {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s.trim().parse().ok(),
        Value::Bool(b) => Some(*b as i64),
        _ => None,
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::fork::stub::{self, Stub};
    use std::sync::Mutex;

    pub const FORK: &str = "session-0e380e38-0e38-4e38-8e38-0e380e380e38";
    pub const R9: &str = "ds0e380e380e384e388e380e380e380e38";

    #[derive(Default)]
    pub struct FakeProcs {
        pub env: HashMap<u32, HashMap<String, String>>,
        pub parents: HashMap<u32, u32>,
    }

    impl Procs for FakeProcs {
        fn environ(&self, pid: u32) -> HashMap<String, String> {
            self.env.get(&pid).cloned().unwrap_or_default()
        }
        fn ppid(&self, pid: u32) -> u32 {
            self.parents.get(&pid).copied().unwrap_or(0)
        }
    }

    pub type Seen = Arc<Mutex<Vec<Value>>>;

    /// A reporter that records each event and answers `reply` (true: taken).
    pub fn recording(spool: std::path::PathBuf, taken: bool) -> (Reporter, Seen) {
        let seen: Seen = Arc::default();
        let s = seen.clone();
        let send: contract::Sender = Arc::new(move |v| {
            s.lock().unwrap().push(v);
            Box::pin(async move { if taken { Ok(json!({"ok": true})) } else { Err("reflection is down".to_string()) } })
        });
        (Reporter::with_sender(send, spool), seen)
    }

    pub fn default_items() -> Vec<Value> {
        vec![
            json!({"sessionId": FORK, "running": false, "title": "web", "cwd": "/w/projects/awm/redux-harness", "agentPreset": "code"}),
            json!({"sessionId": "odd-id", "running": true}),
        ]
    }

    pub struct Rig {
        pub stub: Stub,
        pub s: Sessions,
        pub events: Seen,
        pub _dir: tempfile::TempDir,
    }

    pub async fn rig_with(procs: FakeProcs, items: Vec<Value>) -> Rig {
        let stub = Stub::default();
        stub.with(|g| g.items = items);
        let port = stub::serve(stub.clone()).await;
        let dir = tempfile::tempdir().unwrap();
        let (reporter, events) = recording(dir.path().join("unsent.jsonl"), true);
        let s = Sessions {
            fork: Fork::new(port),
            procs: Box::new(procs),
            reporter,
            lifecycle: Lifecycle::new().0,
            compact_wait: COMPACT_WAIT,
            poll: Duration::from_millis(5),
        };
        Rig { stub, s, events, _dir: dir }
    }

    pub async fn rig() -> Rig {
        rig_with(FakeProcs::default(), default_items()).await
    }

    #[tokio::test]
    async fn list_leaves_out_a_session_with_no_uuid() {
        let r = rig().await;
        let rows = r.s.rows().await.unwrap();
        assert_eq!(rows.len(), 1);
        let row = &rows[0];
        assert_eq!((row["session_id"].as_str(), row["project"].as_str(), row["scope"].as_str(), row["state"].as_str()),
                   (Some(R9), Some("awm"), Some("redux-harness"), Some("idle")));
    }

    #[tokio::test]
    async fn by_id_takes_the_whole_id_and_named_the_whole_title() {
        let r = rig().await;
        assert_eq!(r.s.by_id(R9).await.unwrap().unwrap()["session_id"], R9);
        assert!(r.s.by_id(&R9[..10]).await.unwrap().is_none());
        assert_eq!(r.s.named("WEB").await.unwrap()[0]["session_id"], R9);
        assert!(r.s.named("we").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn plain_text_is_queued_and_a_slash_command_is_executed() {
        let r = rig().await;
        let row = r.s.by_id(R9).await.unwrap().unwrap();
        assert_eq!(r.s.send(&row, "hello").await.unwrap()["confirmed"], "enqueued");
        assert_eq!(r.stub.last(), ("session.prompt".into(),
                   json!({"sessionId": FORK, "mode": "queue", "content": [{"type": "text", "text": "hello"}]})));
        r.s.send(&row, "/compact").await.unwrap();
        assert_eq!(r.stub.last(), ("commands/execute".into(), json!({"agentId": FORK, "line": "/compact"})));
    }

    #[tokio::test]
    async fn model_and_effort_go_in_one_select() {
        let r = rig().await;
        let row = r.s.by_id(R9).await.unwrap().unwrap();
        r.s.set(&row, &json!({"model": "deepseek-v4", "effort": "high"}), Some("or".into())).await.unwrap();
        assert_eq!(r.stub.last(), ("session.selectModel".into(),
                   json!({"sessionId": FORK, "provider": "or", "model": "deepseek-v4", "reasoningEffort": "high"})));
    }

    #[tokio::test]
    async fn start_preallocates_the_id_so_it_knows_the_r9_first() {
        let r = rig().await;
        let ws = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(ws.path().join("projects/awm/x")).unwrap();
        let out = r.s.start(&json!({"project": "awm", "scope": "x", "name": "n", "prompt": "go"}), ws.path(), None).await.unwrap();
        let (method, payload) = r.stub.calls()[0].clone();
        assert_eq!(method, "session.create");
        assert_eq!(payload["sessionId"], out["fork_session_id"]);
        assert_eq!(out["session_id"].as_str(), r9(out["fork_session_id"].as_str().unwrap()).as_deref());
        assert_eq!(r.stub.methods(), ["session.create", "session.rename", "session.prompt"]);
    }

    #[tokio::test]
    async fn start_refuses_a_missing_worktree_and_pins() {
        let r = rig().await;
        let ws = tempfile::tempdir().unwrap();
        assert_eq!(r.s.start(&json!({"project": "a", "scope": "b"}), ws.path(), None).await.unwrap()["ok"], false);
        assert_eq!(r.s.start(&json!({"project": "a", "scope": "b", "pin": true}), ws.path(), None).await.unwrap()["unsupported"], true);
    }

    #[tokio::test]
    async fn compact_queues_the_followup_after_compaction_end_and_reports_done() {
        let r = rig().await;
        r.stub.with(|g| g.history = vec![json!({"seq": 4, "type": "message"})]);
        let row = r.s.by_id(R9).await.unwrap().unwrap();
        let (out, watch) = r.s.compact(&row, Some("carry on")).await.unwrap();
        assert!(out["ticket"].as_str().is_some_and(|t| !t.is_empty()));
        assert_eq!(out["followup_deferred"], true);
        let (ticket, since) = watch.unwrap();
        assert_eq!(since, Some(4));
        r.stub.with(|g| {
            g.history.push(json!({"seq": 5, "type": "compaction/start"}));
            g.history.push(json!({"seq": 6, "type": "compaction/end", "payload": {}}));
        });
        assert_eq!(r.s.watch_compaction(&row, "carry on", &ticket, since).await, Kind::Done);
        assert_eq!(r.stub.last().1["content"][0]["text"], "carry on");
        let ev = r.events.lock().unwrap().last().cloned().unwrap();
        assert_eq!((ev["kind"].as_str(), ev["ticket"].as_str()), (Some("done"), out["ticket"].as_str()));
    }

    #[tokio::test]
    async fn a_failed_compaction_is_lost_and_types_nothing() {
        let r = rig().await;
        let row = r.s.by_id(R9).await.unwrap().unwrap();
        r.stub.with(|g| g.history = vec![json!({"seq": 9, "type": "compaction/end", "payload": {"error": "overflow"}})]);
        assert_eq!(r.s.watch_compaction(&row, "carry on", "t", Some(8)).await, Kind::Lost);
        assert!(r.events.lock().unwrap().last().unwrap()["reason"].as_str().unwrap().contains("overflow"));
        assert!(!r.stub.methods().iter().any(|m| m == "session.prompt"));
    }

    #[tokio::test]
    async fn an_event_reflection_refuses_is_spooled() {
        let mut r = rig().await;
        let spool = r._dir.path().join("refused.jsonl");
        let (reporter, _) = recording(spool.clone(), false);
        r.s.reporter = reporter;
        let row = r.s.by_id(R9).await.unwrap().unwrap();
        r.stub.with(|g| g.history = vec![json!({"seq": 2, "type": "compaction/end"})]);
        assert_eq!(r.s.watch_compaction(&row, "carry on", "t1", Some(1)).await, Kind::Done);
        let line: Value = serde_json::from_str(std::fs::read_to_string(&spool).unwrap().trim()).unwrap();
        assert_eq!((line["kind"].as_str(), line["ticket"].as_str(), line["session_id"].as_str()), (Some("done"), Some("t1"), Some(R9)));
    }

    #[tokio::test]
    async fn resolve_reads_the_bash_tools_session_id() {
        let procs = FakeProcs {
            env: HashMap::from([(100, HashMap::from([("DSH_SESSION_ID".to_string(), FORK.to_string())])), (200, HashMap::new())]),
            parents: HashMap::from([(300, 200), (200, 100), (100, 1)]),
        };
        let r = rig_with(procs, default_items()).await;
        let out = r.s.resolve(Some(300), Some(50)).await;
        assert_eq!((out["mine"].clone(), out["session_id"].clone()), (json!(true), json!(R9)));
    }

    fn two_items(running: &[usize]) -> Vec<Value> {
        ["00000000-0000-4000-8000-000000000000", "11111111-1111-4111-8111-111111111111"]
            .iter()
            .enumerate()
            .map(|(i, u)| json!({"sessionId": format!("session-{u}"), "running": running.contains(&i), "cwd": "/w"}))
            .collect()
    }

    fn under_the_fork() -> FakeProcs {
        FakeProcs { env: HashMap::new(), parents: HashMap::from([(60, 50)]) }
    }

    #[tokio::test]
    async fn the_only_running_session_is_the_mcp_caller() {
        let items = two_items(&[1]);
        let want = r9(items[1]["sessionId"].as_str().unwrap());
        let r = rig_with(under_the_fork(), items).await;
        let out = r.s.resolve(Some(60), Some(50)).await;
        assert_eq!(out["mine"], true);
        assert_eq!(out["session_id"].as_str(), want.as_deref());
    }

    #[tokio::test]
    async fn resolve_disowns_a_stranger() {
        let procs = FakeProcs { env: HashMap::new(), parents: HashMap::from([(77, 1)]) };
        let r = rig_with(procs, default_items()).await;
        assert_eq!(r.s.resolve(Some(77), Some(50)).await, json!({"ok": true, "mine": false}));
        assert_eq!(r.s.resolve(None, Some(50)).await, json!({"ok": true, "mine": false, "error": "no pid"}));
    }

}
