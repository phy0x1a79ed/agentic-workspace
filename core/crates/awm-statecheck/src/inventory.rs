//! What a plugin's state holds: paths, kinds, modes, owners, sizes, and per database its
//! tables, columns and row counts. Never a cell value or a file's bytes.

use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};

use crate::decl::Decl;

/// The names this crate writes into a state dir. A walk never enters them.
pub const PRE_RUST: &str = ".pre-rust";
pub const PRE_RUST_TMP: &str = ".pre-rust.tmp";
pub const RUST_ASIDE_PREFIX: &str = ".rust-";
const SQLITE_HEADER: &[u8; 16] = b"SQLite format 3\0";
const DB_SIDE_FILES: [&str; 3] = ["-wal", "-shm", "-journal"];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    File,
    Dir,
    Symlink,
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Entry {
    /// The path relative to its root. Empty for a root that is a file.
    pub rel: PathBuf,
    pub kind: Kind,
    pub mode: u32,
    pub uid: u32,
    pub gid: u32,
    pub size: u64,
    pub mtime: i64,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub db: bool,
    /// A `-wal`, `-shm` or `-journal` file beside a database.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub db_side: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Table {
    pub name: String,
    pub columns: Vec<String>,
    /// `None` when the table cannot be counted here, such as a virtual table whose module is missing.
    pub rows: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Db {
    pub rel: PathBuf,
    pub tables: Vec<Table>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// One root: a state dir, or one match of an `extra` pattern.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Tree {
    pub path: PathBuf,
    pub exists: bool,
    pub entries: Vec<Entry>,
    pub dbs: Vec<Db>,
    /// Entries left out because they are declared size_only. Each one is also in `size_only`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub skipped: Vec<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SizeOnly {
    pub path: PathBuf,
    pub exists: bool,
    pub size: u64,
    pub files: u64,
    pub mtime: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExtraInv {
    pub name: String,
    pub pattern: PathBuf,
    /// Each match, with `path` the match itself.
    pub matches: Vec<Tree>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Inventory {
    pub plugin: String,
    pub taken_at: String,
    pub dirs: Vec<Tree>,
    pub size_only: Vec<SizeOnly>,
    pub extra: Vec<ExtraInv>,
}

/// Whether a walk skips `name`: this crate's own copies.
pub(crate) fn ours(name: &std::ffi::OsStr) -> bool {
    let n = name.to_string_lossy();
    n == PRE_RUST || n == PRE_RUST_TMP || n.starts_with(RUST_ASIDE_PREFIX)
}

pub(crate) fn is_db(path: &Path) -> bool {
    let mut head = [0u8; 16];
    std::fs::File::open(path).and_then(|mut f| std::io::Read::read_exact(&mut f, &mut head)).is_ok() && &head == SQLITE_HEADER
}

/// The database a side file belongs to, when `path` is one.
pub(crate) fn side_of(path: &Path) -> Option<PathBuf> {
    let s = path.to_string_lossy();
    DB_SIDE_FILES.iter().find_map(|suf| s.strip_suffix(suf)).map(PathBuf::from).filter(|db| is_db(db))
}

fn entry(rel: PathBuf, meta: &std::fs::Metadata) -> Entry {
    let ft = meta.file_type();
    let kind = if ft.is_symlink() {
        Kind::Symlink
    } else if ft.is_dir() {
        Kind::Dir
    } else if ft.is_file() {
        Kind::File
    } else {
        Kind::Other
    };
    Entry { rel, kind, mode: meta.mode() & 0o7777, uid: meta.uid(), gid: meta.gid(), size: meta.len(), mtime: meta.mtime(), db: false, db_side: false }
}

/// Walk `root` without following symlinks. `skip` gets each absolute path.
pub(crate) fn walk(root: &Path, skip: &dyn Fn(&Path) -> bool) -> std::io::Result<Vec<Entry>> {
    let mut out = Vec::new();
    let meta = match root.symlink_metadata() {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(out),
        Err(e) => return Err(e),
    };
    if !meta.is_dir() {
        let mut e = entry(PathBuf::new(), &meta);
        e.db = meta.is_file() && is_db(root);
        e.db_side = meta.is_file() && side_of(root).is_some();
        out.push(e);
        return Ok(out);
    }
    let mut stack = vec![PathBuf::new()];
    while let Some(rel) = stack.pop() {
        let mut names: Vec<_> = std::fs::read_dir(root.join(&rel))?.collect::<Result<_, _>>()?;
        names.sort_by_key(|d| d.file_name());
        for d in names {
            if ours(&d.file_name()) {
                continue;
            }
            let abs = d.path();
            if skip(&abs) {
                continue;
            }
            let child = rel.join(d.file_name());
            let meta = abs.symlink_metadata()?;
            let mut e = entry(child.clone(), &meta);
            if meta.is_file() {
                e.db = is_db(&abs);
                e.db_side = !e.db && side_of(&abs).is_some();
            }
            out.push(e);
            if meta.is_dir() {
                stack.push(child);
            }
        }
    }
    out.sort_by(|a, b| a.rel.cmp(&b.rel));
    Ok(out)
}

pub(crate) fn quote(ident: &str) -> String {
    format!("\"{}\"", ident.replace('"', "\"\""))
}

pub(crate) fn open_ro(path: &Path) -> rusqlite::Result<Connection> {
    let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    c.busy_timeout(std::time::Duration::from_secs(10))?;
    Ok(c)
}

pub(crate) fn table_names(c: &Connection, schema: &str) -> rusqlite::Result<Vec<String>> {
    let sql = format!("SELECT name FROM {}.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name", quote(schema));
    let mut st = c.prepare(&sql)?;
    let names = st.query_map([], |r| r.get(0))?.collect();
    names
}

/// Each column of `table` with whether it is part of the primary key, in key order.
pub(crate) fn columns(c: &Connection, schema: &str, table: &str) -> rusqlite::Result<Vec<(String, i64)>> {
    let mut st = c.prepare(&format!("PRAGMA {}.table_info({})", quote(schema), quote(table)))?;
    let cols = st.query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, i64>(5)?)))?.collect();
    cols
}

/// The tables, columns and row counts of the database at `path`.
pub fn db_inventory(path: &Path) -> Result<Vec<Table>, String> {
    let c = open_ro(path).map_err(|e| e.to_string())?;
    let mut out = Vec::new();
    for name in table_names(&c, "main").map_err(|e| e.to_string())? {
        let cols = columns(&c, "main", &name).map(|v| v.into_iter().map(|(n, _)| n).collect()).unwrap_or_default();
        let rows = c.query_row(&format!("SELECT count(*) FROM main.{}", quote(&name)), [], |r| r.get::<_, i64>(0)).ok().map(|n| n as u64);
        out.push(Table { name, columns: cols, rows });
    }
    Ok(out)
}

pub(crate) fn tree(root: &Path, decl: &Decl) -> std::io::Result<Tree> {
    let exists = root.symlink_metadata().is_ok();
    let others: Vec<&PathBuf> = decl.dirs.iter().filter(|d| d.as_path() != root && d.starts_with(root)).collect();
    let mut skipped = Vec::new();
    let skip = |p: &Path| others.iter().any(|o| o.as_path() == p) || decl.is_size_only(p);
    let entries = walk(root, &skip)?;
    if exists && root.is_dir() {
        collect_skipped(root, decl, &mut skipped)?;
    }
    let dbs = entries
        .iter()
        .filter(|e| e.db)
        .map(|e| {
            let abs = if e.rel.as_os_str().is_empty() { root.to_path_buf() } else { root.join(&e.rel) };
            match db_inventory(&abs) {
                Ok(tables) => Db { rel: e.rel.clone(), tables, error: None },
                Err(err) => Db { rel: e.rel.clone(), tables: vec![], error: Some(err) },
            }
        })
        .collect();
    Ok(Tree { path: root.to_path_buf(), exists, entries, dbs, skipped })
}

fn collect_skipped(root: &Path, decl: &Decl, out: &mut Vec<PathBuf>) -> std::io::Result<()> {
    for s in &decl.size_only {
        for m in expand_pattern(s) {
            if m.starts_with(root) {
                out.push(m);
            }
        }
    }
    out.sort();
    Ok(())
}

/// The paths a pattern names now: itself when it holds no glob, else every match.
pub(crate) fn expand_pattern(pattern: &Path) -> Vec<PathBuf> {
    let s = pattern.to_string_lossy();
    if !s.contains(['*', '?', '[']) {
        return vec![pattern.to_path_buf()];
    }
    glob::glob(&s).map(|g| g.filter_map(Result::ok).collect()).unwrap_or_default()
}

/// The directory a pattern's matches are named relative to: its longest literal prefix.
pub(crate) fn pattern_base(pattern: &Path) -> PathBuf {
    let mut base = PathBuf::new();
    for c in pattern.components() {
        if c.as_os_str().to_string_lossy().contains(['*', '?', '[']) {
            return base;
        }
        base.push(c);
    }
    base.parent().map(Path::to_path_buf).unwrap_or(base)
}

pub(crate) fn size_only(path: &Path) -> SizeOnly {
    let mut rec = SizeOnly { path: path.to_path_buf(), exists: false, size: 0, files: 0, mtime: 0 };
    if let Ok(entries) = walk(path, &|_| false) {
        rec.exists = !entries.is_empty() || path.symlink_metadata().is_ok();
        for e in entries {
            rec.size += if e.kind == Kind::File { e.size } else { 0 };
            rec.files += u64::from(e.kind == Kind::File);
            rec.mtime = rec.mtime.max(e.mtime);
        }
    }
    rec
}

/// The live state as `decl` names it. Reads only.
pub fn inventory(plugin: &str, decl: &Decl) -> std::io::Result<Inventory> {
    let dirs = decl.dirs.iter().map(|d| tree(d, decl)).collect::<std::io::Result<_>>()?;
    let size_only = decl.size_only.iter().flat_map(|p| expand_pattern(p)).map(|p| self::size_only(&p)).collect();
    let mut extra = Vec::new();
    for e in &decl.extra {
        let matches = expand_pattern(&e.path).iter().map(|m| tree(m, decl)).collect::<std::io::Result<Vec<_>>>()?;
        extra.push(ExtraInv { name: e.name.clone(), pattern: e.path.clone(), matches: matches.into_iter().filter(|t| t.exists).collect() });
    }
    Ok(Inventory { plugin: plugin.into(), taken_at: chrono::Utc::now().to_rfc3339(), dirs, size_only, extra })
}
