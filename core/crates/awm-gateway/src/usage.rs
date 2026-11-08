//! Usage per plugin (R132): one JSONL file per day under `<state>/usage/`, tallied on read.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

use awm_proto::{Surface, UsageEvent};
use serde::Serialize;

pub const WINDOW_DAYS: i64 = 60;

pub struct UsageStore {
    dir: PathBuf,
    write: Mutex<()>,
}

#[derive(Debug, Default, Serialize, PartialEq, Eq)]
pub struct Tally {
    pub days: i64,
    /// plugin → surface → count.
    pub plugins: BTreeMap<String, BTreeMap<Surface, u64>>,
    /// plugin → `tool verb` or page/job name → count.
    pub detail: BTreeMap<String, BTreeMap<String, u64>>,
}

impl UsageStore {
    pub fn new(dir: PathBuf) -> Self {
        Self { dir, write: Mutex::new(()) }
    }

    /// Append `event` to its day's file. Returns the event as written, with `v` and `ts` filled.
    pub fn record(&self, event: UsageEvent) -> UsageEvent {
        let event = event.stamped();
        let path = self.dir.join(format!("{}.jsonl", event.day()));
        let line = match serde_json::to_string(&event) {
            Ok(l) => l,
            Err(e) => {
                tracing::warn!("usage event not encoded: {e}");
                return event;
            }
        };
        let _guard = self.write.lock().unwrap_or_else(|p| p.into_inner());
        let result = std::fs::create_dir_all(&self.dir).and_then(|_| {
            let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&path)?;
            writeln!(f, "{line}")
        });
        if let Err(e) = result {
            tracing::warn!("usage event not written to {}: {e}", path.display());
        }
        event
    }

    /// Count the last `days` days of events. Files past the 60-day window are deleted.
    pub fn tally(&self, days: i64) -> Tally {
        let today = chrono::Utc::now().date_naive();
        let first = today - chrono::Duration::days(days.clamp(1, WINDOW_DAYS) - 1);
        let expired = today - chrono::Duration::days(WINDOW_DAYS);
        let mut tally = Tally { days, ..Tally::default() };
        let Ok(entries) = std::fs::read_dir(&self.dir) else { return tally };
        for entry in entries.flatten() {
            let path = entry.path();
            let Some(day) = path
                .file_name()
                .and_then(|n| n.to_str())
                .and_then(|n| n.strip_suffix(".jsonl"))
                .and_then(|d| chrono::NaiveDate::parse_from_str(d, "%Y-%m-%d").ok())
            else {
                continue;
            };
            if day < expired {
                let _ = std::fs::remove_file(&path);
                continue;
            }
            if day < first {
                continue;
            }
            let Ok(text) = std::fs::read_to_string(&path) else { continue };
            for ev in text.lines().filter_map(|l| serde_json::from_str::<UsageEvent>(l).ok()) {
                *tally.plugins.entry(ev.plugin.clone()).or_default().entry(ev.surface).or_default() += 1;
                let key = match (&ev.tool, &ev.verb, &ev.name) {
                    (Some(t), Some(v), _) => format!("{} {t} {v}", ev.surface.as_str()),
                    (_, _, Some(n)) => format!("{} {n}", ev.surface.as_str()),
                    _ => ev.surface.as_str().to_string(),
                };
                *tally.detail.entry(ev.plugin).or_default().entry(key).or_default() += 1;
            }
        }
        tally
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_and_tallies() {
        let dir = tempfile::tempdir().unwrap();
        let store = UsageStore::new(dir.path().join("usage"));
        let mut ev = UsageEvent::new("journal", Surface::Mcp);
        ev.tool = Some("journal".into());
        ev.verb = Some("post".into());
        store.record(ev.clone());
        store.record(ev);
        let mut page = UsageEvent::new("fileviewer", Surface::Page);
        page.name = Some("fileviewer".into());
        store.record(page);
        let mut old = UsageEvent::new("x", Surface::Cli);
        old.ts = "2020-01-01T00:00:00Z".into();
        store.record(old);

        let t = store.tally(60);
        assert_eq!(t.plugins["journal"][&Surface::Mcp], 2);
        assert_eq!(t.plugins["fileviewer"][&Surface::Page], 1);
        assert_eq!(t.detail["journal"]["mcp journal post"], 2);
        assert!(!t.plugins.contains_key("x"));
        assert!(!dir.path().join("usage/2020-01-01.jsonl").exists());
    }
}
