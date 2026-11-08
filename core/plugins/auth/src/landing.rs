//! The landing page's own store: which tags each page carries, which tags are selected as a
//! filter, and each page's display name.
//!
//! `landing.db` keeps the schema of the former `httpsfront.db`, and [`copy_once`] carries that
//! file over on the first start.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;

use rusqlite::{params_from_iter, Connection, OpenFlags, OptionalExtension};

/// The schema version the former `httpsfront.db` carried, and this store keeps.
pub const SCHEMA_VERSION: i64 = 2;

pub const SCHEMA_SQL: &str = "\
CREATE TABLE IF NOT EXISTS page_tags (
    page TEXT NOT NULL,
    tag  TEXT NOT NULL,
    PRIMARY KEY (page, tag)
);
CREATE TABLE IF NOT EXISTS selected_tags (
    tag TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS page_names (
    page         TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);
";

/// Upgrade steps for a store at an older version, as `(from, sql)`.
const MIGRATIONS: &[(i64, &str)] = &[(
    1,
    "\
CREATE TABLE IF NOT EXISTS page_names (
    page         TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);
",
)];

/// Copies `old` to `new` by `VACUUM INTO`, once. Returns whether it copied.
///
/// Does nothing when `new` exists or `old` does not. Never writes to `old`, which it opens
/// read-only, and never overwrites `new`: the copy lands beside it and is linked into place, so
/// a crash mid-copy leaves no half-written `new` behind.
pub fn copy_once(old: &Path, new: &Path) -> anyhow::Result<bool> {
    if new.exists() || !old.exists() {
        return Ok(false);
    }
    if let Some(dir) = new.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut tmp = new.as_os_str().to_owned();
    tmp.push(".copying");
    let tmp = std::path::PathBuf::from(tmp);
    let _ = std::fs::remove_file(&tmp);
    let src = Connection::open_with_flags(old, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    src.execute("VACUUM INTO ?1", [tmp.to_string_lossy()])?;
    drop(src);
    let linked = std::fs::hard_link(&tmp, new);
    let _ = std::fs::remove_file(&tmp);
    match linked {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
        Err(e) => Err(e.into()),
    }
}

/// `" ".join(str(tag).split())`: runs of whitespace collapsed, ends trimmed.
fn normalize(tag: &str) -> String {
    awm_pycompat::pystr::split(tag).join(" ")
}

/// The landing store, open on one connection.
pub struct Landing {
    db: Mutex<Connection>,
}

impl Landing {
    /// Opens the store at `path`, creating or migrating it as needed.
    pub fn open(path: &Path) -> anyhow::Result<Landing> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let db = Connection::open(path)?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "synchronous", "NORMAL")?;
        db.pragma_update(None, "foreign_keys", "ON")?;
        init(&db)?;
        Ok(Landing { db: Mutex::new(db) })
    }

    fn conn(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.db.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn column(&self, sql: &str, params: &[&str]) -> rusqlite::Result<Vec<String>> {
        let db = self.conn();
        let mut st = db.prepare_cached(sql)?;
        let rows = st.query_map(params_from_iter(params), |r| r.get(0))?;
        rows.collect()
    }

    fn pairs<T: rusqlite::types::FromSql>(&self, sql: &str, params: &[&str]) -> rusqlite::Result<Vec<(String, T)>> {
        let db = self.conn();
        let mut st = db.prepare(sql)?;
        let rows = st.query_map(params_from_iter(params), |r| Ok((r.get(0)?, r.get(1)?)))?;
        rows.collect()
    }

    fn execute(&self, sql: &str, params: &[&str]) -> rusqlite::Result<()> {
        self.conn().prepare_cached(sql)?.execute(params_from_iter(params)).map(|_| ())
    }

    /// The tags of `page`, sorted.
    pub fn tags_for_page(&self, page: &str) -> rusqlite::Result<Vec<String>> {
        self.column("SELECT tag FROM page_tags WHERE page = ? ORDER BY tag", &[page])
    }

    /// Every one of `pages`, with its tags sorted, in one query.
    pub fn tags_by_page(&self, pages: &[String]) -> rusqlite::Result<HashMap<String, Vec<String>>> {
        let mut out: HashMap<String, Vec<String>> = pages.iter().map(|p| (p.clone(), Vec::new())).collect();
        if pages.is_empty() {
            return Ok(out);
        }
        let marks = vec!["?"; pages.len()].join(",");
        let refs: Vec<&str> = pages.iter().map(String::as_str).collect();
        let rows: Vec<(String, String)> = self.pairs(&format!("SELECT page, tag FROM page_tags WHERE page IN ({marks}) ORDER BY page, tag"), &refs)?;
        for (page, tag) in rows {
            out.entry(page).or_default().push(tag);
        }
        Ok(out)
    }

    /// Every tag and the number of pages that carry it, sorted by tag.
    pub fn all_tag_counts(&self) -> rusqlite::Result<Vec<(String, i64)>> {
        self.pairs("SELECT tag, COUNT(*) AS n FROM page_tags GROUP BY tag ORDER BY tag", &[])
    }

    pub fn add_tag(&self, page: &str, tag: &str) -> rusqlite::Result<()> {
        let tag = normalize(tag);
        if page.is_empty() || tag.is_empty() {
            return Ok(());
        }
        self.execute("INSERT OR IGNORE INTO page_tags (page, tag) VALUES (?, ?)", &[page, &tag])
    }

    /// Removes `tag` from `page`, and from the selection once no page carries it.
    pub fn remove_tag(&self, page: &str, tag: &str) -> rusqlite::Result<()> {
        let tag = normalize(tag);
        self.execute("DELETE FROM page_tags WHERE page = ? AND tag = ?", &[page, &tag])?;
        self.execute("DELETE FROM selected_tags WHERE tag = ? AND tag NOT IN (SELECT DISTINCT tag FROM page_tags)", &[&tag])
    }

    /// The selected tags that some page still carries, sorted.
    pub fn selected_tags(&self) -> rusqlite::Result<Vec<String>> {
        self.column("SELECT tag FROM selected_tags WHERE tag IN (SELECT DISTINCT tag FROM page_tags) ORDER BY tag", &[])
    }

    pub fn select_tag(&self, tag: &str) -> rusqlite::Result<()> {
        let tag = normalize(tag);
        if tag.is_empty() {
            return Ok(());
        }
        self.execute("INSERT OR IGNORE INTO selected_tags (tag) VALUES (?)", &[&tag])
    }

    pub fn deselect_tag(&self, tag: &str) -> rusqlite::Result<()> {
        self.execute("DELETE FROM selected_tags WHERE tag = ?", &[&normalize(tag)])
    }

    pub fn display_name(&self, page: &str) -> rusqlite::Result<Option<String>> {
        self.conn().prepare_cached("SELECT display_name FROM page_names WHERE page = ?")?.query_row([page], |r| r.get(0)).optional()
    }

    /// The display-name overrides of `pages`, in one query, in SQLite's order.
    pub fn display_names(&self, pages: &[String]) -> rusqlite::Result<Vec<(String, String)>> {
        if pages.is_empty() {
            return Ok(Vec::new());
        }
        let marks = vec!["?"; pages.len()].join(",");
        let refs: Vec<&str> = pages.iter().map(String::as_str).collect();
        self.pairs(&format!("SELECT page, display_name FROM page_names WHERE page IN ({marks})"), &refs)
    }

    /// Sets the display name of `page`. A blank name is a no-op.
    pub fn set_display_name(&self, page: &str, name: &str) -> rusqlite::Result<()> {
        let name = normalize(name);
        if page.is_empty() || name.is_empty() {
            return Ok(());
        }
        self.execute(
            "INSERT INTO page_names (page, display_name) VALUES (?, ?) ON CONFLICT(page) DO UPDATE SET display_name = excluded.display_name",
            &[page, &name],
        )
    }

    pub fn clear_display_name(&self, page: &str) -> rusqlite::Result<()> {
        self.execute("DELETE FROM page_names WHERE page = ?", &[page])
    }
}

/// Creates a new store at the current shape, or migrates an older one, as `init_db_at` did.
fn init(db: &Connection) -> anyhow::Result<()> {
    let current: Option<i64> = db.query_row("SELECT version FROM schema_version", [], |r| r.get(0)).optional().unwrap_or(None);
    let Some(mut current) = current else {
        db.execute_batch(SCHEMA_SQL)?;
        db.execute_batch("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")?;
        let existing: Option<i64> = db.query_row("SELECT version FROM schema_version", [], |r| r.get(0)).optional()?;
        if existing.is_none() {
            db.execute("INSERT INTO schema_version (version) VALUES (?1)", [SCHEMA_VERSION])?;
        } else {
            db.execute("UPDATE schema_version SET version = ?1", [SCHEMA_VERSION])?;
        }
        return Ok(());
    };
    while current < SCHEMA_VERSION {
        let next = current + 1;
        let sql = MIGRATIONS
            .iter()
            .find(|(from, _)| *from == current)
            .map(|(_, sql)| *sql)
            .ok_or_else(|| anyhow::anyhow!("No migration path from v{current} to v{next}"))?;
        db.execute_batch(&format!(
            "PRAGMA foreign_keys=OFF;\nBEGIN;\n{sql}\nUPDATE schema_version SET version = {next};\nCOMMIT;\nPRAGMA foreign_key_check;\nPRAGMA foreign_keys=ON;"
        ))?;
        current = next;
    }
    Ok(())
}
