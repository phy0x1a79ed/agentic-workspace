//! The machine kind of channel: one tether session, driven through this host's operator daemon.
//!
//! No other tool reaches the daemon, so the channel store is the one record of who holds which
//! session, and a session answers only to the channel that minted it. A slot is not an identity:
//! every verb names the session id the stream gave the mint, and refuses when the slot now
//! carries a later session.
//!
//! What the daemon reports splits in two (channels.md R110 item 5). What the two people say is
//! conversation, which the channel follows and pushes. What ran and what it printed is a tool
//! log that only `drain` reads.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use base64::Engine;
use serde_json::{json, Map, Value};

use crate::tether::adopted::{Adopted, BoxFut};
use crate::tether::control::Unavailable;
use crate::tether::paths::Paths;
use crate::tether::remote_os;
use crate::tether::stream::{Filter, Stream};

/// The event kinds that are conversation and session state. The rest is the tool log.
pub const TALK: &[&str] = &["owner.", "operator.", "session.phase"];
pub const LOG: &[&str] = &["task."];

/// How long `invite` waits for its own mint to come round on the stream.
const MINT_WAIT: Duration = Duration::from_secs(5);
/// The most a `drain` waits for something to arrive.
pub const MAX_WAIT_S: f64 = 90.0;

/// The daemon's own words for a session that is not there any more.
const GONE: [&str; 4] = ["names no session", "there is no session", "has ended", "no session is open"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MachineError {
    /// The tether no longer has the session.
    Gone(String),
    /// The tether refused, or could not be reached.
    Refused(String),
}

impl std::fmt::Display for MachineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MachineError::Gone(m) | MachineError::Refused(m) => f.write_str(m),
        }
    }
}

pub type ControlFn = Arc<dyn Fn(String, Value, Duration) -> BoxFut<Result<Value, Unavailable>> + Send + Sync>;

pub struct Machines {
    pub stream: Arc<Stream>,
    pub child: Option<Arc<Adopted>>,
    pub paths: Paths,
    control: ControlFn,
}

impl Machines {
    pub fn new(stream: Arc<Stream>, child: Option<Arc<Adopted>>, paths: Paths, control: ControlFn) -> Self {
        Self { stream, child, paths, control }
    }

    /// Mint a session for one declared kind of computer. Refused before the daemon when the OS
    /// is missing or unknown: a session minted on a guess is what this exists to prevent.
    pub async fn invite(&self, os: Option<&str>) -> Result<Value, MachineError> {
        if let Some(why) = remote_os::refusal(os) {
            return Err(MachineError::Refused(why));
        }
        let os = os.unwrap_or_default();
        let floor = self.stream.journal.next();
        let answer = self.ask("invite", json!({}), Duration::from_secs(60)).await?;
        let Some(slot) = answer.get("slot").and_then(Value::as_i64) else {
            return Err(MachineError::Refused("the tether minted no usable slot".into()));
        };
        let word = answer.get("code").and_then(Value::as_str).unwrap_or("").split(' ').next().unwrap_or("").to_string();
        let claimed = {
            let mut book = self.stream.book();
            book.0.declared.insert(slot, json!({"os": os, "word": word}));
            book.0.claim(slot, floor)
        };
        let sid = match claimed {
            Some(s) => Some(s),
            None => self.await_mint(slot).await,
        };
        let line = remote_os::launcher_line(os, answer.get("command").and_then(Value::as_str).unwrap_or("")).map_err(MachineError::Refused)?;
        let open = remote_os::choice(os).map_or("", |c| c.open);
        let mut out = answer.as_object().cloned().unwrap_or_default();
        out.insert("session".into(), json!(sid));
        out.insert("command".into(), json!(line));
        out.insert("read_out".into(), json!(format!("{open}, then answer the prompt it shows you: {line}")));
        out.insert("os".into(), remote_os::describe(Some(os), None));
        Ok(Value::Object(out))
    }

    async fn await_mint(&self, slot: i64) -> Option<String> {
        if !self.stream.journal.is_connected() {
            return None;
        }
        let deadline = tokio::time::Instant::now() + MINT_WAIT;
        while self.stream.book().0.claimed(slot) {
            let left = deadline.saturating_duration_since(tokio::time::Instant::now());
            if left.is_zero() {
                return None;
            }
            self.stream.journal.wait(left, None, None).await;
        }
        self.session_of(slot)
    }

    pub fn session_of(&self, slot: i64) -> Option<String> {
        self.stream.book().0.current(slot)
    }

    pub async fn cut(&self, slot: i64, sid: &str, reason: &str) -> Result<(), MachineError> {
        match self.aimed("cut", slot, sid, json!({"reason": reason})).await {
            Ok(_) | Err(MachineError::Gone(_)) => Ok(()),
            Err(e) => Err(e),
        }
    }

    pub async fn say(&self, slot: i64, sid: &str, text: &str) -> Result<Value, MachineError> {
        self.aimed("send", slot, sid, json!({"text": text})).await
    }

    /// Put a local file on the owner's machine. Answers once the owner says where it landed.
    pub async fn put(&self, slot: i64, sid: &str, path: &str, name: &str) -> Result<Value, MachineError> {
        self.aimed_for("file", slot, sid, json!({"path": path, "name": name}), Duration::from_secs(3600)).await
    }

    pub async fn run(&self, slot: i64, sid: &str, command: &str, limit_s: Option<i64>) -> Result<Value, MachineError> {
        let mut fields = json!({"command": command});
        if let Some(l) = limit_s {
            fields["limit_s"] = json!(l);
        }
        self.aimed("run", slot, sid, fields).await
    }

    pub async fn terminal(&self, slot: i64, sid: &str, command: Option<&str>, cols: Option<i64>, rows: Option<i64>) -> Result<Value, MachineError> {
        let mut fields = Map::new();
        if let Some(c) = command.filter(|c| !c.is_empty()) {
            fields.insert("command".into(), json!(c));
        }
        if let Some(c) = cols {
            fields.insert("cols".into(), json!(c));
        }
        if let Some(r) = rows {
            fields.insert("rows".into(), json!(r));
        }
        self.aimed("shell", slot, sid, Value::Object(fields)).await
    }

    pub async fn keys(&self, slot: i64, sid: &str, task: i64, raw: &[u8]) -> Result<Value, MachineError> {
        let data = base64::engine::general_purpose::STANDARD.encode(raw);
        self.aimed("keys", slot, sid, json!({"task": task, "data": data})).await
    }

    pub async fn stop_task(&self, slot: i64, sid: &str, task: i64) -> Result<Value, MachineError> {
        self.aimed("close", slot, sid, json!({"task": task})).await
    }

    pub async fn tasks(&self, slot: i64, sid: &str) -> Result<Value, MachineError> {
        self.aimed("tasks", slot, sid, json!({})).await
    }

    /// The newest task still open in the session, for keys with no task.
    pub async fn last_task(&self, slot: i64, sid: &str) -> Result<Option<i64>, MachineError> {
        let answer = self.tasks(slot, sid).await?;
        Ok(answer.get("tasks").and_then(Value::as_array).into_iter().flatten().filter_map(|t| t.get("task").and_then(Value::as_i64)).max())
    }

    /// What the session's machine was declared as, beside what its client reports.
    pub async fn describe_os(&self, slot: i64, brief: bool) -> Value {
        let report = self.ask("status", json!({}), Duration::from_secs(30)).await.unwrap_or(json!({}));
        let reported = report
            .get("sessions")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter(|s| s.get("slot").and_then(Value::as_i64) == Some(slot))
            .filter_map(|s| s.pointer("/owner/os").and_then(Value::as_str).map(str::to_string))
            .next_back();
        let declared = self.stream.book().0.declared.get(&slot).and_then(|d| d.get("os")).and_then(Value::as_str).map(str::to_string);
        let mut about = remote_os::describe(declared.as_deref(), reported.as_deref());
        if brief {
            if let Some(m) = about.as_object_mut() {
                m.retain(|k, _| ["declared", "reported", "shell", "mismatch", "warning"].contains(&k.as_str()));
            }
        }
        about
    }

    pub fn epoch(&self) -> Option<i64> {
        self.stream.journal.epoch()
    }

    /// The session's events of `types` from `cursor`, held up to `wait` seconds. The cursor
    /// belongs to one daemon epoch: the caller starts again from 0 when the answer's differs.
    pub async fn events(&self, sid: &str, cursor: i64, types: &[&str], wait: f64, until: Option<i64>, task: Option<i64>) -> Value {
        let sessions: HashSet<String> = [sid.to_string()].into();
        let filter = Filter { limit: Some(500), task, types, sessions: Some(&sessions), ..Filter::default() };
        let hold = wait.clamp(0.0, MAX_WAIT_S);
        if hold > 0.0 && self.stream.journal.since(Some(cursor), &filter).0.is_empty() {
            self.stream.journal.wait(Duration::from_secs_f64(hold), until, Some(&sessions)).await;
        }
        let (events, next, gap) = self.stream.journal.since(Some(cursor), &filter);
        json!({"epoch": self.epoch(), "cursor": next, "more": next < self.stream.journal.next(), "gap": gap, "events": events})
    }

    /// This host's tether binary: role, release, child and stream.
    pub async fn report(&self) -> Value {
        let child = match &self.child {
            Some(c) => c.snapshot().await,
            None => Value::Null,
        };
        json!({"role": self.paths.role.as_str(), "child": child, "release": self.paths.release_report(), "stream": self.stream.journal.head(), "log": self.paths.daemon_log()})
    }

    async fn aimed(&self, verb: &str, slot: i64, sid: &str, fields: Value) -> Result<Value, MachineError> {
        self.aimed_for(verb, slot, sid, fields, Duration::from_secs(60)).await
    }

    async fn aimed_for(&self, verb: &str, slot: i64, sid: &str, fields: Value, timeout: Duration) -> Result<Value, MachineError> {
        if let Some(current) = self.session_of(slot) {
            if current != sid {
                return Err(MachineError::Gone(format!("slot {slot} now carries a later session")));
            }
        }
        let mut args = json!({"code": slot.to_string()});
        if let (Some(a), Some(f)) = (args.as_object_mut(), fields.as_object()) {
            a.extend(f.clone());
        }
        self.ask(verb, args, timeout).await
    }

    async fn ask(&self, verb: &str, args: Value, timeout: Duration) -> Result<Value, MachineError> {
        let answer = (self.control)(verb.to_string(), args, timeout).await.map_err(|e| {
            MachineError::Refused(format!("no tether daemon is answering on this host: {}. Its log is {}.", e.0, self.paths.daemon_log().display()))
        })?;
        if !answer.is_object() {
            return Err(MachineError::Refused(format!("the tether daemon answered {verb} with {answer}")));
        }
        if answer.get("ok") == Some(&json!(false)) || answer.get("error").is_some() {
            let error = match answer.get("error") {
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                Some(v) if !v.is_null() && v != &json!("") => v.to_string(),
                _ => "no reason given".into(),
            };
            return Err(if GONE.iter().any(|m| error.contains(m)) { MachineError::Gone(error) } else { MachineError::Refused(error) });
        }
        Ok(answer)
    }
}

/// The operator daemon's control socket as a [`ControlFn`].
pub fn socket_control(paths: &Paths) -> ControlFn {
    let socket = paths.control_socket();
    Arc::new(move |verb, args, timeout| {
        let socket = socket.clone();
        Box::pin(async move { crate::tether::control::call(&socket, &verb, args, timeout).await })
    })
}

/// Join a task's output chunks and decode them to text. Output travels as bytes, and a chunk
/// boundary can split a character, so consecutive chunks of one task and stream are joined
/// before the decode. Each chunk carries its own base64 padding, so each is decoded alone.
pub fn decoded(events: &[Value], raw: bool) -> Vec<Value> {
    let b64 = base64::engine::general_purpose::STANDARD;
    let mut out: Vec<(Value, Option<Vec<String>>)> = vec![];
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("task.output") {
            out.push((event.clone(), None));
            continue;
        }
        let chunk = event.get("data").and_then(Value::as_str).unwrap_or("").to_string();
        if let Some((last, Some(chunks))) = out.last_mut() {
            if last.get("task") == event.get("task") && last.get("stream") == event.get("stream") {
                chunks.push(chunk);
                let bytes = last.get("bytes").and_then(Value::as_i64).unwrap_or(0) + event.get("bytes").and_then(Value::as_i64).unwrap_or(0);
                last["bytes"] = json!(bytes);
                continue;
            }
        }
        out.push((event.clone(), Some(vec![chunk])));
    }
    out.into_iter()
        .map(|(mut event, chunks)| {
            let Some(chunks) = chunks else { return event };
            let blob: Vec<u8> = chunks.iter().filter_map(|c| b64.decode(c).ok()).flatten().collect();
            if raw {
                event["data"] = json!(b64.encode(&blob));
            } else if let Some(m) = event.as_object_mut() {
                m.remove("data");
                m.insert("text".into(), json!(String::from_utf8_lossy(&blob)));
            }
            event
        })
        .collect()
}
