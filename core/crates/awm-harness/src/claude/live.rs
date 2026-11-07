//! The conversation ids that belong to a Claude Code process running right now.
//!
//! Three places name one, and none is complete on its own: the per-pid session records (checked
//! against the process table, because the file outlives the process), the daemon roster's
//! `sessionId`, and each job record's `sessionId` / `resumeSessionId`, which is where a cleared or
//! respawned conversation went. The caller decides from this what may be deleted, so a record it
//! could not read is an error and never a shorter answer.

use std::collections::BTreeSet;
use std::path::Path;

use serde_json::{Map, Value};

use super::records::{py_int_str, py_str, py_truthy, read_json_strict, Unreadable};
use super::roster::RosterFile;
use super::ClaudeHome;

fn add(ids: &mut BTreeSet<String>, v: Option<&Value>) {
    if let Some(v) = v.filter(|v| py_truthy(v)) {
        ids.insert(py_str(v));
    }
}

fn running(pid: i64) -> bool {
    pid >= 0 && Path::new(&format!("/proc/{pid}")).exists()
}

fn entries(dir: &Path) -> Result<Vec<std::fs::DirEntry>, Unreadable> {
    let unreadable = |e: std::io::Error| Unreadable::new(dir, e.to_string());
    match std::fs::read_dir(dir) {
        Ok(rd) => rd.map(|e| e.map_err(unreadable)).collect(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(e) => Err(unreadable(e)),
    }
}

/// Every conversation id a running Claude Code process holds, or [`Unreadable`] naming the first
/// present record that could not be read. Absent files read as empty.
pub fn live_session_ids(home: &ClaudeHome) -> Result<BTreeSet<String>, Unreadable> {
    let mut ids = BTreeSet::new();

    for entry in entries(&home.sessions())? {
        let name = entry.file_name();
        let Some(stem) = name.to_str().and_then(|n| n.strip_suffix(".json")) else { continue };
        let Some(pid) = py_int_str(stem) else { continue };
        if running(pid) {
            add(&mut ids, read_json_strict(&entry.path())?.get("sessionId"));
        }
    }

    for worker in RosterFile::read_strict(home)?.workers.values() {
        add(&mut ids, worker.as_object().and_then(|w| w.get("sessionId")));
    }

    for entry in entries(&home.jobs())? {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let state: Map<String, Value> = read_json_strict(&path.join("state.json"))?;
        for key in ["sessionId", "resumeSessionId"] {
            add(&mut ids, state.get(key));
        }
    }

    Ok(ids)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claude::records::tests::{home, me, record};
    use serde_json::json;
    use tempfile::TempDir;

    fn set(items: &[&str]) -> BTreeSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    fn write_roster(h: &ClaudeHome, workers: Value) {
        std::fs::write(h.roster(), json!({"supervisorPid": me(), "workers": workers}).to_string()).unwrap();
    }

    fn write_job(h: &ClaudeHome, short: &str, state: Value) {
        std::fs::create_dir_all(h.jobs().join(short)).unwrap();
        std::fs::write(h.job_state(short), state.to_string()).unwrap();
    }

    #[test]
    fn live_ids_come_from_all_three_places() {
        let (_d, h) = home();
        record(&h, me(), json!({"sessionId": "from-record"}));
        record(&h, (1 << 22) + 99, json!({"sessionId": "dead-pid"}));
        write_roster(&h, json!({"ab12cd34": {"sessionId": "from-roster"}}));
        write_job(&h, "ab12cd34", json!({"sessionId": "job-sid", "resumeSessionId": "job-resume"}));
        std::fs::write(h.pins(), "not json, and not a job").unwrap();
        assert_eq!(live_session_ids(&h).unwrap(), set(&["from-record", "from-roster", "job-sid", "job-resume"]));
    }

    #[test]
    fn a_home_with_nothing_in_it_has_no_live_ids() {
        let dir = TempDir::new().unwrap();
        let h = ClaudeHome::at(dir.path().join("none"), dir.path().join("none.json"));
        assert_eq!(live_session_ids(&h).unwrap(), BTreeSet::new());
    }

    #[test]
    fn an_unreadable_record_is_not_an_empty_one() {
        type Break = (fn(&ClaudeHome), fn(&ClaudeHome) -> std::path::PathBuf);
        let breaks: [Break; 4] = [
            (|h| std::fs::write(h.roster(), "{torn").unwrap(), |h| h.roster()),
            (|h| std::fs::write(h.roster(), r#"{"workers": [1]}"#).unwrap(), |h| h.roster()),
            (|h| std::fs::write(h.job_state("ab12cd34"), "[1]").unwrap(), |h| h.job_state("ab12cd34")),
            (|h| std::fs::write(h.session_record(me()), "nope").unwrap(), |h| h.session_record(me())),
        ];
        for (break_it, culprit) in breaks {
            let (_d, h) = home();
            record(&h, me(), json!({"sessionId": "s"}));
            write_roster(&h, json!({}));
            write_job(&h, "ab12cd34", json!({"sessionId": "x"}));
            break_it(&h);
            assert_eq!(live_session_ids(&h).unwrap_err().path, culprit(&h));
        }
    }
}
