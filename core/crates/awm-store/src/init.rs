use crate::store::{Store, StoreError};
use rusqlite::fallible_iterator::FallibleIterator;
use rusqlite::{Connection, OptionalExtension};
use std::path::PathBuf;

/// One schema step: the SQL that takes a database from `from` to `from + 1`.
#[derive(Debug, Clone, Copy)]
pub struct Migration {
    pub from: i64,
    pub sql: &'static str,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InitOutcome {
    /// A fresh database, created at the schema's version.
    Created { version: i64 },
    /// Already at the schema's version.
    Current { version: i64 },
    /// Migrated after a backup to `backup`.
    Migrated { from: i64, to: i64, backup: PathBuf },
}

impl Store {
    /// Create the schema on a fresh database, or migrate an older one to `version`.
    ///
    /// Before a migration it writes a `VACUUM INTO` backup next to the database, and a failed
    /// backup refuses the start. A database at a version above `version` is refused.
    ///
    /// **CAUTION** Each step runs in autocommit, as Python's `executescript` does, and a step
    /// that fails on `duplicate column name` or `no such table` counts as done. That is how a
    /// step a crash interrupted resumes. Wrapping a step in a transaction changes it.
    pub fn init(&self, schema: &str, version: i64, migrations: &[Migration]) -> Result<InitOutcome, StoreError> {
        let conn = self.connection()?;
        let current: Option<i64> = conn
            .query_row("SELECT version FROM schema_version", [], |r| r.get(0))
            .optional()
            .or_else(|e| if is_no_such_table(&e) { Ok(None) } else { Err(e) })?;
        let Some(current) = current else {
            conn.execute_batch(schema)?;
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)", [])?;
            let existing: Option<i64> =
                conn.query_row("SELECT version FROM schema_version", [], |r| r.get(0)).optional()?;
            if existing.is_none() {
                conn.execute("INSERT INTO schema_version (version) VALUES (?1)", [version])?;
            } else {
                conn.execute("UPDATE schema_version SET version = ?1", [version])?;
            }
            return Ok(InitOutcome::Created { version });
        };
        if current > version {
            return Err(StoreError::VersionAhead { path: self.path().into(), found: current, ours: version });
        }
        if current == version {
            return Ok(InitOutcome::Current { version });
        }
        let backup = self.backup(&conn, current)?;
        migrate(&conn, current, version, migrations)?;
        Ok(InitOutcome::Migrated { from: current, to: version, backup })
    }

    fn backup(&self, conn: &Connection, from: i64) -> Result<PathBuf, StoreError> {
        let name = self.path().file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let dir = self.path().parent().map(PathBuf::from).unwrap_or_default();
        let mut dest = dir.join(format!("{name}.v{from}.bak"));
        if dest.exists() {
            dest = dir.join(format!("{name}.v{from}.{}.bak", crate::time::now_us() / 1000));
        }
        conn.execute("VACUUM INTO ?1", [dest.to_string_lossy()])
            .map_err(|source| StoreError::Backup { path: self.path().into(), from, source })?;
        Ok(dest)
    }
}

fn is_no_such_table(e: &rusqlite::Error) -> bool {
    e.to_string().contains("no such table")
}

fn migrate(conn: &Connection, mut current: i64, target: i64, migrations: &[Migration]) -> Result<(), StoreError> {
    while current < target {
        let next = current + 1;
        let step = migrations
            .iter()
            .find(|m| m.from == current)
            .ok_or(StoreError::NoMigration { from: current, to: next })?;
        let script = format!(
            "PRAGMA foreign_keys=OFF;\n{}\nUPDATE schema_version SET version = {next};\nPRAGMA foreign_key_check;\nPRAGMA foreign_keys=ON;",
            step.sql
        );
        if let Err(e) = run_script(conn, &script) {
            let msg = e.to_string();
            if msg.contains("duplicate column name") || msg.contains("no such table") {
                conn.execute("UPDATE schema_version SET version = ?1", [next])?;
            } else {
                return Err(e.into());
            }
        }
        current = next;
    }
    Ok(())
}

/// Run each statement of `script` in turn, stepping past any rows a statement returns, as
/// `executescript` does with `PRAGMA foreign_key_check`.
fn run_script(conn: &Connection, script: &str) -> rusqlite::Result<()> {
    let mut batch = rusqlite::Batch::new(conn, script);
    while let Some(mut stmt) = batch.next()? {
        let mut rows = stmt.raw_query();
        while rows.next()?.is_some() {}
    }
    Ok(())
}
