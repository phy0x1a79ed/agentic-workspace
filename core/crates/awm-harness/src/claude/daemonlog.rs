//! The daemon's own account of its starts and stops, read from `daemon.log`.
//!
//! `claude daemon status` has no JSON mode and says nothing about the past, so why the daemon last
//! stopped, and for how long, comes from this log.

use std::io::{Read, Seek, SeekFrom};

use chrono::{DateTime, NaiveDateTime};
use serde::ser::{SerializeMap, Serializer};
use serde::Serialize;

use super::ClaudeHome;

/// How many starts [`history`] keeps by default.
pub const HISTORY: usize = 10;

/// Why a daemon stopped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Stop {
    /// A `shutting down` line: when, the cause it logged, and its uptime when it logged one.
    Logged { at: String, cause: String, uptime_s: Option<u64> },
    /// A start with no shutdown line before it: a SIGKILL, an OOM kill or a power loss. `at` is
    /// the previous start's time, so a downtime measured from it is an upper bound.
    Killed { at: Option<String> },
}

impl Stop {
    pub fn cause(&self) -> &str {
        match self {
            Stop::Logged { cause, .. } => cause,
            Stop::Killed { .. } => "killed",
        }
    }

    pub fn at(&self) -> Option<&str> {
        match self {
            Stop::Logged { at, .. } => Some(at),
            Stop::Killed { at } => at.as_deref(),
        }
    }
}

impl Serialize for Stop {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self {
            Stop::Logged { at, cause, uptime_s } => {
                let mut m = s.serialize_map(Some(3))?;
                m.serialize_entry("at", at)?;
                m.serialize_entry("cause", cause)?;
                m.serialize_entry("uptime_s", uptime_s)?;
                m.end()
            }
            Stop::Killed { at } => {
                let mut m = s.serialize_map(Some(2))?;
                m.serialize_entry("cause", "killed")?;
                m.serialize_entry("at", at)?;
                m.end()
            }
        }
    }
}

/// One daemon start and why the daemon before it stopped, or (with no `pid`) a trailing stop that
/// no start followed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonEvent {
    pub at: Option<String>,
    pub pid: Option<u32>,
    pub version: Option<String>,
    pub origin: Option<String>,
    pub after: Option<Stop>,
    /// Whole seconds the daemon was down before this start. Serialized only for a start.
    pub down_s: Option<u64>,
}

impl Serialize for DaemonEvent {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let start = self.pid.is_some();
        let mut m = s.serialize_map(Some(if start { 6 } else { 5 }))?;
        m.serialize_entry("at", &self.at)?;
        m.serialize_entry("pid", &self.pid)?;
        m.serialize_entry("version", &self.version)?;
        m.serialize_entry("origin", &self.origin)?;
        m.serialize_entry("after", &self.after)?;
        if start {
            m.serialize_entry("down_s", &self.down_s)?;
        }
        m.end()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum State {
    Up,
    Down,
}

/// Up or down, since when, and why the daemon last stopped.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Summary {
    pub state: State,
    pub pid: Option<u32>,
    pub since: Option<String>,
    pub cause: Option<String>,
}

// Python's str.splitlines boundaries.
fn split_lines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let end = match c {
            '\r' => {
                if let Some((_, '\n')) = chars.peek() {
                    chars.next();
                    i + 2
                } else {
                    i + 1
                }
            }
            '\n' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}' => i + c.len_utf8(),
            _ => continue,
        };
        out.push(&text[start..i]);
        start = end;
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

fn non_space(s: &str) -> (&str, &str) {
    let end = s.find(char::is_whitespace).unwrap_or(s.len());
    s.split_at(end)
}

fn digits(s: &str) -> (&str, &str) {
    let end = s.find(|c: char| !c.is_ascii_digit()).unwrap_or(s.len());
    s.split_at(end)
}

// `[<at>] [supervisor] <rest>`
fn supervisor(line: &str) -> Option<(&str, &str)> {
    let rest = line.strip_prefix('[')?;
    let close = rest.find(']')?;
    let at = &rest[..close];
    if at.is_empty() {
        return None;
    }
    Some((at, rest[close..].strip_prefix("] [supervisor] ")?))
}

struct StartLine<'a> {
    at: &'a str,
    version: &'a str,
    pid: u32,
    origin: &'a str,
}

fn parse_start(line: &str) -> Option<StartLine<'_>> {
    let (at, rest) = supervisor(line)?;
    let rest = rest.strip_prefix("─── daemon start ─── version=")?;
    let (version, rest) = non_space(rest);
    let (pid, rest) = digits(rest.strip_prefix(" pid=").filter(|_| !version.is_empty())?);
    let pid = pid.parse().ok()?;
    let (origin, _) = non_space(rest.strip_prefix(" origin=")?);
    if origin.is_empty() {
        return None;
    }
    Some(StartLine { at, version, pid, origin })
}

fn parse_stop(line: &str) -> Option<Stop> {
    let (at, rest) = supervisor(line)?;
    let rest = rest.strip_prefix("shutting down (cause=")?;
    let end = rest.find([',', ')']).unwrap_or(rest.len());
    let (cause, rest) = rest.split_at(end);
    if cause.is_empty() {
        return None;
    }
    let uptime_s = rest.strip_prefix(", uptime=").and_then(|r| {
        let (n, r) = digits(r);
        r.starts_with('s').then(|| n.parse().ok()).flatten()
    });
    Some(Stop::Logged { at: at.to_string(), cause: cause.to_string(), uptime_s })
}

fn parse_time(s: &str) -> Option<Result<DateTime<chrono::FixedOffset>, NaiveDateTime>> {
    let s = s.replace('Z', "+00:00");
    if let Ok(t) = DateTime::parse_from_rfc3339(&s) {
        return Some(Ok(t));
    }
    ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f"]
        .iter()
        .find_map(|f| NaiveDateTime::parse_from_str(&s, f).ok())
        .map(Err)
}

// Whole seconds between two daemon.log timestamps, rounded half to even.
fn between(start: Option<&str>, end: &str) -> Option<u64> {
    let delta = match (parse_time(start?)?, parse_time(end)?) {
        (Ok(a), Ok(b)) => b - a,
        (Err(a), Err(b)) => b - a,
        _ => return None,
    };
    let seconds = delta.num_microseconds()? as f64 / 1e6;
    Some(seconds.round_ties_even().max(0.0) as u64)
}

/// Each daemon start in `text` (a daemon.log tail), with why the one before it stopped; the last
/// `limit` of them, or all when `limit` is 0.
pub fn history(text: &str, limit: usize) -> Vec<DaemonEvent> {
    let mut starts: Vec<DaemonEvent> = Vec::new();
    let mut stop: Option<Stop> = None;
    for line in split_lines(text) {
        if let Some(s) = parse_stop(line) {
            stop = Some(s);
        } else if let Some(m) = parse_start(line) {
            let after = stop.take().or_else(|| starts.last().map(|prev| Stop::Killed { at: prev.at.clone() }));
            let down_s = after.as_ref().and_then(|a| between(a.at(), m.at));
            starts.push(DaemonEvent {
                at: Some(m.at.to_string()),
                pid: Some(m.pid),
                version: Some(m.version.to_string()),
                origin: Some(m.origin.to_string()),
                after,
                down_s,
            });
        }
    }
    if let Some(stop) = stop {
        starts.push(DaemonEvent { at: None, pid: None, version: None, origin: None, after: Some(stop), down_s: None });
    }
    if limit > 0 && starts.len() > limit {
        starts.drain(..starts.len() - limit);
    }
    starts
}

/// The last `size` bytes of the home's `daemon.log`, or "" when it cannot be read. Blocking IO.
pub fn read_tail(home: &ClaudeHome, size: u64) -> String {
    let read = || -> std::io::Result<String> {
        let mut fh = std::fs::File::open(home.daemon_log())?;
        let end = fh.seek(SeekFrom::End(0))?;
        fh.seek(SeekFrom::Start(end.saturating_sub(size)))?;
        let mut bytes = Vec::new();
        fh.read_to_end(&mut bytes)?;
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    };
    read().unwrap_or_default()
}

/// The size [`read_tail`] reads by default.
pub const TAIL_BYTES: u64 = 256 * 1024;

/// Why the daemon most recently stopped, or None if the events show no stop.
pub fn last_stop(events: &[DaemonEvent]) -> Option<&Stop> {
    events.iter().rev().find_map(|e| e.after.as_ref())
}

/// Up or down, since when, and why it last stopped, given the running daemon's pid (if any) and
/// a daemon.log tail.
///
/// A daemon that is down while the log's last line is a start was killed without logging, so its
/// cause is `killed` and its time is unknown.
pub fn summary(pid: Option<u32>, text: &str) -> Summary {
    let events = history(text, HISTORY);
    let latest = events.last();
    let stop = last_stop(&events);
    let cause = stop.map(|s| s.cause().to_string());
    if let Some(pid) = pid {
        let since = latest.filter(|l| l.pid == Some(pid)).and_then(|l| l.at.clone());
        return Summary { state: State::Up, pid: Some(pid), since, cause };
    }
    if latest.is_some_and(|l| l.pid.is_some()) {
        return Summary { state: State::Down, pid: None, since: None, cause: Some("killed".into()) };
    }
    Summary { state: State::Down, pid: None, since: stop.and_then(|s| s.at().map(str::to_string)), cause }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const LOG: &str = "\
[2026-10-19T21:34:28.606Z] [supervisor] shutting down (cause=upgrade, uptime=7322s, leases=0, live_workers=2)
[2026-10-19T21:34:39.307Z] [supervisor] ─── daemon start ─── version=9.0.1 pid=299292 origin=service
[2026-10-19T22:00:00.000Z] [bg] something else
[2026-10-20T02:40:43.397Z] [supervisor] ─── daemon start ─── version=9.0.1 pid=3790938 origin=transient
[2026-10-20T03:00:00.000Z] [supervisor] shutting down (cause=signal, uptime=1157s, leases=1, live_workers=15)
";

    // Everything but the last line.
    fn without_last_stop() -> String {
        let trimmed = LOG.trim_end_matches('\n');
        format!("{}\n", &trimmed[..trimmed.rfind('\n').unwrap()])
    }

    #[test]
    fn history_names_why_each_daemon_before_a_start_stopped() {
        let events = history(LOG, HISTORY);
        assert_eq!(
            serde_json::to_value(&events[0].after).unwrap(),
            json!({"at": "2026-10-19T21:34:28.606Z", "cause": "upgrade", "uptime_s": 7322})
        );
        assert_eq!(events[0].down_s, Some(11));
        assert_eq!(events[1].pid, Some(3790938));
        assert_eq!(events[1].after.as_ref().unwrap().cause(), "killed");
        assert_eq!(events[2].pid, None);
        assert_eq!(events[2].after.as_ref().unwrap().cause(), "signal");
        let trailing = serde_json::to_value(&events[2]).unwrap();
        assert!(trailing.get("down_s").is_none());
    }

    #[test]
    fn summary_of_a_daemon_that_stopped_and_has_not_returned() {
        assert_eq!(
            serde_json::to_value(summary(None, LOG)).unwrap(),
            json!({"state": "down", "pid": null, "since": "2026-10-20T03:00:00.000Z", "cause": "signal"})
        );
    }

    #[test]
    fn summary_of_a_daemon_killed_without_a_word() {
        let out = summary(None, &without_last_stop());
        assert_eq!(out.state, State::Down);
        assert_eq!(out.cause.as_deref(), Some("killed"));
    }

    #[test]
    fn summary_of_a_running_daemon() {
        assert_eq!(
            serde_json::to_value(summary(Some(3790938), &without_last_stop())).unwrap(),
            json!({"state": "up", "pid": 3790938, "since": "2026-10-20T02:40:43.397Z", "cause": "killed"})
        );
    }

    #[test]
    fn downtime_rounds_half_to_even() {
        let log = "\
[2026-10-19T10:00:00.000Z] [supervisor] shutting down (cause=upgrade, uptime=5s)
[2026-10-19T10:00:10.500Z] [supervisor] ─── daemon start ─── version=9.0.1 pid=11 origin=service
[2026-10-19T10:01:00.000Z] [supervisor] shutting down (cause=upgrade)
[2026-10-19T10:01:11.500Z] [supervisor] ─── daemon start ─── version=9.0.1 pid=12 origin=service
";
        let events = history(log, HISTORY);
        assert_eq!(events[0].down_s, Some(10));
        assert_eq!(events[1].down_s, Some(12));
        assert_eq!(events[1].after, Some(Stop::Logged { at: "2026-10-19T10:01:00.000Z".into(), cause: "upgrade".into(), uptime_s: None }));
    }
}
