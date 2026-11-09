//! The `operations` verbs a session calls (queue.md R145, fleet.md R106).
//!
//! A verb names the calling session by what the gateway stamped on the call: on this node
//! the pid, which reflection turns into the session's R9 id; through alcor's singleton door
//! the claim the door stamped, with the node it verified. A service with no session passes
//! `session_id` instead, except where only the session itself may act: an approval request
//! records the calling session, and only that session cancels it (fleet.md R97 item 1).
//! Every write goes through the relay, so it waits in the outbox while alcor is away.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::arbutus::Approvals;
use crate::relay::{async_trait_free::BoxFut, ClientError, Relay};

pub const NEEDS_YOU_STATES: [&str; 3] = ["planned", "blocked", "review"];
pub const TITLE_CHARS: usize = 80;
pub const NOTICE: &str = "This session's work is card {task_id} on Tony's Swarm board. Move it with operations task_report, and call request_human when you need Tony.";

/// What the front asks of other plugins. Each degrades: a missing answer is no session, no
/// notice or no DM, never a failed call.
pub trait Outside: Send + Sync {
    /// The session behind a pid on this node: `{session_id, job, name}`.
    fn session_of(&self, pid: u32) -> BoxFut<'_, Option<Value>>;
    /// Type one line into a session, on `peer` when it is another node.
    fn tell<'a>(&'a self, peer: Option<&'a str>, job: &'a str, text: &'a str) -> BoxFut<'a, bool>;
    /// One line to Tony's notifications channel.
    fn dm<'a>(&'a self, text: &'a str) -> BoxFut<'a, bool>;
    /// One message in Tony's own Discord DM.
    fn dm_tony<'a>(&'a self, text: &'a str) -> BoxFut<'a, bool>;
    /// Turn a job's Remote Control on, when it is off, and say how it can be reached.
    fn reach<'a>(&'a self, node: &'a str, job: &'a str) -> BoxFut<'a, Reach>;
    /// Each job the roster holds on a node, with its claude.ai link. None when the roster has
    /// no answer for that node.
    fn links<'a>(&'a self, node: &'a str) -> BoxFut<'a, Option<HashMap<String, Option<String>>>>;
}

/// How a session can be reached after asking for Remote Control.
#[derive(Debug, Clone, PartialEq)]
pub enum Reach {
    Link(String),
    /// `/rc` is typed and connects when the session's turn ends.
    Connecting,
    Unavailable,
}

const RC_PENDING: &str = "rc:";

fn rc_key(node: &str, job: &str) -> String {
    format!("{RC_PENDING}{node}:{job}")
}

/// Who is calling, from the gateway's stamps.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Stamp {
    pub pid: Option<u32>,
    /// `_caller_session_id`: the R9 id of an isolated session.
    pub session_id: Option<String>,
    /// `_caller_session`: the claim of a session on another node.
    pub claim: Option<Value>,
    /// The node a call from another node's service came from, by its `as` stamp.
    pub peer: Option<String>,
}

impl Stamp {
    pub fn is_session(&self) -> bool {
        self.pid.is_some() || self.session_id.is_some() || self.claim.is_some()
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Caller {
    pub session_id: Option<String>,
    pub job: Option<String>,
    pub name: Option<String>,
    /// The session's own node, when it is not this one.
    pub node: Option<String>,
}

pub type Answer = Result<Value, String>;

fn unwrap_stale(out: Value) -> Value {
    if out.get("stale") == Some(&json!(true)) && out.get("result").is_some() {
        return out["result"].clone();
    }
    out
}

fn norm(s: &str) -> String {
    awm_plugin_sdk::ids::normalise(s).unwrap_or_else(|_| s.trim().to_lowercase())
}

/// A session id a caller passed: refused unless it is an R9 id.
fn norm_arg(s: &str) -> Result<String, String> {
    awm_plugin_sdk::ids::normalise(s).map_err(|e| e.to_string())
}

fn err(e: ClientError) -> String {
    e.to_string()
}

fn str_of(a: &Value, k: &str) -> Option<String> {
    match a.get(k) {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

fn merge(mut a: Value, b: Value) -> Value {
    if let (Some(m), Value::Object(extra)) = (a.as_object_mut(), b) {
        m.extend(extra);
    }
    a
}

/// A card title from free text: whitespace folded, at most 80 characters, cut at a word.
pub fn title_of(text: &str) -> String {
    let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if text.chars().count() <= TITLE_CHARS {
        return text;
    }
    let cut: String = text.chars().take(TITLE_CHARS - 1).collect();
    let cut = match cut.rfind(' ') {
        Some(i) => cut[..i].to_string(),
        None => cut,
    };
    format!("{cut}…")
}

/// The first Markdown heading of a plan.
pub fn heading(text: &str) -> Option<String> {
    text.lines().find_map(|l| {
        let t = l.trim_end();
        let rest = t.strip_prefix('#')?;
        let rest = rest.trim_start_matches('#');
        if !rest.starts_with(char::is_whitespace) {
            return None;
        }
        let h = rest.trim().trim_end_matches('#').trim_end();
        (!h.is_empty()).then(|| h.to_string())
    })
}

pub struct Front {
    pub relay: Arc<Relay>,
    pub node: String,
    pub outside: Arc<dyn Outside>,
    pub arbutus: Option<Arc<Approvals>>,
    /// The board's link to one card, with `{task_id}` in it.
    pub card_url: Option<String>,
    /// The approval modal's origin.
    pub approver_origin: String,
    pub remind_s: f64,
    pub clock: crate::store::Clock,
    /// How long a no-card DM waits for a queued Remote Control's link before it goes without one.
    pub link_wait: Duration,
    /// The Remote Control marks of DMs still being composed: their link belongs in that DM.
    pub asking: Mutex<HashSet<String>>,
    pub roster_moved: tokio::sync::Notify,
}

pub const VERBS: [&str; 27] = [
    "task_open", "task_attach", "task_join", "task_report", "task_session_end", "task_for_session", "task_sessions", "task_get",
    "task_list", "request_human", "hook_prompt", "hook_plan", "hook_plan_approved", "hook_todos", "hook_session_clear", "claim_task",
    "track_task", "swarm_message_post", "swarm_messages", "approval_request", "approval_get",
    "approval_wait", "approval_cancel", "order_pull", "order_done", "keys", "status",
];

impl Front {
    pub fn card_link(&self, task_id: &str) -> Value {
        match &self.card_url {
            Some(t) if !task_id.is_empty() => json!(t.replace("{task_id}", task_id)),
            _ => Value::Null,
        }
    }

    // -- who is calling ----------------------------------------------------------------

    async fn identify(&self, stamp: &Stamp) -> Caller {
        if let Some(s) = &stamp.session_id {
            return Caller { session_id: Some(norm(s)), ..Caller::default() };
        }
        if let Some(c) = stamp.claim.as_ref().filter(|c| str_of(c, "session_id").is_some()) {
            let node = str_of(c, "node");
            return Caller {
                session_id: str_of(c, "session_id").map(|s| norm(&s)),
                job: str_of(c, "job"),
                name: str_of(c, "name"),
                node: node.filter(|n| *n != self.node),
            };
        }
        let service = || Caller { node: stamp.peer.clone().filter(|n| *n != self.node), ..Caller::default() };
        let Some(pid) = stamp.pid else { return service() };
        let Some(rec) = self.outside.session_of(pid).await else { return service() };
        Caller { session_id: str_of(&rec, "session_id").map(|s| norm(&s)), job: str_of(&rec, "job"), name: str_of(&rec, "name"), node: None }
    }

    fn job_key(&self, c: &Caller) -> String {
        match &c.node {
            Some(n) => format!("job:{n}:{}", c.job.clone().unwrap_or_default()),
            None => format!("job:{}", c.job.clone().unwrap_or_default()),
        }
    }

    async fn caller(&self, stamp: &Stamp) -> Caller {
        let c = self.identify(stamp).await;
        if let (Some(_), Some(s)) = (&c.job, &c.session_id) {
            self.relay.set_mark(&self.job_key(&c), s);
        }
        c
    }

    /// The node and job a write records for the caller's session.
    fn whereabouts(c: &Caller) -> Value {
        let mut m = Map::new();
        if let Some(n) = &c.node {
            m.insert("node".into(), json!(n));
        }
        if let Some(j) = &c.job {
            m.insert("job".into(), json!(j));
        }
        Value::Object(m)
    }

    async fn session(&self, args: &Value, stamp: &Stamp, required: bool) -> Result<(Option<String>, Caller), String> {
        let c = self.caller(stamp).await;
        let session = str_of(args, "session_id").map(|s| norm_arg(&s)).transpose()?.or_else(|| c.session_id.clone());
        if required && session.is_none() {
            return Err("no calling session: pass session_id".into());
        }
        Ok((session, c))
    }

    async fn own(&self, args: &Value, stamp: &Stamp) -> Result<Caller, String> {
        let c = self.caller(stamp).await;
        let Some(sid) = c.session_id.clone() else { return Err("this verb is for a running session: no calling session".into()) };
        if let Some(given) = str_of(args, "session_id") {
            if norm_arg(&given)? != sid {
                return Err("session_id does not match the calling session".into());
            }
        }
        Ok(c)
    }

    async fn task_of(&self, session: Option<&str>) -> Result<Option<String>, String> {
        let Some(s) = session else { return Ok(None) };
        let out = unwrap_stale(self.relay.read("task_for_session", json!({"session_id": s})).await.map_err(err)?);
        Ok(out.as_str().map(str::to_string))
    }

    // -- the task verbs (sessions.md R57, R58) ------------------------------------------

    pub async fn task_open(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, c) = self.session(a, st, false).await?;
        let title = str_of(a, "title").or_else(|| c.name.clone()).unwrap_or_default();
        let w = merge(json!({"title": title.trim(), "session_id": session, "peer": a.get("peer"), "tags": a.get("tags"),
                             "name": str_of(a, "name").or_else(|| c.name.clone())}), Front::whereabouts(&c));
        Ok(merge(json!({"ok": true}), self.relay.write("task_open", w).await.map_err(err)?))
    }

    pub async fn task_attach(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, c) = self.session(a, st, true).await?;
        let role = str_of(a, "role").unwrap_or_else(|| "worker".into());
        let w = merge(json!({"task_id": a.get("task_id"), "session_id": session, "role": role,
                             "name": str_of(a, "name").or_else(|| c.name.clone())}), Front::whereabouts(&c));
        let out = self.relay.write("task_attach", w).await.map_err(err)?;
        if let (Some(sid), Some(task_id), "main") = (&session, str_of(a, "task_id"), role.as_str()) {
            self.relay.set_mark(&format!("needs:{sid}"), &task_id);
        }
        Ok(merge(json!({"ok": true}), out))
    }

    pub async fn task_join(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, c) = self.session(a, st, true).await?;
        let w = merge(json!({"task_id": a.get("task_id"), "session_id": session}), Front::whereabouts(&c));
        let out = self.relay.write("task_join", w).await.map_err(err)?;
        let main = str_of(&out, "main_session_id");
        let mut told = false;
        if let (Some(m), Some(name)) = (main.as_ref(), str_of(&out, "main_name")) {
            if Some(m) != session.as_ref() {
                let text = format!("[awm] session {} joined your task {} (operations task_sessions shows it).",
                                   session.clone().unwrap_or_default(), out["task_id"].as_str().unwrap_or_default());
                told = self.outside.tell(out.get("main_node").and_then(Value::as_str), &name, &text).await;
            }
        }
        Ok(merge(merge(json!({"ok": true}), out), json!({"main_notified": told})))
    }

    pub async fn task_report(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, _) = self.session(a, st, false).await?;
        let task_id = match str_of(a, "task_id") {
            Some(t) => Some(t),
            None => self.task_of(session.as_deref()).await?,
        };
        let Some(task_id) = task_id else { return Err("no task: pass task_id, or open one with task_open".into()) };
        self.report(&task_id, session.as_deref(), str_of(a, "state").unwrap_or_default().as_str(), a.get("reason").cloned()).await
    }

    async fn report(&self, task_id: &str, session: Option<&str>, state: &str, reason: Option<Value>) -> Answer {
        let out = self.relay.write("task_report", json!({"task_id": task_id, "session_id": session, "state": state,
                                                          "reason": reason})).await.map_err(err)?;
        if let Some(s) = session {
            if NEEDS_YOU_STATES.contains(&state) {
                self.relay.set_mark(&format!("needs:{s}"), task_id);
            } else {
                self.relay.clear_mark(&format!("needs:{s}"));
            }
        }
        Ok(merge(json!({"ok": true}), out))
    }

    pub async fn task_session_end(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, _) = self.session(a, st, true).await?;
        let out = self.relay.write("task_session_end", json!({"task_id": a.get("task_id"), "session_id": session})).await.map_err(err)?;
        Ok(merge(json!({"ok": true}), out))
    }

    pub async fn task_for_session(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, _) = self.session(a, st, true).await?;
        Ok(json!({"ok": true, "session_id": session, "task_id": self.task_of(session.as_deref()).await?}))
    }

    pub async fn task_sessions(&self, a: &Value) -> Answer {
        let out = self.relay.read("task_sessions", json!({"task_id": a.get("task_id")})).await.map_err(err)?;
        Ok(json!({"ok": true, "task_id": a.get("task_id"), "sessions": unwrap_stale(out)}))
    }

    pub async fn task_get(&self, a: &Value) -> Answer {
        let out = self.relay.read("task_get", json!({"task_id": a.get("task_id")})).await.map_err(err)?;
        let tid = str_of(a, "task_id").unwrap_or_default();
        Ok(json!({"ok": true, "task": unwrap_stale(out), "card_url": self.card_link(&tid)}))
    }

    pub async fn task_list(&self, a: &Value) -> Answer {
        let mut keep = Map::new();
        for k in ["section", "q", "before", "limit"] {
            if let Some(v) = a.get(k).filter(|v| !v.is_null()) {
                keep.insert(k.into(), v.clone());
            }
        }
        let out = unwrap_stale(self.relay.read("task_list", Value::Object(keep)).await.map_err(err)?);
        Ok(merge(json!({"ok": true}), out))
    }

    // -- Tony (queue.md R61 item 2) --------------------------------------------------------

    async fn card_for(&self, session: Option<&str>, c: &Caller, reason: &str) -> Result<String, String> {
        if let Some(t) = self.task_of(session).await? {
            return Ok(t);
        }
        let title = c.name.clone().filter(|n| !n.is_empty()).unwrap_or_else(|| {
            let t = title_of(reason);
            if t.is_empty() { "A session needs Tony".into() } else { t }
        });
        let out = self.relay.write("task_open", merge(json!({"title": title, "session_id": session, "name": c.name}), Front::whereabouts(c)))
            .await.map_err(err)?;
        Ok(out["task_id"].as_str().unwrap_or_default().to_string())
    }

    /// Tell Tony you need him: blocked, review, or an Arbutus admin approval. A caller that
    /// holds no card reaches him by DM instead, with a link to its session.
    pub async fn request_human(&self, a: &Value, st: &Stamp) -> Answer {
        let kind = match str_of(a, "kind").map(|k| k.trim().to_string()).filter(|k| !k.is_empty()) {
            Some(k) => k,
            None if a.get("summary").is_some_and(|v| !v.is_null()) && a.get("reason").is_none_or(Value::is_null) => "review".into(),
            None => "blocked".into(),
        };
        let (session, c) = self.session(a, st, false).await?;
        if kind == "approval" {
            return self.arbutus_open(a, session.as_deref(), &c).await;
        }
        if kind != "blocked" && kind != "review" {
            return Err("kind must be blocked, review or approval".into());
        }
        let text = if kind == "blocked" { str_of(a, "reason") } else { str_of(a, "summary").or_else(|| str_of(a, "reason")) }
            .unwrap_or_default().trim().to_string();
        if text.is_empty() {
            return Err(if kind == "blocked" { "reason is required" } else { "summary is required" }.into());
        }
        let Some(task_id) = self.task_of(session.as_deref()).await? else {
            return self.ask_by_dm(&kind, &text, &c).await;
        };
        let mut out = self.report(&task_id, session.as_deref(), &kind, Some(json!(text))).await?;
        out["card_url"] = self.card_link(&task_id);
        if kind == "blocked" && a.get("urgent") == Some(&json!(true)) {
            let node = c.node.clone().unwrap_or_else(|| self.node.clone());
            let who = c.name.clone().unwrap_or_else(|| task_id.clone());
            let attach = c.job.clone().or_else(|| c.name.clone()).unwrap_or_else(|| "<job>".into());
            let message = format!("**{who}** on **{node}** needs you.\n{text}\nCard {task_id}. Attach: `claude attach {attach}` on {node}");
            out["notified"] = json!(self.outside.dm(&message).await);
            self.relay.set_mark(&format!("remind:{task_id}"), &json!({"due": (self.clock)() + self.remind_s, "text": message}).to_string());
        }
        Ok(out)
    }

    async fn ask_by_dm(&self, kind: &str, text: &str, c: &Caller) -> Answer {
        let node = c.node.as_deref().unwrap_or(&self.node);
        let key = c.job.as_ref().map(|job| rc_key(node, job));
        if let Some(k) = &key {
            self.asking.lock().unwrap().insert(k.clone());
        }
        let out = self.send_ask(kind, text, c, node).await;
        if let Some(k) = &key {
            self.asking.lock().unwrap().remove(k);
            if self.relay.mark(k).is_some() {
                self.links_due().await;
            }
        }
        out
    }

    async fn send_ask(&self, kind: &str, text: &str, c: &Caller, node: &str) -> Answer {
        let who = c.name.as_deref().or(c.job.as_deref()).unwrap_or("A service");
        let reach = match &c.job {
            Some(job) => self.turn_on_rc(node, job, who).await,
            None => Reach::Unavailable,
        };
        let need = if kind == "review" { "has something for you to review" } else { "needs you" };
        let mut message = format!("**{who}** on **{node}** {need}.\n{text}");
        let attach = c.job.as_ref().map(|job| format!("\nAttach: `claude attach {job}` on {node}")).unwrap_or_default();
        let (link, note) = match &reach {
            Reach::Link(url) => {
                message.push_str(&format!("\n{url}"));
                (Some(url), None)
            }
            Reach::Connecting => {
                message.push_str(&format!("{attach}\nRemote Control connects when this session ends its turn. The link follows then."));
                (None, Some("Remote Control connects when you end your turn, and Tony gets the link then."))
            }
            Reach::Unavailable => {
                message.push_str(&attach);
                (None, None)
            }
        };
        if !self.outside.dm_tony(&message).await {
            return Err("this session holds no card and the DM to Tony did not go out: try again".into());
        }
        let mut out = json!({"ok": true, "notified": true, "link": link});
        if let Some(note) = note {
            out["note"] = json!(note);
        }
        Ok(out)
    }

    /// The pending mark goes first, so a second call in the same turn types no second `/rc`:
    /// that one would land on a connected session and open its disconnect modal.
    async fn turn_on_rc(&self, node: &str, job: &str, who: &str) -> Reach {
        let key = rc_key(node, job);
        if self.relay.mark(&key).is_some() {
            return Reach::Connecting;
        }
        self.relay.set_mark(&key, who);
        let reach = match self.outside.reach(node, job).await {
            Reach::Connecting => self.link_soon(node, job).await.map_or(Reach::Connecting, Reach::Link),
            other => other,
        };
        if reach != Reach::Connecting {
            self.relay.clear_mark(&key);
        }
        reach
    }

    async fn link_soon(&self, node: &str, job: &str) -> Option<String> {
        let deadline = tokio::time::Instant::now() + self.link_wait;
        loop {
            let moved = self.roster_moved.notified();
            tokio::pin!(moved);
            moved.as_mut().enable();
            let links = tokio::time::timeout_at(deadline, self.outside.links(node)).await.ok()?;
            if let Some(url) = links.and_then(|mut links| links.remove(job)).flatten() {
                return Some(url);
            }
            tokio::time::timeout_at(deadline, moved).await.ok()?;
        }
    }

    /// Send Tony the link of each session whose Remote Control has connected since it asked,
    /// and forget each session that went. Answers how many went out.
    pub async fn links_due(&self) -> usize {
        self.roster_moved.notify_waiters();
        let mut sent = 0;
        let mut rosters = HashMap::new();
        for (key, who) in self.relay.marks(RC_PENDING) {
            if self.asking.lock().unwrap().contains(&key) {
                continue;
            }
            let Some((node, job)) = key.strip_prefix(RC_PENDING).and_then(|k| k.split_once(':')) else { continue };
            if !rosters.contains_key(node) {
                rosters.insert(node.to_string(), self.outside.links(node).await);
            }
            let Some(links) = &rosters[node] else { continue };
            if let Some(link) = links.get(job) {
                let Some(url) = link else { continue };
                if !self.outside.dm_tony(&format!("**{who}** on **{node}**: Remote Control is on.\n{url}")).await {
                    continue;
                }
                sent += 1;
            }
            self.relay.clear_mark(&key);
        }
        sent
    }

    /// Send the one reminder of each urgent block that is still blocked. Answers how many went.
    pub async fn remind_due(&self) -> usize {
        let mut sent = 0;
        for (key, value) in self.relay.marks("remind:") {
            let task_id = key.trim_start_matches("remind:").to_string();
            let Ok(due) = serde_json::from_str::<Value>(&value) else {
                self.relay.clear_mark(&key);
                continue;
            };
            if (self.clock)() < due.get("due").and_then(Value::as_f64).unwrap_or(0.0) {
                continue;
            }
            let card = match self.relay.read("task_get", json!({"task_id": task_id})).await {
                Ok(v) => Some(unwrap_stale(v)).filter(Value::is_object),
                Err(_) => None,
            };
            match card {
                Some(c) if c["section"] == "needs_you" && c["status"] == "blocked" => {
                    let text = format!("Still waiting: {}", due.get("text").and_then(Value::as_str).unwrap_or(&task_id));
                    if !self.outside.dm(&text).await {
                        continue;
                    }
                    sent += 1;
                }
                None if self.relay.down_since().is_some() => continue,
                _ => {}
            }
            self.relay.clear_mark(&key);
        }
        sent
    }

    async fn arbutus_open(&self, a: &Value, session: Option<&str>, c: &Caller) -> Answer {
        let Some(arb) = self.arbutus.clone() else { return Err("this node takes no admin approvals".into()) };
        let agent = c.name.clone().or_else(|| c.job.clone()).unwrap_or_else(|| "a session".into());
        let node = c.node.clone().unwrap_or_else(|| self.node.clone());
        let req = arb.open(&str_of(a, "bundle").unwrap_or_default(), &agent, &node, c.job.as_deref().unwrap_or("")).await?;
        let (name, r) = (req["name"].as_str().unwrap_or_default(), req["ref"].as_str().unwrap_or_default().to_string());
        let reason = format!("Admin approval {name} (ref {r}) waits for Tony or Ryan in Slack.");
        let task_id = self.card_for(session, c, &reason).await?;
        let out = self.report(&task_id, session, "blocked", Some(json!(reason))).await?;
        arb.attach(&r, &task_id).map_err(|e| e.to_string())?;
        Ok(merge(out, json!({"approval": r, "notice": "A line is typed into this session when an admin decides."})))
    }

    /// An Arbutus decision moves its card back to Working.
    pub async fn arbutus_decided(&self, task_id: &str, line: &str) {
        if let Err(e) = self.relay.write("task_report", json!({"task_id": task_id, "state": "working", "reason": line})).await {
            tracing::warn!("operations: an Arbutus decision could not move card {task_id}: {e}");
        }
    }

    /// Tony's prompt to a session moves its card from Needs you to Working (R61 item 2).
    pub async fn hook_prompt(&self, a: &Value, st: &Stamp) -> Answer {
        let c = self.caller(st).await;
        let Some(sid) = c.session_id.clone() else { return Ok(json!({"ok": true, "moved": false})) };
        let Some(task_id) = self.relay.mark(&format!("needs:{sid}")) else { return Ok(json!({"ok": true, "moved": false, "blocked": false})) };
        self.report(&task_id, Some(&sid), "working", None).await?;
        self.autotitle(&task_id, &str_of(a, "prompt").unwrap_or_default(), false).await?;
        self.relay.clear_mark(&format!("remind:{task_id}"));
        Ok(json!({"ok": true, "moved": true, "task_id": task_id, "blocked": false}))
    }

    // -- the plan, todo and clear hooks, and a session's own card ---------------------------

    async fn card_or_open(&self, session: &str, c: &Caller, title: &str) -> Result<(String, bool), String> {
        if let Some(t) = self.task_of(Some(session)).await? {
            return Ok((t, false));
        }
        let mut t = title_of(title);
        if t.is_empty() {
            t = c.name.clone().filter(|n| !n.is_empty()).unwrap_or_else(|| format!("Work on {}", c.node.clone().unwrap_or_else(|| self.node.clone())));
        }
        let out = self.relay.write("task_open", merge(json!({"title": t, "session_id": session, "name": c.name}), Front::whereabouts(c)))
            .await.map_err(err)?;
        Ok((out["task_id"].as_str().unwrap_or_default().to_string(), true))
    }

    async fn autotitle(&self, task_id: &str, text: &str, from_plan: bool) -> Result<(), String> {
        let title = title_of(text);
        if !title.is_empty() {
            self.relay.write("task_autotitle", json!({"task_id": task_id, "title": title, "from_plan": from_plan})).await.map_err(err)?;
        }
        Ok(())
    }

    fn opened(out: Value, task_id: &str, opened: bool) -> Value {
        let mut out = merge(out, json!({"task_id": task_id, "opened": opened}));
        if opened {
            out["notice"] = json!(NOTICE.replace("{task_id}", task_id));
        }
        out
    }

    pub async fn hook_plan(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, c) = self.session(a, st, true).await?;
        let session = session.unwrap_or_default();
        let text = str_of(a, "plan_text").unwrap_or_default();
        let (task_id, opened) = self.card_or_open(&session, &c, &heading(&text).unwrap_or_default()).await?;
        self.relay.write("task_detail", json!({"task_id": task_id, "plan": text})).await.map_err(err)?;
        self.autotitle(&task_id, &heading(&text).unwrap_or_default(), true).await?;
        let out = self.report(&task_id, Some(&session), "planned", None).await?;
        Ok(Front::opened(merge(out, json!({"blocked": true})), &task_id, opened))
    }

    pub async fn hook_plan_approved(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, c) = self.session(a, st, true).await?;
        let session = session.unwrap_or_default();
        let text = str_of(a, "plan_text").unwrap_or_default();
        let (task_id, opened) = self.card_or_open(&session, &c, &heading(&text).unwrap_or_default()).await?;
        if !text.is_empty() {
            self.relay.write("task_detail", json!({"task_id": task_id, "plan": text})).await.map_err(err)?;
            self.autotitle(&task_id, &heading(&text).unwrap_or_default(), true).await?;
        }
        let out = self.report(&task_id, Some(&session), "working", None).await?;
        Ok(Front::opened(merge(out, json!({"blocked": false})), &task_id, opened))
    }

    pub async fn hook_todos(&self, a: &Value, st: &Stamp) -> Answer {
        let Some(items) = a.get("items").and_then(Value::as_array).cloned() else { return Err("items must be a list".into()) };
        let (session, _) = self.session(a, st, true).await?;
        let Some(task_id) = self.task_of(session.as_deref()).await? else { return Ok(json!({"ok": true, "tracked": false})) };
        self.relay.write("task_detail", json!({"task_id": task_id, "todos": items})).await.map_err(err)?;
        Ok(json!({"ok": true, "tracked": true, "task_id": task_id}))
    }

    /// A /clear gave the job a new session id. The old one leaves its card, so the job's
    /// next plan opens one of its own.
    pub async fn hook_session_clear(&self, a: &Value, st: &Stamp) -> Answer {
        let c = self.identify(st).await;
        let (Some(_), Some(sid)) = (&c.job, &c.session_id) else { return Ok(json!({"ok": true, "ended": null})) };
        let new = norm_arg(&str_of(a, "new_session_id").unwrap_or_else(|| sid.clone()))?;
        let key = self.job_key(&c);
        let seen = self.relay.mark(&key);
        let old = [seen, Some(sid.clone())].into_iter().flatten().find(|s| !s.is_empty() && *s != new);
        self.relay.set_mark(&key, &new);
        let Some(task_id) = self.task_of(old.as_deref()).await? else { return Ok(json!({"ok": true, "ended": null})) };
        let old = old.unwrap_or_default();
        self.relay.write("task_session_end", json!({"task_id": task_id, "session_id": old})).await.map_err(err)?;
        self.relay.clear_mark(&format!("needs:{old}"));
        Ok(json!({"ok": true, "ended": old, "task_id": task_id}))
    }

    pub async fn claim_task(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, c) = self.session(a, st, true).await?;
        let raw = str_of(a, "task_id").or_else(|| str_of(a, "ref")).unwrap_or_default();
        let words: Vec<&str> = raw.split_whitespace().collect();
        let prefixed = words.len() > 2 && words[0].eq_ignore_ascii_case("claim") && words[1].eq_ignore_ascii_case("task");
        let Some(id) = words.get(if prefixed { 2 } else { 0 }).map(|s| s.to_string()) else { return Err("task_id is required".into()) };
        let w = merge(json!({"task_id": id, "session_id": session, "name": c.name}), Front::whereabouts(&c));
        let out = self.relay.write("task_claim", w).await.map_err(err)?;
        self.relay.clear_mark(&format!("needs:{}", session.unwrap_or_default()));
        Ok(merge(json!({"ok": true}), out))
    }

    pub async fn track_task(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, c) = self.session(a, st, true).await?;
        let (task_id, opened) = self.card_or_open(&session.unwrap_or_default(), &c, &str_of(a, "title").unwrap_or_default()).await?;
        Ok(Front::opened(json!({"ok": true}), &task_id, opened))
    }

    pub async fn swarm_message_post(&self, a: &Value, st: &Stamp) -> Answer {
        let (session, c) = self.session(a, st, false).await?;
        let author = c.name.clone().or_else(|| session.clone()).unwrap_or_else(|| "a service".into());
        let mut w = json!({"task_id": a.get("card_id"), "body": str_of(a, "body").unwrap_or_default(), "author": author, "session_id": session});
        if let Some(n) = &c.node {
            w["node"] = json!(n);
        }
        Ok(merge(json!({"ok": true}), self.relay.write("task_message", w).await.map_err(err)?))
    }

    pub async fn swarm_messages(&self, a: &Value) -> Answer {
        let after = a.get("after").and_then(Value::as_i64).unwrap_or(0);
        let out = self.relay.read("task_messages", json!({"task_id": a.get("card_id"), "after": after})).await.map_err(err)?;
        Ok(json!({"ok": true, "card_id": a.get("card_id"), "messages": unwrap_stale(out)}))
    }

    // -- approvals (fleet.md R97) -----------------------------------------------------------

    pub async fn approval_request(&self, a: &Value, st: &Stamp) -> Answer {
        let c = self.caller(st).await;
        let out = self.relay.write("approval_request", json!({
            "kind": a.get("kind"), "node": a.get("node"), "reason": a.get("reason").cloned().unwrap_or(json!("")),
            "minutes": a.get("minutes").cloned().unwrap_or(json!(15)), "payload": a.get("payload"), "session_id": c.session_id,
        })).await.map_err(err)?;
        let modal = out.get("request_id").and_then(Value::as_str).map(|r| format!("{}/modal/approve/{r}", self.approver_origin.trim_end_matches('/')));
        Ok(merge(merge(json!({"ok": true}), out), json!({"modal_url": modal})))
    }

    pub async fn approval_get(&self, a: &Value) -> Answer {
        let out = unwrap_stale(self.relay.read("approval_get", json!({"request_id": a.get("request_id")})).await.map_err(err)?);
        Ok(merge(json!({"ok": true}), if out.is_object() { out } else { json!({}) }))
    }

    pub async fn approval_wait(&self, a: &Value) -> Answer {
        let wait = a.get("wait_s").cloned().unwrap_or(json!(55));
        match self.relay.poll("approval_wait", json!({"request_id": a.get("request_id"), "wait_s": wait})).await {
            Ok(out) => Ok(merge(json!({"ok": true}), out)),
            Err(ClientError::Unreachable(_)) => {
                let got = self.approval_get(a).await?;
                Ok(merge(got, json!({"alcor_down_since": self.relay.down_since(), "outbox": self.relay.pending()})))
            }
            Err(e) => Err(err(e)),
        }
    }

    pub async fn approval_cancel(&self, a: &Value, st: &Stamp) -> Answer {
        let c = self.own(a, st).await?;
        let out = self.relay.write("approval_cancel", json!({"request_id": a.get("request_id"), "session_id": c.session_id})).await.map_err(err)?;
        Ok(merge(json!({"ok": true}), out))
    }

    // -- orders (R97: alcor never acts on another node) -------------------------------------

    pub async fn order_pull(&self, a: &Value) -> Answer {
        let node = str_of(a, "node").unwrap_or_else(|| self.node.clone());
        self.relay.poll("order_pull", json!({"node": node, "kinds": a.get("kinds"), "wait_s": a.get("wait_s").cloned().unwrap_or(json!(0))}))
            .await.map_err(err)
    }

    pub async fn order_done(&self, a: &Value) -> Answer {
        let ok = a.get("ok").is_some_and(crate::service::truthy);
        let out = self.relay.write("order_done", json!({"order_id": a.get("order_id"), "ok": ok,
                                                         "note": a.get("note").cloned().unwrap_or(json!(""))})).await.map_err(err)?;
        Ok(merge(json!({"ok": true}), out))
    }

    pub async fn keys(&self) -> Answer {
        Ok(unwrap_stale(self.relay.read("keys", json!({})).await.map_err(err)?))
    }

    pub fn status(&self) -> Answer {
        Ok(merge(json!({"ok": true}), self.relay.status()))
    }

    /// One verb by name. A refusal answers `{ok: false, error}`, as Python's `Front.call`.
    pub async fn call(&self, verb: &str, args: &Value, st: &Stamp) -> Value {
        let out = match verb {
            "task_open" => self.task_open(args, st).await,
            "task_attach" => self.task_attach(args, st).await,
            "task_join" => self.task_join(args, st).await,
            "task_report" => self.task_report(args, st).await,
            "task_session_end" => self.task_session_end(args, st).await,
            "task_for_session" => self.task_for_session(args, st).await,
            "task_sessions" => self.task_sessions(args).await,
            "task_get" => self.task_get(args).await,
            "task_list" => self.task_list(args).await,
            "request_human" => self.request_human(args, st).await,
            "hook_prompt" => self.hook_prompt(args, st).await,
            "hook_plan" => self.hook_plan(args, st).await,
            "hook_plan_approved" => self.hook_plan_approved(args, st).await,
            "hook_todos" => self.hook_todos(args, st).await,
            "hook_session_clear" => self.hook_session_clear(args, st).await,
            "claim_task" => self.claim_task(args, st).await,
            "track_task" => self.track_task(args, st).await,
            "swarm_message_post" => self.swarm_message_post(args, st).await,
            "swarm_messages" => self.swarm_messages(args).await,
            "approval_request" => self.approval_request(args, st).await,
            "approval_get" => self.approval_get(args).await,
            "approval_wait" => self.approval_wait(args).await,
            "approval_cancel" => self.approval_cancel(args, st).await,
            "order_pull" => self.order_pull(args).await,
            "order_done" => self.order_done(args).await,
            "keys" => self.keys().await,
            "status" => self.status(),
            _ => Err(format!("unknown verb {verb:?}")),
        };
        out.unwrap_or_else(|e| json!({"ok": false, "error": e}))
    }
}
