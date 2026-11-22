//! `genai.db`: one row per job.
//!
//! A job moves queued → running → done | failed, or to cancelled from queued or running.
//! A job running when the service stops stays running, and its provider resumes it on the next start.

use std::path::Path;

use awm_store::rusqlite::{self, params, Connection, OptionalExtension};
use awm_store::StoreError;
use serde::Serialize;

const SCHEMA_VERSION: i64 = 1;

const SCHEMA_SQL: &str = "\
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT NOT NULL PRIMARY KEY,
    style       TEXT NOT NULL,
    provider    TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    inputs      TEXT NOT NULL DEFAULT '[]',
    state       TEXT NOT NULL DEFAULT 'queued',
    error       TEXT NOT NULL DEFAULT '',
    outputs     TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);";

const COLS: &str = "id, style, provider, prompt, inputs, state, error, outputs, created_at, updated_at";

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Job {
    pub id: String,
    pub style: String,
    pub provider: String,
    pub prompt: String,
    /// Paths of the input files under the job's directory.
    pub inputs: Vec<String>,
    pub state: String,
    pub error: String,
    /// Paths of the result files under the job's directory.
    pub outputs: Vec<String>,
    pub created_at: String,
    pub updated_at: String,
}

fn now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn list(raw: String) -> Vec<String> {
    serde_json::from_str(&raw).unwrap_or_default()
}

fn job(r: &rusqlite::Row) -> rusqlite::Result<Job> {
    Ok(Job {
        id: r.get(0)?,
        style: r.get(1)?,
        provider: r.get(2)?,
        prompt: r.get(3)?,
        inputs: list(r.get(4)?),
        state: r.get(5)?,
        error: r.get(6)?,
        outputs: list(r.get(7)?),
        created_at: r.get(8)?,
        updated_at: r.get(9)?,
    })
}

#[derive(Clone)]
pub struct Store {
    db: awm_store::Store,
}

impl Store {
    pub fn open(path: &Path) -> anyhow::Result<Self> {
        let db = awm_store::Store::open(path)?;
        db.init(SCHEMA_SQL, SCHEMA_VERSION, &[])?;
        Ok(Store { db })
    }

    async fn run<T: Send + 'static>(&self, f: impl FnOnce(&Connection) -> rusqlite::Result<T> + Send + 'static) -> anyhow::Result<T> {
        Ok(self.db.write(move |tx| f(tx).map_err(StoreError::from)).await?)
    }

    pub async fn add(&self, id: &str, style: &str, provider: &str, prompt: &str, inputs: &[String]) -> anyhow::Result<()> {
        let (id, style, provider, prompt, inputs, at) = (id.to_string(), style.to_string(), provider.to_string(), prompt.to_string(), serde_json::to_string(inputs)?, now());
        self.run(move |c| c.execute(&format!("INSERT INTO jobs ({COLS}) VALUES (?1, ?2, ?3, ?4, ?5, 'queued', '', '[]', ?6, ?6)"), params![id, style, provider, prompt, inputs, at]).map(|_| ())).await
    }

    pub async fn get(&self, id: &str) -> anyhow::Result<Option<Job>> {
        let id = id.to_string();
        self.run(move |c| c.query_row(&format!("SELECT {COLS} FROM jobs WHERE id = ?1"), [id], job).optional()).await
    }

    pub async fn recent(&self, limit: i64, state: Option<String>) -> anyhow::Result<Vec<Job>> {
        self.run(move |c| {
            let mut st = c.prepare(&format!("SELECT {COLS} FROM jobs WHERE ?1 IS NULL OR state = ?1 ORDER BY created_at DESC, rowid DESC LIMIT ?2"))?;
            let rows = st.query_map(params![state, limit], job)?.collect();
            rows
        })
        .await
    }

    /// `provider`'s running jobs, oldest first: after a restart, the ones to resume.
    pub async fn running(&self, provider: &str) -> anyhow::Result<Vec<Job>> {
        let provider = provider.to_string();
        self.run(move |c| {
            let mut st = c.prepare(&format!("SELECT {COLS} FROM jobs WHERE provider = ?1 AND state = 'running' ORDER BY created_at, rowid"))?;
            let rows = st.query_map([provider], job)?.collect();
            rows
        })
        .await
    }

    /// Take the oldest queued job of `provider` and mark it running.
    pub async fn claim(&self, provider: &str) -> anyhow::Result<Option<Job>> {
        let (provider, at) = (provider.to_string(), now());
        self.run(move |c| {
            c.query_row(
                &format!("UPDATE jobs SET state = 'running', updated_at = ?2 WHERE id = (SELECT id FROM jobs WHERE provider = ?1 AND state = 'queued' ORDER BY created_at, rowid LIMIT 1) RETURNING {COLS}"),
                params![provider, at],
                job,
            )
            .optional()
        })
        .await
    }

    /// Move a job out of `from` states. Returns whether it moved.
    pub async fn finish(&self, id: &str, from: &[&str], state: &str, error: &str, outputs: &[String]) -> anyhow::Result<bool> {
        let from = serde_json::to_string(from)?;
        let (id, state, error, outputs, at) = (id.to_string(), state.to_string(), error.to_string(), serde_json::to_string(outputs)?, now());
        self.run(move |c| {
            c.execute(
                "UPDATE jobs SET state = ?2, error = ?3, outputs = ?4, updated_at = ?5 WHERE id = ?1 AND state IN (SELECT value FROM json_each(?6))",
                params![id, state, error, outputs, at, from],
            )
            .map(|n| n > 0)
        })
        .await
    }
}
