//! MIGRATION(session-ids): the one-time rewrite of `seats.db` from job short ids to session ids.

use std::path::{Path, PathBuf};

use anyhow::Context;
use awm_plugin_sdk::ids::{Platform, SessionId};
use rusqlite::{params, Connection};
use serde_json::Value;

// MIGRATION(session-ids): the jobs dir that maps a short id to its session.
pub fn jobs_dir() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default().join(".claude/jobs")
}

// MIGRATION(session-ids): a stored id as the canonical id, or None when no job knows it.
fn canonical(jobs: &Path, stored: &str) -> Option<String> {
    if SessionId::parse(stored).is_ok() {
        return Some(stored.to_string());
    }
    let state: Value = serde_json::from_str(&std::fs::read_to_string(jobs.join(stored).join("state.json")).ok()?).ok()?;
    SessionId::from_harness_native(Platform::Claude, state.get("sessionId")?.as_str()?).ok().map(|id| id.canonical())
}

// MIGRATION(session-ids): rename a body's `job` key to `session_id`, mapped. False when it maps to nothing.
fn rekey(jobs: &Path, body: &mut Value) -> bool {
    let Some(map) = body.as_object_mut() else { return true };
    let Some(job) = map.remove("job") else { return true };
    if let Some(public) = map.get_mut("public") {
        rekey(jobs, public);
    }
    match job.as_str().filter(|j| !j.is_empty()).map(|j| canonical(jobs, j)) {
        None => {
            map.insert("session_id".into(), Value::String(String::new()));
            true
        }
        Some(Some(id)) => {
            map.insert("session_id".into(), Value::String(id));
            true
        }
        Some(None) => false,
    }
}

// MIGRATION(session-ids): run once, in one transaction, while `seat_registry` still has its `job` column.
pub fn run(conn: &mut Connection, jobs: &Path) -> anyhow::Result<()> {
    let old: bool = conn.query_row("SELECT COUNT(*) > 0 FROM pragma_table_info('seat_registry') WHERE name = 'job'", [], |r| r.get(0))?;
    if !old {
        return Ok(());
    }
    std::fs::read_dir(jobs).with_context(|| format!("session-ids migration: cannot read {}", jobs.display()))?;
    let tx = conn.transaction()?;
    tx.execute_batch("ALTER TABLE seat_registry RENAME COLUMN job TO session_id")?;
    let seats: Vec<(String, String, String, String)> =
        tx.prepare("SELECT account, channel_id, session_id, previous FROM seat_registry")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?.collect::<Result<_, _>>()?;
    for (account, channel, stored, previous) in seats {
        let session = canonical(jobs, &stored).unwrap_or_default();
        let mut previous: Value = serde_json::from_str(&previous).unwrap_or_default();
        if !rekey(jobs, &mut previous) {
            previous["session_id"] = Value::String(String::new());
        }
        let lost = !stored.is_empty() && session.is_empty();
        tx.execute("UPDATE seat_registry SET session_id = ?1, previous = ?2, state = CASE WHEN ?5 THEN 'empty' ELSE state END WHERE account = ?3 AND channel_id = ?4", params![session, previous.to_string(), account, channel, lost])?;
    }
    tx.execute_batch("CREATE TABLE seat_lines_new (session_id TEXT PRIMARY KEY, account TEXT NOT NULL, channel_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '', at REAL NOT NULL)")?;
    let lines: Vec<(String, String, String, String, String, f64)> =
        tx.prepare("SELECT job, account, channel_id, thread_id, message_id, at FROM seat_lines")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))?.collect::<Result<_, _>>()?;
    for (stored, account, channel, thread, message, at) in lines {
        if let Some(session) = canonical(jobs, &stored) {
            tx.execute("INSERT OR REPLACE INTO seat_lines_new VALUES (?1, ?2, ?3, ?4, ?5, ?6)", params![session, account, channel, thread, message, at])?;
        }
    }
    tx.execute_batch("DROP TABLE seat_lines; ALTER TABLE seat_lines_new RENAME TO seat_lines")?;
    let briefed: Vec<String> = tx.prepare("SELECT DISTINCT briefed_to FROM seat_queue WHERE briefed_to != ''")?.query_map([], |r| r.get(0))?.collect::<Result<_, _>>()?;
    for stored in briefed {
        tx.execute("UPDATE seat_queue SET briefed_to = ?1 WHERE briefed_to = ?2", params![canonical(jobs, &stored).unwrap_or_default(), stored])?;
    }
    tx.execute("DELETE FROM outbox WHERE kind = 'live'", [])?;
    let rows: Vec<(i64, String)> = tx.prepare("SELECT id, body FROM outbox")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<Result<_, _>>()?;
    for (id, body) in rows {
        let mut body: Value = serde_json::from_str(&body).unwrap_or_default();
        if rekey(jobs, &mut body) {
            tx.execute("UPDATE outbox SET body = ?1 WHERE id = ?2", params![body.to_string(), id])?;
        } else {
            tx.execute("DELETE FROM outbox WHERE id = ?1", [id])?;
        }
    }
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const UUID: &str = "0a1b2c3d-1111-2222-3333-444455556666";

    #[test]
    fn short_ids_become_session_ids() {
        let dir = tempfile::tempdir().unwrap();
        let jobs = dir.path().join("jobs");
        std::fs::create_dir_all(jobs.join("0a1b2c3d")).unwrap();
        std::fs::write(jobs.join("0a1b2c3d/state.json"), format!(r#"{{"sessionId":"{UUID}"}}"#)).unwrap();
        let id = format!("cc{}", UUID.replace('-', ""));
        let mut c = Connection::open_in_memory().unwrap();
        c.execute_batch(
            "CREATE TABLE seat_registry (account TEXT, channel_id TEXT, job TEXT, previous TEXT, state TEXT);
             CREATE TABLE seat_queue (briefed_to TEXT);
             CREATE TABLE seat_lines (job TEXT PRIMARY KEY, account TEXT, channel_id TEXT, thread_id TEXT, message_id TEXT, at REAL);
             CREATE TABLE outbox (id INTEGER PRIMARY KEY, kind TEXT, body TEXT, at REAL);
             INSERT INTO seat_registry VALUES ('a', 'c1', '0a1b2c3d', '{\"job\":\"0a1b2c3d\",\"node\":\"n\"}', 'occupied'), ('a', 'c2', 'deadbeef', '{}', 'occupied');
             INSERT INTO seat_queue VALUES ('0a1b2c3d'), ('');
             INSERT INTO seat_lines VALUES ('0a1b2c3d', 'a', 'c1', '', 'm', 1), ('deadbeef', 'a', 'c2', '', 'm', 1);
             INSERT INTO outbox VALUES (1, 'mount', '{\"job\":\"0a1b2c3d\"}', 1), (2, 'live', '{\"jobs\":[]}', 1), (3, 'unmount', '{\"job\":\"deadbeef\"}', 1);",
        )
        .unwrap();
        run(&mut c, &jobs).unwrap();
        run(&mut c, &jobs).unwrap();
        let one = |sql: &str| c.query_row(sql, [], |r| r.get::<_, String>(0)).unwrap();
        assert_eq!(one("SELECT session_id || '|' || previous || '|' || state FROM seat_registry WHERE channel_id = 'c1'"), format!("{id}|{{\"node\":\"n\",\"session_id\":\"{id}\"}}|occupied"));
        assert_eq!(one("SELECT session_id || '|' || state FROM seat_registry WHERE channel_id = 'c2'"), "|empty");
        assert_eq!(one("SELECT group_concat(session_id) FROM seat_lines"), id);
        assert_eq!(one("SELECT briefed_to FROM seat_queue WHERE briefed_to != ''"), id);
        assert_eq!(one("SELECT group_concat(body, ' ') FROM outbox"), format!("{{\"session_id\":\"{id}\"}}"));
        assert!(run(&mut Connection::open_in_memory().unwrap(), &dir.path().join("missing")).is_ok(), "a database with no old column needs no jobs dir");
    }
}
