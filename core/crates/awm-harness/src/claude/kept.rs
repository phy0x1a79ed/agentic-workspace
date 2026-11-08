//! Kept sessions: what to do when a session that must stay up goes down.
//!
//! A keeper is a loop that holds one Claude Code session per job it keeps. This is the part every
//! keeper shares. It is pure, with no I/O and no clock of its own: times come in as epoch seconds,
//! state goes out as JSON values, and a test drives it with a fake clock.
//!
//! - [`Outage`] is the restart record: when the session went down and why, when it came back, what
//!   arrived meanwhile and what the keeper did. It renders into the one prompt a resumed or fresh
//!   session gets.
//! - [`decide`] says what to do this tick: nothing, wait, back off, resume, start fresh, or give up.
//! - [`Breaker`] bounds the attempts. It counts attempts that did not reach healthy, not attempts
//!   per hour, so a slow loop still trips it. Waiting for a daemon or a node spends nothing.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Attempts that did not reach healthy, within [`DECAY_S`], before the breaker trips.
pub const BUDGET: usize = 4;
pub const DECAY_S: f64 = 3600.0;
/// The wait after the 1st, 2nd, 3rd and later attempt.
pub const BACKOFF_S: [f64; 4] = [60.0, 120.0, 300.0, 600.0];
/// A tripped breaker clears itself after this long, or when an operator clears it.
pub const HOLD_S: f64 = 6.0 * 3600.0;
/// A session alive this long after an attempt counts as healthy again.
pub const SETTLE_S: f64 = 600.0;
/// Resumes that did not reach healthy before the keeper starts fresh instead.
pub const MAX_RESUMES: usize = 3;

/// The persistent breaker on one kept session's restart attempts.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct Breaker {
    /// (epoch seconds, kind) of each attempt, kind being `resume` or `fresh`.
    pub attempts: Vec<(f64, String)>,
    pub tripped_at: Option<f64>,
    pub reason: String,
    pub next_try: f64,
}

fn bad(what: impl std::fmt::Display) -> serde_json::Error {
    <serde_json::Error as serde::de::Error>::custom(what)
}

fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}

fn py_float(v: &Value) -> Result<f64, serde_json::Error> {
    match v {
        Value::Number(n) => n.as_f64().ok_or_else(|| bad("not a number")),
        Value::Bool(b) => Ok(f64::from(u8::from(*b))),
        Value::String(s) => s.trim().parse().map_err(|_| bad(format!("could not convert string to float: {s:?}"))),
        other => Err(bad(format!("float() argument must be a string or a real number, not {other}"))),
    }
}

fn truthy(v: Option<&Value>) -> Option<&Value> {
    v.filter(|v| match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    })
}

impl Breaker {
    /// Whether the breaker holds at `now`. A trip older than [`HOLD_S`] clears itself here.
    pub fn tripped(&mut self, now: f64) -> bool {
        let Some(at) = self.tripped_at else {
            return false;
        };
        if now - at >= HOLD_S {
            self.clear();
            return false;
        }
        true
    }

    /// The attempts within [`DECAY_S`] of `now`, of `kind` when given.
    pub fn recent(&self, now: f64, kind: Option<&str>) -> usize {
        self.attempts.iter().filter(|(at, k)| now - at < DECAY_S && kind.is_none_or(|kind| k == kind)).count()
    }

    /// Count one resume or fresh start, and set the wait before the next.
    pub fn attempt(&mut self, now: f64, kind: &str, reason: &str) {
        self.attempts.retain(|(at, _)| now - at < DECAY_S);
        self.attempts.push((now, kind.to_string()));
        let n = self.attempts.len();
        self.next_try = now + BACKOFF_S[n.min(BACKOFF_S.len()) - 1];
        if n >= BUDGET {
            self.tripped_at = Some(now);
            self.reason = if reason.is_empty() {
                format!("{n} attempts in {:.0} h did not stay up", DECAY_S / 3600.0)
            } else {
                reason.to_string()
            };
        }
    }

    /// Forget the attempts once the session has stayed up [`SETTLE_S`]; true when this cleared
    /// something, so the keeper can say so.
    pub fn healthy(&mut self, now: f64, alive_since: Option<f64>) -> bool {
        let Some(since) = alive_since else {
            return false;
        };
        if self.attempts.is_empty() || now - since < SETTLE_S {
            return false;
        }
        let last = self.attempts.iter().map(|(at, _)| *at).fold(f64::NEG_INFINITY, f64::max);
        if since < last {
            return false;
        }
        self.clear();
        true
    }

    pub fn clear(&mut self) {
        self.attempts.clear();
        self.tripped_at = None;
        self.reason.clear();
        self.next_try = 0.0;
    }

    /// The breaker as the JSON a keeper persists.
    pub fn to_value(&self) -> Value {
        serde_json::to_value(self).expect("a breaker serializes")
    }

    /// A breaker from persisted JSON, read as leniently as a hand-edited state file needs:
    /// null, a missing or empty record is a fresh breaker, and numbers may be strings.
    pub fn from_value(data: Option<&Value>) -> Result<Breaker, serde_json::Error> {
        let empty = Map::new();
        let data = match data {
            None | Some(Value::Null) => &empty,
            Some(Value::Object(m)) => m,
            Some(other) => return Err(bad(format!("a breaker record is an object, not {other}"))),
        };
        let mut attempts = Vec::new();
        if let Some(list) = truthy(data.get("attempts")) {
            let Value::Array(list) = list else {
                return Err(bad("attempts is not a list"));
            };
            for entry in list {
                match entry.as_array().map(Vec::as_slice) {
                    Some([at, k]) => attempts.push((py_float(at)?, py_str(k))),
                    _ => return Err(bad(format!("an attempt is [at, kind], not {entry}"))),
                }
            }
        }
        let tripped_at = match data.get("tripped_at") {
            None | Some(Value::Null) => None,
            Some(v) => Some(v.as_f64().ok_or_else(|| bad(format!("tripped_at is not a number: {v}")))?),
        };
        Ok(Breaker {
            attempts,
            tripped_at,
            reason: truthy(data.get("reason")).map(py_str).unwrap_or_default(),
            next_try: truthy(data.get("next_try")).map(py_float).transpose()?.unwrap_or(0.0),
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    None,
    Wait,
    Backoff,
    Resume,
    Fresh,
    GiveUp,
}

impl Action {
    pub fn as_str(self) -> &'static str {
        match self {
            Action::None => "none",
            Action::Wait => "wait",
            Action::Backoff => "backoff",
            Action::Resume => "resume",
            Action::Fresh => "fresh",
            Action::GiveUp => "give_up",
        }
    }
}

/// What the keeper knows about the session this tick.
///
/// `state` is `alive`, `blocked` (a person must act), `failed` (an error only a new session
/// mends), `exited` or `missing`. `daemon_up` is None when the node did not say.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Situation {
    pub state: String,
    pub node_reachable: bool,
    pub daemon_up: Option<bool>,
    pub resumable: bool,
    pub spec_changed: bool,
}

impl Situation {
    /// A situation in `state` on a reachable node whose daemon runs, resumable, spec unchanged.
    pub fn new(state: impl Into<String>) -> Self {
        Self { state: state.into(), node_reachable: true, daemon_up: Some(true), resumable: true, spec_changed: false }
    }
}

/// The action for this tick, and a short reason for it.
///
/// A session on a node that cannot run it is waited for, never stopped or replaced: replacing it
/// there would discard a conversation that a resume keeps once the daemon is back.
pub fn decide(s: &Situation, breaker: &mut Breaker, now: f64) -> (Action, String) {
    if !s.node_reachable {
        return (Action::Wait, "its node does not answer".into());
    }
    if s.daemon_up == Some(false) {
        return (Action::Wait, "no Claude Code daemon runs on its node".into());
    }
    if s.state == "alive" || s.state == "blocked" {
        return (Action::None, String::new());
    }
    if breaker.tripped(now) {
        return (Action::GiveUp, breaker.reason.clone());
    }
    if now < breaker.next_try {
        return (Action::Backoff, format!("next try in {:.0}s", breaker.next_try - now));
    }
    if s.state == "missing" {
        return (Action::Fresh, "it has no session".into());
    }
    if s.state == "failed" {
        return (Action::Fresh, "it failed on an error a resume cannot mend".into());
    }
    if s.spec_changed {
        return (Action::Fresh, "its launch spec changed".into());
    }
    if !s.resumable {
        return (Action::Fresh, "it cannot be resumed".into());
    }
    if breaker.recent(now, Some("resume")) >= MAX_RESUMES {
        return (Action::Fresh, format!("{MAX_RESUMES} resumes did not stay up"));
    }
    (Action::Resume, "it exited".into())
}

/// What a daemon.log cause means, in the words a session reads.
pub fn cause_text(cause: &str) -> Option<&'static str> {
    Some(match cause {
        "killed" => {
            "the Claude Code daemon on its node was killed without warning (an OOM kill, a SIGKILL or a power loss)"
        }
        "signal" => "the Claude Code daemon on its node was stopped by a signal",
        "upgrade" => "the Claude Code daemon on its node restarted to upgrade",
        "idle_exit" => "the Claude Code daemon on its node exited while idle",
        "reboot" => "its node rebooted",
        _ => return None,
    })
}

/// The restart record of one outage of a kept session.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Outage {
    pub down_at: f64,
    pub cause: String,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub up_at: Option<f64>,
    #[serde(default)]
    pub missed: Option<i64>,
    /// `resumed`, `fresh` or `deaf`.
    #[serde(default)]
    pub action: Option<String>,
    #[serde(default)]
    pub alerted_at: Option<f64>,
    #[serde(default)]
    pub recovery_alerted: bool,
}

fn hhmm(epoch: f64) -> String {
    let secs = epoch.floor();
    let nanos = ((epoch - secs) * 1e9) as u32;
    DateTime::<Utc>::from_timestamp(secs as i64, nanos.min(999_999_999))
        .map(|t| t.format("%Y-%m-%d %H:%MZ").to_string())
        .unwrap_or_else(|| epoch.to_string())
}

fn span(seconds: f64) -> String {
    let minutes = (seconds / 60.0).round_ties_even().max(0.0) as i64;
    if minutes < 60 {
        format!("{minutes} min")
    } else {
        format!("{} h {} min", minutes / 60, minutes % 60)
    }
}

impl Outage {
    /// An outage that began at `down_at` for `cause`, with nothing else known yet.
    pub fn new(down_at: f64, cause: impl Into<String>) -> Self {
        Self {
            down_at,
            cause: cause.into(),
            detail: String::new(),
            up_at: None,
            missed: None,
            action: None,
            alerted_at: None,
            recovery_alerted: false,
        }
    }

    pub fn describe_cause(&self) -> String {
        let text = cause_text(&self.cause).unwrap_or(&self.cause);
        if self.detail.is_empty() {
            text.to_string()
        } else {
            format!("{text} ({})", self.detail)
        }
    }

    /// The one line a session gets when it comes back from this outage. `what_now` tells it how
    /// to catch up, for example which command reads the messages it missed.
    pub fn render(&self, node: &str, what_now: &str) -> String {
        let mut when = format!("from {}", hhmm(self.down_at));
        if let Some(up) = self.up_at {
            when += &format!(" to {} ({})", hhmm(up), span(up - self.down_at));
        }
        let how = match self.action.as_deref() {
            Some("resumed") => "It was resumed with its own conversation.",
            Some("fresh") => "It is a new session. The tail of the previous one follows.",
            Some("deaf") => "It kept running, but no line could be pushed into it.",
            _ => "",
        };
        let missed = match self.missed {
            Some(n) if n != 0 => format!("{n} message{} arrived meanwhile.", if n != 1 { "s" } else { "" }),
            _ => String::new(),
        };
        let head = format!("keeper: this session was down {when} on {node} because {}.", self.describe_cause());
        [head.as_str(), how, missed.as_str(), what_now, "Do not announce the restart in the chat."]
            .into_iter()
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>()
            .join(" ")
    }

    /// The outage as the JSON a keeper persists.
    pub fn to_value(&self) -> Value {
        serde_json::to_value(self).expect("an outage serializes")
    }

    /// An outage from persisted JSON: None for null, a missing or an empty record. Unknown keys
    /// are ignored.
    pub fn from_value(data: Option<&Value>) -> Result<Option<Outage>, serde_json::Error> {
        match data {
            None | Some(Value::Null) => Ok(None),
            Some(Value::Object(m)) if m.is_empty() => Ok(None),
            Some(v) => Outage::deserialize(v).map(Some),
        }
    }
}

/// The last `turns` user and assistant texts of a Claude Code transcript, at most `limit`
/// characters from the end; 0 for either means no bound.
///
/// `lines` are the transcript's JSONL lines. Tool calls and results are left out: a fresh session
/// needs what was said, not what was run.
pub fn transcript_tail<I, S>(lines: I, turns: usize, limit: usize) -> String
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut said = Vec::new();
    for raw in lines {
        let Ok(Value::Object(entry)) = serde_json::from_str::<Value>(raw.as_ref()) else {
            continue;
        };
        let role = match entry.get("type").and_then(Value::as_str) {
            Some(role @ ("user" | "assistant")) => role,
            _ => continue,
        };
        let content = entry.get("message").and_then(|m| m.get("content"));
        let text = match content {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Array(parts)) => parts
                .iter()
                .filter(|p| p.get("type").and_then(Value::as_str) == Some("text"))
                .map(|p| p.get("text").and_then(Value::as_str).unwrap_or(""))
                .collect::<Vec<_>>()
                .join("\n"),
            _ => String::new(),
        };
        let text = text.trim();
        if !text.is_empty() {
            said.push(format!("[{role}] {text}"));
        }
    }
    let from = if turns == 0 { 0 } else { said.len().saturating_sub(turns) };
    let out = said[from..].join("\n\n");
    let chars = out.chars().count();
    if limit == 0 || chars <= limit {
        return out;
    }
    out.chars().skip(chars - limit).collect()
}

/// The turns [`transcript_tail`] keeps by default.
pub const TAIL_TURNS: usize = 6;
/// The characters [`transcript_tail`] keeps by default.
pub const TAIL_LIMIT: usize = 6000;

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const T0: f64 = 1_792_000_000.0;

    #[test]
    fn decide_picks_the_action_for_each_situation() {
        let mut b = Breaker::default();
        for state in ["exited", "missing", "failed"] {
            let s = Situation { daemon_up: Some(false), ..Situation::new(state) };
            let (action, why) = decide(&s, &mut b, T0);
            assert_eq!(action, Action::Wait);
            assert!(why.contains("daemon"));
        }
        let unreachable = Situation { node_reachable: false, ..Situation::new("exited") };
        assert_eq!(decide(&unreachable, &mut b, T0).0, Action::Wait);
        assert!(b.attempts.is_empty(), "waiting spends no budget");

        assert_eq!(decide(&Situation::new("alive"), &mut b, T0).0, Action::None);
        assert_eq!(decide(&Situation::new("blocked"), &mut b, T0).0, Action::None);

        assert_eq!(decide(&Situation::new("failed"), &mut b, T0).0, Action::Fresh);
        assert_eq!(decide(&Situation::new("missing"), &mut b, T0).0, Action::Fresh);
        let changed = Situation { spec_changed: true, ..Situation::new("exited") };
        assert_eq!(decide(&changed, &mut b, T0).0, Action::Fresh);
        let stuck = Situation { resumable: false, ..Situation::new("exited") };
        assert_eq!(decide(&stuck, &mut b, T0).0, Action::Fresh);
    }

    #[test]
    fn an_exited_session_is_resumed_backed_off_then_started_fresh_until_the_budget_trips() {
        let mut b = Breaker::default();
        let exited = Situation::new("exited");
        assert_eq!(decide(&exited, &mut b, T0).0, Action::Resume);
        b.attempt(T0, "resume", "");
        assert_eq!(decide(&exited, &mut b, T0 + 1.0).0, Action::Backoff);
        assert_eq!(decide(&exited, &mut b, T0 + BACKOFF_S[0]).0, Action::Resume);

        let mut b = Breaker::default();
        let mut waits = Vec::new();
        for i in 0..3 {
            let now = T0 + f64::from(i);
            b.attempt(now, "resume", "");
            waits.push(b.next_try - now);
        }
        assert_eq!(waits, BACKOFF_S[..3]);
        let due = b.next_try;
        let (action, why) = decide(&exited, &mut b, due);
        assert_eq!(action, Action::Fresh, "three resumes that did not stay up");
        assert!(why.contains("resumes"));

        let mut b = Breaker::default();
        for i in 0..BUDGET {
            b.attempt(T0 + i as f64 * 700.0, if i < 3 { "resume" } else { "fresh" }, "");
        }
        let now = T0 + BUDGET as f64 * 700.0;
        assert!(b.tripped(now));
        assert_eq!(decide(&exited, &mut b, now).0, Action::GiveUp);
        assert_eq!(b.reason, "4 attempts in 1 h did not stay up");
        let later = b.tripped_at.unwrap() + HOLD_S;
        assert_eq!(decide(&exited, &mut b, later).0, Action::Resume);
    }

    #[test]
    fn the_breaker_decays_clears_when_healthy_and_round_trips_through_json() {
        let mut b = Breaker::default();
        for i in 0..BUDGET - 1 {
            b.attempt(T0 + i as f64, "resume", "");
        }
        b.attempt(T0 + DECAY_S + 10.0, "resume", "");
        assert!(!b.tripped(T0 + DECAY_S + 11.0), "old attempts decay");

        let mut b = Breaker::default();
        b.attempt(T0, "resume", "");
        let text = serde_json::to_string(&b.to_value()).unwrap();
        let again = Breaker::from_value(Some(&serde_json::from_str(&text).unwrap())).unwrap();
        assert_eq!(again, b);
        assert_eq!(b.to_value()["attempts"], json!([[T0, "resume"]]));
        assert_eq!(Breaker::from_value(None).unwrap(), Breaker::default());

        assert!(!b.healthy(T0 + 60.0, Some(T0 + 5.0)));
        assert!(b.healthy(T0 + SETTLE_S + 10.0, Some(T0 + 5.0)));
        assert!(b.attempts.is_empty());
        assert_eq!(b.next_try, 0.0);
    }

    #[test]
    fn outage_renders_cause_span_and_missed() {
        let o = Outage {
            up_at: Some(T0 + 3.0 * 3600.0 + 120.0),
            missed: Some(4),
            action: Some("resumed".into()),
            ..Outage::new(T0, "killed")
        };
        let text = o.render("node-a", "Read them with `example history`.");
        assert!(text.contains("on node-a because the Claude Code daemon on its node was killed"), "{text}");
        assert!(text.contains("(3 h 2 min)") && text.contains("4 messages arrived meanwhile."), "{text}");
        assert!(text.contains("resumed with its own conversation"));
        assert!(text.ends_with("Do not announce the restart in the chat."));
        assert_eq!(Outage::from_value(Some(&o.to_value())).unwrap(), Some(o));
        assert_eq!(Outage::from_value(None).unwrap(), None);
    }

    #[test]
    fn transcript_tail_keeps_what_was_said() {
        let lines = [
            json!({"type": "user", "message": {"content": "hello"}}).to_string(),
            json!({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hi there"}, {"type": "tool_use", "name": "Bash"}]}})
            .to_string(),
            json!({"type": "user", "message": {"content": [{"type": "tool_result"}]}}).to_string(),
            "not json".to_string(),
            json!({"type": "summary", "summary": "x"}).to_string(),
        ];
        assert_eq!(transcript_tail(&lines, TAIL_TURNS, TAIL_LIMIT), "[user] hello\n\n[assistant] hi there");
        assert_eq!(transcript_tail(&lines, 1, TAIL_LIMIT), "[assistant] hi there");
    }
}
