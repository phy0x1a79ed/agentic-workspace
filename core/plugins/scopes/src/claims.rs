//! Scope claims and the queue for a busy scope (scopes.md R3, R41, R42, R46).
//!
//! A live session holds the claim on its scope, and the claim dies with the session. A
//! session counts as live while its process runs under the same start time, or while the
//! harness lists it as waiting on other sessions (R51). A claim found dead on read frees the
//! scope at once and hands it to the first live session in its queue.

use std::collections::HashSet;
use std::path::Path;

use awm_store::rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{json, Value};

use crate::db::now_ms;
use crate::outbound;
use crate::{Fail, Op};

/// The kernel start time of `pid`, which tells a live process from a reused pid. This is
/// the exact string the Python stored: field 22 of `/proc/<pid>/stat`.
pub fn proc_start(proc_root: &Path, pid: i64) -> Option<String> {
    let stat = std::fs::read_to_string(proc_root.join(pid.to_string()).join("stat")).ok()?;
    let (_, rest) = stat.rsplit_once(')')?;
    rest.split_whitespace().nth(19).map(str::to_string)
}

/// One claim or queue row.
#[derive(Debug, Clone)]
struct Row_ {
    session: String,
    pid: Option<i64>,
    proc_start: Option<String>,
    job: Option<String>,
    /// `claimed_at` for a claim, `asked_at` for a waiter.
    at: i64,
    notified_at: Option<i64>,
}

fn claim_row(r: &Row<'_>) -> awm_store::rusqlite::Result<Row_> {
    Ok(Row_ { session: r.get("session")?, pid: r.get("pid")?, proc_start: r.get("proc_start")?, job: r.get("job")?, at: r.get("claimed_at")?, notified_at: None })
}

fn waiter_row(r: &Row<'_>) -> awm_store::rusqlite::Result<Row_> {
    Ok(Row_ { session: r.get("session")?, pid: r.get("pid")?, proc_start: r.get("proc_start")?, job: r.get("job")?, at: r.get("asked_at")?, notified_at: r.get("notified_at")? })
}

fn public(row: Option<&Row_>) -> Value {
    match row {
        None => Value::Null,
        Some(r) => json!({"session_id": r.session, "job": r.job, "pid": r.pid, "since": r.at}),
    }
}

impl Op {
    fn waiting(&self) -> HashSet<String> {
        outbound::waiting_sessions(self.out.as_ref())
    }

    fn holder_live(&self, row: &Row_, waiting: &HashSet<String>) -> bool {
        if let (Some(pid), Some(start)) = (row.pid, row.proc_start.as_deref()) {
            if pid > 0 && !start.is_empty() && proc_start(&self.proc_root, pid).as_deref() == Some(start) {
                return true;
            }
        }
        waiting.contains(&row.session)
    }

    /// The live claim on the scope. A dead one is dropped here.
    fn holder(&self, c: &Connection, project: &str, scope: &str, waiting: &HashSet<String>) -> Result<Option<Row_>, Fail> {
        let row = c.query_row("SELECT * FROM scope_claims WHERE project=? AND scope=?", [project, scope], claim_row).optional()?;
        let Some(row) = row else { return Ok(None) };
        if self.holder_live(&row, waiting) {
            return Ok(Some(row));
        }
        c.execute("DELETE FROM scope_claims WHERE project=? AND scope=?", [project, scope])?;
        tracing::info!("scopes: claim of {project}/{scope} by {} ended with its session", row.session);
        Ok(None)
    }

    /// The live waiters, first come first served (R46). Dead ones leave the queue.
    fn queue(&self, c: &Connection, project: &str, scope: &str, waiting: &HashSet<String>) -> Result<Vec<Row_>, Fail> {
        let mut stmt = c.prepare("SELECT * FROM scope_waiters WHERE project=? AND scope=? ORDER BY asked_at, rowid")?;
        let rows: Vec<Row_> = stmt.query_map([project, scope], waiter_row)?.collect::<Result<_, _>>()?;
        let mut live = vec![];
        for r in rows {
            if self.holder_live(&r, waiting) {
                live.push(r);
            } else {
                c.execute("DELETE FROM scope_waiters WHERE project=? AND scope=? AND session=?", [project, scope, &r.session])?;
            }
        }
        Ok(live)
    }

    /// Tell the first waiter its scope is free, once.
    fn hand_over(&self, c: &Connection, project: &str, scope: &str, head: Option<&Row_>) -> Result<bool, Fail> {
        let Some(head) = head else { return Ok(false) };
        let Some(job) = head.job.as_deref().filter(|j| !j.is_empty()) else { return Ok(false) };
        if head.notified_at.is_some_and(|n| n != 0) {
            return Ok(false);
        }
        let text = format!(
            "[awm] scope {project}/{scope} is free and you are first in its queue. Move there with reflection cd now. \
             Add to your question on the board that the conflict is clear (scopes.md R7)."
        );
        let sent = outbound::notify(self.out.as_ref(), job, &text);
        if sent {
            c.execute("UPDATE scope_waiters SET notified_at=? WHERE project=? AND scope=? AND session=?", params![now_ms(), project, scope, head.session])?;
        }
        Ok(sent)
    }

    /// Claim a scope for a session, or say who holds it. A terminal session (R41) is warned,
    /// never blocked or queued. With `queue` a refused session joins the queue.
    #[allow(clippy::too_many_arguments)]
    pub fn claim(&self, project: &str, scope: &str, session_id: &str, pid: Option<i64>, job: Option<&str>, queue: bool, terminal: bool) -> Result<Value, Fail> {
        if session_id.is_empty() {
            return Err(Fail::value("session_id is required"));
        }
        let start = pid.filter(|p| *p > 0).and_then(|p| proc_start(&self.proc_root, p));
        let waiting = self.waiting();
        self.write(|c| {
            let holder = self.holder(c, project, scope, &waiting)?;
            let waiters = self.queue(c, project, scope, &waiting)?;
            let mine = waiters.iter().find(|w| w.session == session_id);
            let ahead: Vec<&Row_> = match mine {
                Some(m) => waiters.iter().filter(|w| (w.at, &w.session) < (m.at, &m.session)).collect(),
                None => waiters.iter().filter(|w| w.session != session_id).collect(),
            };
            let enqueue = |c: &Connection| -> Result<(), Fail> {
                c.execute(
                    "INSERT INTO scope_waiters (project, scope, session, pid, proc_start, job, asked_at) VALUES (?,?,?,?,?,?,?)",
                    params![project, scope, session_id, pid, start, job, now_ms()],
                )?;
                Ok(())
            };
            if let Some(h) = holder.as_ref().filter(|h| h.session != session_id) {
                let mut out = json!({"project": project, "scope": scope, "claimed": false, "holder": public(Some(h))});
                if terminal {
                    out["ok"] = json!(true);
                    out["warning"] = json!(format!("session {} holds {project}/{scope}; you are not blocked", h.session));
                    return Ok(out);
                }
                if queue {
                    if mine.is_none() {
                        enqueue(c)?;
                    }
                    out["ok"] = json!(false);
                    out["queued_position"] = json!(ahead.len() + 1);
                    return Ok(out);
                }
                out["ok"] = json!(false);
                out["reason"] = json!(format!(
                    "session {} holds {project}/{scope}. Pass queue=true to wait for it, or ask Tony on the board (R7).",
                    h.session
                ));
                return Ok(out);
            }
            if holder.is_none() && !ahead.is_empty() && !terminal {
                if queue && mine.is_none() {
                    enqueue(c)?;
                }
                self.hand_over(c, project, scope, Some(ahead[0]))?;
                let position = if queue || mine.is_some() { json!(ahead.len() + 1) } else { Value::Null };
                return Ok(json!({
                    "ok": false, "project": project, "scope": scope, "claimed": false, "holder": null,
                    "reason": format!("{} session(s) asked for {project}/{scope} first", ahead.len()),
                    "queued_position": position,
                }));
            }
            c.execute(
                "INSERT INTO scope_claims (project, scope, session, pid, proc_start, job, claimed_at) VALUES (?,?,?,?,?,?,?) \
                 ON CONFLICT (project, scope) DO UPDATE SET session=excluded.session, pid=excluded.pid, \
                 proc_start=excluded.proc_start, job=excluded.job, claimed_at=excluded.claimed_at",
                params![project, scope, session_id, pid, start, job, now_ms()],
            )?;
            c.execute("DELETE FROM scope_waiters WHERE project=? AND scope=? AND session=?", [project, scope, session_id])?;
            touch(c, project, scope)?;
            Ok(json!({"ok": true, "project": project, "scope": scope, "claimed": true, "holder": {"session_id": session_id, "job": job, "pid": pid}}))
        })
    }

    /// End a session's claim and hand the scope to the head of its queue.
    pub fn release(&self, project: &str, scope: &str, session_id: &str) -> Result<Value, Fail> {
        let waiting = self.waiting();
        self.write(|c| {
            let released = c.execute("DELETE FROM scope_claims WHERE project=? AND scope=? AND session=?", [project, scope, session_id])? > 0;
            if released {
                touch(c, project, scope)?;
            }
            let holder = self.holder(c, project, scope, &waiting)?;
            let waiters = self.queue(c, project, scope, &waiting)?;
            let notified = holder.is_none() && self.hand_over(c, project, scope, waiters.first())?;
            let next = if holder.is_none() { public(waiters.first()) } else { Value::Null };
            Ok(json!({"ok": true, "project": project, "scope": scope, "released": released, "next": next, "notified": notified}))
        })
    }

    /// Leave a scope's queue, as a session sent elsewhere or cancelled does (R46).
    pub fn withdraw(&self, project: &str, scope: &str, session_id: &str) -> Result<Value, Fail> {
        let left = self.write(|c| Ok(c.execute("DELETE FROM scope_waiters WHERE project=? AND scope=? AND session=?", [project, scope, session_id])? > 0))?;
        Ok(json!({"ok": true, "project": project, "scope": scope, "withdrawn": left}))
    }

    /// The live session that holds the scope, as `{session_id, job, pid, since}`.
    pub fn live_holder(&self, project: &str, scope: &str) -> Result<Option<Value>, Fail> {
        let waiting = self.waiting();
        self.write(|c| Ok(self.holder(c, project, scope, &waiting)?.map(|h| public(Some(&h)))))
    }

    /// Every live claim and queue, optionally for one project or scope.
    pub fn listing(&self, project: Option<&str>, scope: Option<&str>) -> Result<Value, Fail> {
        let waiting = self.waiting();
        let mut clauses = vec![];
        let mut vals: Vec<String> = vec![];
        for (col, val) in [("project", project), ("scope", scope)] {
            if let Some(v) = val.filter(|v| !v.is_empty()) {
                clauses.push(format!("{col}=?"));
                vals.push(v.to_string());
            }
        }
        let clause = if clauses.is_empty() { String::new() } else { format!(" WHERE {}", clauses.join(" AND ")) };
        let out = self.write(|c| {
            let sql = format!("SELECT project, scope FROM scope_claims{clause} UNION SELECT project, scope FROM scope_waiters{clause} ORDER BY project, scope");
            let both: Vec<&String> = vals.iter().chain(vals.iter()).collect();
            let mut stmt = c.prepare(&sql)?;
            let keys: Vec<(String, String)> =
                stmt.query_map(awm_store::rusqlite::params_from_iter(both), |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<Result<_, _>>()?;
            let mut out = vec![];
            for (p, s) in keys {
                let holder = self.holder(c, &p, &s, &waiting)?;
                let waiters = self.queue(c, &p, &s, &waiting)?;
                if holder.is_some() || !waiters.is_empty() {
                    let queue: Vec<Value> = waiters.iter().map(|w| public(Some(w))).collect();
                    out.push(json!({"project": p, "scope": s, "holder": public(holder.as_ref()), "queue": queue}));
                }
            }
            Ok(out)
        })?;
        let count = out.len();
        Ok(json!({"claims": out, "count": count}))
    }

    pub fn last_used(&self, project: &str, scope: &str) -> Result<Option<i64>, Fail> {
        self.read(|c| Ok(c.query_row("SELECT last_used FROM scope_use WHERE project=? AND scope=?", [project, scope], |r| r.get(0)).optional()?))
    }

    /// Add one to a counter and return the new value.
    pub fn bump(&self, name: &str) -> Result<i64, Fail> {
        self.write(|c| {
            c.execute("INSERT INTO scope_counters (name, value) VALUES (?, 1) ON CONFLICT (name) DO UPDATE SET value=value+1", [name])?;
            Ok(c.query_row("SELECT value FROM scope_counters WHERE name=?", [name], |r| r.get(0))?)
        })
    }
}

fn touch(c: &Connection, project: &str, scope: &str) -> Result<(), Fail> {
    c.execute(
        "INSERT INTO scope_use (project, scope, last_used) VALUES (?,?,?) ON CONFLICT (project, scope) DO UPDATE SET last_used=excluded.last_used",
        params![project, scope, now_ms()],
    )?;
    Ok(())
}
