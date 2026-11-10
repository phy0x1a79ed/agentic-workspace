//! MIGRATION(session-ids): the v11 to v12 rewrite of `social.db` from job short ids to session ids.

use std::path::{Path, PathBuf};

use anyhow::Context;
use awm_plugin_sdk::ids::{Platform, SessionId};
use rusqlite::{params, Connection};
use serde_json::Value;

// MIGRATION(session-ids): the jobs dir that maps a short id to its session.
pub fn jobs_dir() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default().join(".claude/jobs")
}

// MIGRATION(session-ids): a stored id as the canonical id. A short id maps only on `here`, the node
// whose jobs dir this is, and a short id of another node maps to nothing.
struct Ids<'a> {
    jobs: &'a Path,
    here: &'a str,
}

impl Ids<'_> {
    fn canonical(&self, node: &str, stored: &str) -> anyhow::Result<Option<String>> {
        if SessionId::parse(stored).is_ok() {
            return Ok(Some(stored.to_string()));
        }
        if node != self.here {
            return Ok(None);
        }
        std::fs::metadata(self.jobs).with_context(|| format!("session-ids migration: cannot read {}", self.jobs.display()))?;
        let state = std::fs::read_to_string(self.jobs.join(stored).join("state.json")).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok());
        Ok(state.and_then(|s| SessionId::from_harness_native(Platform::Claude, s.get("sessionId")?.as_str()?).ok()).map(|id| id.canonical()))
    }

    /// Rename `body.job` to `session_id`, mapped. False when a stored id maps to nothing.
    fn rekey(&self, node: &str, body: &mut Value) -> anyhow::Result<bool> {
        let Some(map) = body.as_object_mut() else { return Ok(true) };
        let Some(job) = map.remove("job") else { return Ok(true) };
        let Some(stored) = job.as_str().filter(|j| !j.is_empty()) else {
            map.insert("session_id".into(), job);
            return Ok(true);
        };
        let id = self.canonical(node, stored)?;
        let mapped = id.is_some();
        map.insert("session_id".into(), id.map_or(Value::Null, Value::String));
        Ok(mapped)
    }
}

// MIGRATION(session-ids): run in the v11 to v12 step, inside its transaction.
pub fn session_ids(tx: &Connection, jobs: &Path, here: &str) -> anyhow::Result<()> {
    let ids = Ids { jobs, here };
    tx.execute_batch("ALTER TABLE social_mounts RENAME COLUMN job TO session_id; ALTER TABLE social_keepers RENAME COLUMN job TO session_id; ALTER TABLE seat_registry RENAME COLUMN job TO session_id; ALTER TABLE seat_lines RENAME COLUMN job TO session_id;")?;

    let rows: Vec<(String, String, String, String)> = tx.prepare("SELECT account, channel_id, node, session_id FROM social_mounts")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?.collect::<Result<_, _>>()?;
    for (account, channel, node, stored) in rows {
        match ids.canonical(&node, &stored)? {
            Some(id) => tx.execute("UPDATE social_mounts SET session_id = ?1 WHERE account = ?2 AND channel_id = ?3", params![id, account, channel])?,
            None => tx.execute("DELETE FROM social_mounts WHERE account = ?1 AND channel_id = ?2", params![account, channel])?,
        };
    }
    let rows: Vec<(String, String, String)> = tx.prepare("SELECT seat, node, session_id FROM social_keepers")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect::<Result<_, _>>()?;
    for (seat, node, stored) in rows {
        match ids.canonical(&node, &stored)? {
            Some(id) => tx.execute("UPDATE social_keepers SET session_id = ?1 WHERE seat = ?2", params![id, seat])?,
            None => tx.execute("DELETE FROM social_keepers WHERE seat = ?1", params![seat])?,
        };
    }
    let rows: Vec<(String, String, String, String, String)> =
        tx.prepare("SELECT account, channel_id, node, session_id, report FROM seat_registry")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?.collect::<Result<_, _>>()?;
    for (account, channel, node, stored, report) in rows {
        let id = if stored.is_empty() { Some(String::new()) } else { ids.canonical(&node, &stored)? };
        let mut report: Value = serde_json::from_str(&report).unwrap_or_default();
        if let Some(public) = report.get_mut("public") {
            ids.rekey(&node, public)?;
        }
        ids.rekey(&node, &mut report)?;
        tx.execute(
            "UPDATE seat_registry SET session_id = ?1, report = ?2, state = CASE WHEN ?3 THEN 'empty' ELSE state END WHERE account = ?4 AND channel_id = ?5",
            params![id.clone().unwrap_or_default(), report.to_string(), id.is_none(), account, channel],
        )?;
    }
    let rows: Vec<(String, String)> = tx.prepare("SELECT node, session_id FROM seat_lines")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<Result<_, _>>()?;
    for (node, stored) in rows {
        match ids.canonical(&node, &stored)? {
            Some(id) => tx.execute("UPDATE OR REPLACE seat_lines SET session_id = ?1 WHERE node = ?2 AND session_id = ?3", params![id, node, stored])?,
            None => tx.execute("DELETE FROM seat_lines WHERE node = ?1 AND session_id = ?2", params![node, stored])?,
        };
    }
    let rows: Vec<(i64, String, String)> = tx.prepare("SELECT id, node, body FROM seat_queue WHERE kind IN ('hand', 'released', 'seed') AND delivered_at = 0")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect::<Result<_, _>>()?;
    for (id, node, body) in rows {
        let mut body: Value = serde_json::from_str(&body).unwrap_or_default();
        let mut keep = ids.rekey(&node, &mut body)?;
        if let Some(state) = body.get_mut("state").filter(|s| s.is_object()) {
            if let Some(moved) = state.as_object_mut().and_then(|m| m.remove("job_node")) {
                state["session_node"] = moved;
            }
            if !ids.rekey(&node, state)? {
                state["session_id"] = Value::String(String::new());
            }
            if let Some(mut previous) = state.get("previous").and_then(Value::as_str).and_then(|p| serde_json::from_str::<Value>(p).ok()) {
                if !ids.rekey(&node, &mut previous)? {
                    previous["session_id"] = Value::String(String::new());
                }
                state["previous"] = Value::String(previous.to_string());
            }
            keep = true;
        }
        if keep {
            tx.execute("UPDATE seat_queue SET body = ?1 WHERE id = ?2", params![body.to_string(), id])?;
        } else {
            tx.execute("DELETE FROM seat_queue WHERE id = ?1", params![id])?;
        }
    }
    Ok(())
}
