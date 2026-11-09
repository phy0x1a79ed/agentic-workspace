//! The harness@1 providers (harness-contract.md): the live contract table, each provider's
//! `platform` answer, the down marks, one call, the parallel fan-out, and the lookups that find
//! the provider of a session. reflection names no provider itself (install.md R117 item 4).

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use awm_plugin_sdk::ids;
use futures_util::future::join_all;
use serde_json::{json, Map, Value};

use awm_pycompat::repr::str_repr;
use crate::app::{is_false, text, truthy, App};
use crate::caller::Frame;
use crate::gateway::{Call, CallError, Gateway};

pub const CONTRACT: &str = "harness@1";
/// A provider that fails a call is skipped for this long, so a node without dsh does not pay a
/// failed call on every reflection verb.
pub const DOWN_FOR: Duration = Duration::from_secs(60);
/// The provider list is read again after this.
pub const TABLE_FOR: Duration = Duration::from_secs(30);

/// The platform a session id names: its first two characters.
pub fn platform_of(id: &str) -> &str {
    id.get(..2).unwrap_or_default()
}

/// One harness plugin: its tool, the platform code it serves, the node it runs on when not
/// this one, and the optional verbs its `platform` answer lists.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Provider {
    pub tool: String,
    pub code: Option<String>,
    pub peer: Option<String>,
    pub capabilities: Vec<String>,
}

impl Provider {
    pub fn new(tool: &str, code: Option<&str>, peer: Option<&str>) -> Self {
        Self { tool: tool.into(), code: code.map(str::to_string), peer: peer.map(str::to_string), capabilities: vec![] }
    }

    pub fn serves(&self, verb: &str) -> bool {
        self.capabilities.iter().any(|c| c == verb)
    }
}

/// No harness plugin could do what was asked.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum HarnessError {
    #[error("{0}")]
    Failed(String),
    /// A pid or a ref that more than one provider claims.
    #[error("{0}")]
    Ambiguous(String),
    /// A job id or a name no provider on this node knows.
    #[error("no session on this node has the job id or name {}; `list_other` shows them", str_repr(.0))]
    Unknown(String),
}

/// The cached contract table, platform answers and down marks.
#[derive(Default)]
pub struct Table {
    rows: Mutex<Option<(Instant, Vec<Value>)>>,
    platforms: Mutex<HashMap<(String, Option<String>), Value>>,
    down: Mutex<HashMap<String, Instant>>,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

/// The client timeout of one provider verb.
pub fn timeout_of(verb: &str) -> Duration {
    Duration::from_secs(match verb {
        "start" => 1800,
        "revive_pinned" => 600,
        "resume" => 90,
        "set" => 120,
        "probe" => 25,
        _ => 60,
    })
}

/// The caller among the `resolve` answers to `pid`: the one provider that says `mine`.
pub fn pick_caller(pid: Option<u32>, answers: &[(Provider, Value)]) -> Result<(Provider, Value), HarnessError> {
    let Some(pid) = pid else {
        return Err(HarnessError::Failed(
            "could not tell which session is calling, so there is no session of your own to act on. Call through a \
             session's own `awm mcp` proxy, or name a session with `job`"
                .into(),
        ));
    };
    let mine: Vec<&(Provider, Value)> = answers.iter().filter(|(_, r)| truthy(r.get("mine"))).collect();
    match mine.as_slice() {
        [] => Err(HarnessError::Failed(format!("no harness plugin on this node knows pid {pid} as a session"))),
        [(p, who)] => {
            if text(who, "session_id").is_none() {
                let why = text(who, "error")
                    .map(str::to_string)
                    .unwrap_or_else(|| format!("{} owns pid {pid} but cannot tell which of its sessions is calling", p.tool));
                return Err(HarnessError::Failed(why));
            }
            Ok((p.clone(), who.clone()))
        }
        many => {
            let tools: Vec<&str> = many.iter().map(|(p, _)| p.tool.as_str()).collect();
            Err(HarnessError::Ambiguous(format!("pid {pid} is claimed by {}", tools.join(", "))))
        }
    }
}

/// A contract row's `major`, where a missing or zero one reads as 1.
fn major_of(row: &Value) -> i64 {
    match awm_harness::contract::args::int_lenient(row.get("major"), 1) {
        0 => 1,
        m => m,
    }
}

/// `args` without its null values, as a provider is sent them.
fn without_nulls(args: Value) -> Value {
    match args {
        Value::Object(m) => Value::Object(m.into_iter().filter(|(_, v)| !v.is_null()).collect()),
        other => other,
    }
}

impl<G: Gateway> App<G> {
    /// The live providers of the contract, read at most every [`TABLE_FOR`]. A failed read keeps
    /// the last rows.
    async fn contract_rows(&self) -> Vec<Value> {
        let cached = lock(&self.table.rows).clone();
        if let Some((at, rows)) = &cached {
            if !rows.is_empty() && at.elapsed() < TABLE_FOR {
                return rows.clone();
            }
        }
        let stale = cached.map(|(_, r)| r).unwrap_or_default();
        let got = match self.gw.contract_rows().await {
            Ok(got) => got,
            Err(e) => {
                tracing::warn!("reflection: cannot list the providers of {CONTRACT}: {e}");
                return stale;
            }
        };
        let rows: Vec<Value> = got
            .into_iter()
            .filter(|r| text(r, "name").is_some() && major_of(r) == 1)
            .collect();
        *lock(&self.table.rows) = Some((Instant::now(), rows.clone()));
        rows
    }

    /// The `platform` answer of `tool`, kept once it names a known platform code.
    async fn platform_of(&self, tool: &str, peer: Option<&str>) -> Option<Value> {
        let key = (tool.to_string(), peer.map(str::to_string));
        if let Some(v) = lock(&self.table.platforms).get(&key) {
            return Some(v.clone());
        }
        let out = match self.harness_call(&Provider::new(tool, None, peer), "platform", json!({}), None).await {
            Ok(out) => out,
            Err(e) => {
                tracing::info!("reflection: {e}");
                return None;
            }
        };
        let code = out.get("code").and_then(Value::as_str)?;
        if !ids::PLATFORMS.contains(&code) {
            return None;
        }
        lock(&self.table.platforms).insert(key, out.clone());
        Some(out)
    }

    fn is_down(&self, tool: &str) -> bool {
        lock(&self.table.down).get(tool).is_some_and(|until| *until > Instant::now())
    }

    /// Every provider reflection may call, and the tools left out because a call failed lately.
    pub async fn providers_and_down(&self) -> (Vec<Provider>, Vec<String>) {
        let tool = |row: &Value| text(row, "name").unwrap_or_default().to_string();
        let (up, down): (Vec<Value>, Vec<Value>) = self.contract_rows().await.into_iter().partition(|r| !self.is_down(&tool(r)));
        let platforms = join_all(up.iter().map(|r| async move { self.platform_of(&tool(r), text(r, "peer")).await })).await;
        let live = up
            .iter()
            .zip(platforms)
            .filter_map(|(row, platform)| {
                let platform = platform?;
                let mut p = Provider::new(&tool(row), platform.get("code").and_then(Value::as_str), text(row, "peer"));
                p.capabilities = platform
                    .get("capabilities")
                    .and_then(Value::as_array)
                    .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
                    .unwrap_or_default();
                Some(p)
            })
            .collect();
        (live, down.iter().map(tool).collect())
    }

    /// Every provider reflection may call, the ones marked down left out.
    pub async fn providers(&self) -> Vec<Provider> {
        self.providers_and_down().await.0
    }

    /// The provider of platform `code`.
    pub async fn for_platform(&self, code: &str) -> Result<Provider, HarnessError> {
        self.providers()
            .await
            .into_iter()
            .find(|p| p.code.as_deref() == Some(code))
            .ok_or_else(|| HarnessError::Failed(format!("no harness plugin for platform {} runs on this node", str_repr(code))))
    }

    /// `verb` on provider `p`. A transport failure marks the provider down. A result that is not
    /// an object reads as `{ok: true, result}`.
    pub async fn harness_call(&self, p: &Provider, verb: &str, args: Value, as_: Option<&str>) -> Result<Value, HarnessError> {
        let call = Call::new(&p.tool, verb, without_nulls(args), timeout_of(verb)).peer(p.peer.as_deref()).as_(as_);
        match self.gw.call(call).await {
            Ok(out @ Value::Object(_)) => Ok(out),
            Ok(other) => Ok(json!({"ok": true, "result": other})),
            Err(CallError::Refused(why)) => Err(HarnessError::Failed(why)),
            Err(CallError::Down(why)) => {
                lock(&self.table.down).insert(p.tool.clone(), Instant::now() + DOWN_FOR);
                Err(HarnessError::Failed(format!("{} did not answer {verb}: {why}", p.tool)))
            }
        }
    }

    /// `verb` on every provider at once, with each one's outcome.
    pub async fn everyone(&self, verb: &str, args: &Value, as_: Option<&str>) -> Vec<(Provider, Result<Value, HarnessError>)> {
        let providers = self.providers().await;
        let asks = providers.iter().map(|p| self.harness_call(p, verb, args.clone(), as_));
        providers.iter().cloned().zip(join_all(asks).await).collect()
    }

    /// The answers of every provider that answered `verb`. A failure is logged and skipped.
    pub async fn answers(&self, verb: &str, args: &Value) -> Vec<(Provider, Value)> {
        let mut out = vec![];
        for (p, got) in self.everyone(verb, args, None).await {
            match got {
                Ok(v) => out.push((p, v)),
                Err(e) => tracing::info!("reflection: {e}"),
            }
        }
        out
    }

    /// The provider of an R9 `session_id`, and what it says about that session.
    pub async fn for_session(&self, session_id: &str) -> Result<(Provider, Value), HarnessError> {
        let p = self.for_platform(platform_of(session_id)).await?;
        let who = self.harness_call(&p, "status", json!({"session_id": session_id}), None).await?;
        if is_false(&who, "ok") {
            let why = text(&who, "reason")
                .or_else(|| text(&who, "error"))
                .map(str::to_string)
                .unwrap_or_else(|| format!("{} does not know session {session_id}", p.tool));
            return Err(HarnessError::Failed(why));
        }
        let mut who = crate::app::object(&who);
        who.insert("session_id".into(), json!(session_id));
        Ok((p, Value::Object(who)))
    }

    /// The provider that owns the caller, and what it says about the caller. An isolated session
    /// has no host pid: the gateway names it by its R9 id (plugin-contract.md § Isolated sessions).
    pub async fn for_caller(&self, pid: Option<u32>, session_id: Option<&str>) -> Result<(Provider, Value), HarnessError> {
        if let Some(sid) = session_id {
            return self.for_session(sid).await;
        }
        let Some(pid) = pid else { return pick_caller(None, &[]) };
        pick_caller(Some(pid), &self.answers("resolve", &json!({"pid": pid})).await)
    }

    /// The provider whose session `reference` names, and the argument that names it there.
    pub async fn for_ref(&self, reference: &str) -> Result<(Provider, Map<String, Value>), HarnessError> {
        let text_ref = reference.trim();
        let lower = text_ref.to_lowercase();
        let mut named = Map::new();
        if ids::is_r9(&lower) && text_ref.chars().count() > 8 {
            named.insert("session_id".into(), json!(lower));
            return Ok((self.for_platform(platform_of(&lower)).await?, named));
        }
        let hits: Vec<Provider> = self
            .answers("find", &json!({"ref": text_ref}))
            .await
            .into_iter()
            .filter(|(_, r)| r.get("matches").is_some_and(|m| truthy(Some(m))))
            .map(|(p, _)| p)
            .collect();
        match hits.as_slice() {
            [] => Err(HarnessError::Unknown(text_ref.to_string())),
            [p] => {
                named.insert("ref".into(), json!(text_ref));
                Ok((p.clone(), named))
            }
            many => {
                let tools: Vec<&str> = many.iter().map(|p| p.tool.as_str()).collect();
                Err(HarnessError::Ambiguous(format!("{} names sessions under {}; pass the session id", str_repr(text_ref), tools.join(", "))))
            }
        }
    }

    /// The provider and target args of a verb with an optional `job`: the session `job` names,
    /// with the caller's pid, else the caller's own session through [`App::resolve_caller`].
    pub async fn named_target(&self, job: Option<&str>, frame: &Frame) -> Result<(Provider, Map<String, Value>), HarnessError> {
        if let Some(job) = job.map(str::trim).filter(|j| !j.is_empty()) {
            let (p, mut named) = self.for_ref(job).await?;
            named.insert("pid".into(), frame.pid_value());
            return Ok((p, named));
        }
        let (p, _who) = self.resolve_caller(frame).await?;
        Ok((p, crate::app::object(&frame.me())))
    }
}
