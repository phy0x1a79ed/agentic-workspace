//! The dvc plugin: the chinook Globus collection as an append-only remote for the shared DVC
//! cache, the two backup jobs and their run history, and the scope data verbs over
//! awm-dvcdata. routine's timers fire the jobs; this crate decides what they do.

pub mod backup;
pub mod clock;
pub mod config;
pub mod coverage;
pub mod cron;
pub mod globus;
pub mod jobs;
pub mod runs;
pub mod scopedata;
pub mod service;
pub mod snapshot;
pub mod sync;
pub mod transfer;
pub mod watcher;

use std::path::PathBuf;

use chrono::{DateTime, Local, NaiveDate, NaiveDateTime, TimeZone, Utc};
use serde_json::Value;

/// Where the plugin reads and writes: the workspace and the node's state dir.
#[derive(Debug, Clone)]
pub struct Paths {
    pub workspace: PathBuf,
    pub awm_dir: PathBuf,
}

impl Paths {
    pub fn from_env() -> Self {
        let ws = awm_plugin_sdk::config::workspace_root();
        Self { workspace: std::fs::canonicalize(&ws).unwrap_or(ws), awm_dir: awm_plugin_sdk::config::awm_dir() }
    }

    pub fn state_dir(&self) -> PathBuf {
        self.awm_dir.join("services").join("dvc")
    }

    pub fn db(&self) -> PathBuf {
        self.state_dir().join("dvc.db")
    }

    pub fn projects(&self) -> PathBuf {
        self.workspace.join("projects")
    }

    /// The one place bytes live on this machine. Every project's `.dvc/config.local` points
    /// here, so syncing this one directory covers every project's data.
    pub fn shared_cache(&self) -> PathBuf {
        self.workspace.join("data").join(".dvc_cache")
    }
}

pub fn now() -> f64 {
    let d = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    d.as_secs_f64()
}

/// A uuid4 as 32 hex digits, as the Python rows carry.
pub fn new_uuid() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

fn round_half_even(x: f64) -> f64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 && r % 2.0 != 0.0 {
        r - x.signum()
    } else {
        r
    }
}

/// Python's `datetime.isoformat()` of an aware UTC time: microseconds only when nonzero.
pub fn py_iso(dt: DateTime<Utc>) -> String {
    let base = dt.format("%Y-%m-%dT%H:%M:%S").to_string();
    let us = dt.timestamp_subsec_micros();
    if us == 0 {
        format!("{base}+00:00")
    } else {
        format!("{base}.{us:06}+00:00")
    }
}

/// Epoch seconds as Python's `datetime.fromtimestamp(ts, utc).isoformat()`; "" for 0.
pub fn iso(ts: f64) -> String {
    if ts == 0.0 || !ts.is_finite() {
        return String::new();
    }
    let mut secs = ts.trunc() as i64;
    let mut us = round_half_even(ts.fract() * 1e6) as i64;
    if us >= 1_000_000 {
        secs += 1;
        us -= 1_000_000;
    } else if us < 0 {
        secs -= 1;
        us += 1_000_000;
    }
    match Utc.timestamp_opt(secs, (us * 1000) as u32) {
        chrono::LocalResult::Single(dt) => py_iso(dt),
        _ => String::new(),
    }
}

pub fn iso_value(v: Option<&Value>) -> String {
    iso(v.and_then(Value::as_f64).unwrap_or(0.0))
}

pub fn now_iso() -> String {
    py_iso(Utc::now())
}

/// An epoch number or an ISO-8601 string as epoch seconds, as Python's `to_epoch`. A naive
/// time is local.
pub fn to_epoch(v: Option<&Value>) -> Option<f64> {
    match v? {
        Value::Null => None,
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::String(s) => {
            let s = s.trim();
            if s.is_empty() {
                return None;
            }
            if let Ok(f) = s.parse::<f64>() {
                return Some(f);
            }
            parse_iso(s)
        }
        _ => None,
    }
}

fn parse_iso(s: &str) -> Option<f64> {
    let t = s.replacen(' ', "T", 1);
    let t = t.strip_suffix('Z').map(|x| format!("{x}+00:00")).unwrap_or(t);
    if let Ok(dt) = DateTime::parse_from_rfc3339(&t) {
        return Some(dt.timestamp_micros() as f64 / 1e6);
    }
    for f in ["%Y-%m-%dT%H:%M:%S%.f%:z", "%Y-%m-%dT%H:%M%:z"] {
        if let Ok(dt) = DateTime::parse_from_str(&t, f) {
            return Some(dt.timestamp_micros() as f64 / 1e6);
        }
    }
    let naive = ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H"]
        .iter()
        .find_map(|f| NaiveDateTime::parse_from_str(&t, f).ok())
        .or_else(|| NaiveDate::parse_from_str(&t, "%Y-%m-%d").ok().and_then(|d| d.and_hms_opt(0, 0, 0)))?;
    Local.from_local_datetime(&naive).earliest().map(|dt| dt.timestamp_micros() as f64 / 1e6)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn iso_matches_python() {
        assert_eq!(iso(1791074502.5088522), "2026-10-04T00:41:42.508852+00:00");
        assert_eq!(iso(1791086225.7869637), "2026-10-04T03:57:05.786964+00:00");
        assert_eq!(iso(0.0), "");
        assert_eq!(iso(1791072000.0), "2026-10-04T00:00:00+00:00");
        let t = 1791074502.508852;
        assert!((to_epoch(Some(&json!(iso(t)))).unwrap() - t).abs() < 1e-6);
        assert_eq!(to_epoch(Some(&json!("1700000000"))), Some(1700000000.0));
        assert_eq!(to_epoch(Some(&json!("not a time"))), None);
        assert_eq!(to_epoch(Some(&json!("2026-10-04T00:00:00Z"))), Some(1791072000.0));
    }
}
