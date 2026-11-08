//! Compare the live state against `.pre-rust/`: every table, column, row key and file the
//! snapshot holds must still be there, unless it is declared transient. A report names
//! counts and paths, never a key or a value.

use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::decl::Decl;
use crate::inventory::{self, columns, open_ro, quote, table_names, Inventory, Kind, Tree, PRE_RUST};

#[derive(Debug, Default, Serialize)]
pub struct Report {
    /// What the live state lost. Empty means the check passes.
    pub lost: Vec<String>,
    /// What changed but passes: a new table or column not declared additive, an uncountable table.
    pub notes: Vec<String>,
}

impl Report {
    pub fn ok(&self) -> bool {
        self.lost.is_empty()
    }
}

pub fn read_inventory(dir: &Path) -> anyhow::Result<Inventory> {
    let p = dir.join(PRE_RUST).join("inventory.json");
    let text = std::fs::read_to_string(&p).map_err(|e| anyhow::anyhow!("{}: {e}", p.display()))?;
    Ok(serde_json::from_str(&text)?)
}

/// Check every state dir of `decl` against its snapshot.
pub fn check(decl: &Decl) -> anyhow::Result<Report> {
    let mut r = Report::default();
    for dir in &decl.dirs {
        let pre = dir.join(PRE_RUST);
        if !pre.is_dir() {
            r.lost.push(format!("{}: no {PRE_RUST}/ snapshot to check against", dir.display()));
            continue;
        }
        let inv = read_inventory(dir)?;
        let tree = inv.dirs.first().ok_or_else(|| anyhow::anyhow!("{}: inventory.json lists no dir", pre.display()))?;
        check_tree(decl, tree, &pre.join("state"), &mut r);
        for e in &inv.extra {
            let base = inventory::pattern_base(&e.pattern);
            for m in &e.matches {
                let rel = m.path.strip_prefix(&base).unwrap_or(Path::new(""));
                check_tree(decl, m, &pre.join("extra").join(&e.name).join(rel), &mut r);
            }
        }
        for s in &inv.size_only {
            if s.exists && s.path.symlink_metadata().is_err() && !decl.is_transient_file(&s.path) {
                r.lost.push(format!("{}: size_only path is gone", s.path.display()));
            }
        }
    }
    Ok(r)
}

fn check_tree(decl: &Decl, tree: &Tree, copy: &Path, r: &mut Report) {
    for e in tree.entries.iter().filter(|e| !e.db_side) {
        let live = if e.rel.as_os_str().is_empty() { tree.path.clone() } else { tree.path.join(&e.rel) };
        if decl.is_transient_file(&live) {
            continue;
        }
        let Ok(meta) = live.symlink_metadata() else {
            r.lost.push(format!("{}: {} is gone", live.display(), kind_name(e.kind)));
            continue;
        };
        let now = if meta.file_type().is_symlink() {
            Kind::Symlink
        } else if meta.is_dir() {
            Kind::Dir
        } else if meta.is_file() {
            Kind::File
        } else {
            Kind::Other
        };
        if now != e.kind {
            r.lost.push(format!("{}: was a {}, is now a {}", live.display(), kind_name(e.kind), kind_name(now)));
            continue;
        }
        if e.db {
            let snap = if e.rel.as_os_str().is_empty() { copy.to_path_buf() } else { copy.join(&e.rel) };
            if let Err(err) = compare_db(decl, &live, &snap, r) {
                r.lost.push(format!("{}: cannot compare with the snapshot: {err}", live.display()));
            }
        }
    }
}

fn kind_name(k: Kind) -> &'static str {
    match k {
        Kind::File => "file",
        Kind::Dir => "dir",
        Kind::Symlink => "symlink",
        Kind::Other => "special file",
    }
}

fn additive(decl: &Decl, db: &Path, table: &str, column: Option<&str>) -> bool {
    decl.additive.iter().any(|a| a.path == db && a.table.as_deref() == Some(table) && (a.column.is_none() || a.column.as_deref() == column))
}

fn compare_db(decl: &Decl, live: &Path, snap: &Path, r: &mut Report) -> rusqlite::Result<()> {
    let c = open_ro(live)?;
    c.execute("ATTACH DATABASE ?1 AS snap", [snap.to_string_lossy()])?;
    let live_tables = table_names(&c, "main")?;
    let snap_tables = table_names(&c, "snap")?;
    let at = |what: String| format!("{}: {what}", live.display());
    for t in live_tables.iter().filter(|t| !snap_tables.contains(t)) {
        if !additive(decl, live, t, None) {
            r.notes.push(at(format!("table {t} is new and not declared additive")));
        }
    }
    for t in &snap_tables {
        if decl.is_transient_table(live, t) {
            continue;
        }
        if !live_tables.contains(t) {
            r.lost.push(at(format!("table {t} is gone")));
            continue;
        }
        let snap_cols = columns(&c, "snap", t)?;
        let live_cols: Vec<String> = columns(&c, "main", t)?.into_iter().map(|(n, _)| n).collect();
        let mut missing_col = false;
        for (col, _) in &snap_cols {
            if !live_cols.contains(col) {
                r.lost.push(at(format!("column {t}.{col} is gone")));
                missing_col = true;
            }
        }
        for col in live_cols.iter().filter(|c| !snap_cols.iter().any(|(s, _)| s == *c)) {
            if !additive(decl, live, t, Some(col)) {
                r.notes.push(at(format!("column {t}.{col} is new and not declared additive")));
            }
        }
        let mut key: Vec<(String, i64)> = snap_cols.into_iter().filter(|(_, pk)| *pk > 0).collect();
        key.sort_by_key(|(_, pk)| *pk);
        let count = |schema: &str| c.query_row(&format!("SELECT count(*) FROM {schema}.{}", quote(t)), [], |row| row.get::<_, i64>(0));
        if key.is_empty() || missing_col {
            // VACUUM may renumber the rowids of a table with no declared key, so only the count can compare.
            match (count("snap"), count("main")) {
                (Ok(before), Ok(now)) if now < before => r.lost.push(at(format!("table {t} fell from {before} to {now} rows"))),
                (Ok(_), Ok(_)) => {}
                (Err(e), _) | (_, Err(e)) => r.notes.push(at(format!("table {t} cannot be counted here: {e}"))),
            }
            continue;
        }
        let on: Vec<String> = key.iter().map(|(k, _)| format!("l.{0} IS s.{0}", quote(k))).collect();
        let sql = format!("SELECT count(*) FROM snap.{0} s WHERE NOT EXISTS (SELECT 1 FROM main.{0} l WHERE {1})", quote(t), on.join(" AND "));
        match c.query_row(&sql, [], |row| row.get::<_, i64>(0)) {
            Ok(0) => {}
            Ok(n) => r.lost.push(at(format!("table {t} lost {n} rows by key"))),
            Err(e) => r.notes.push(at(format!("table {t} cannot be compared here: {e}"))),
        }
    }
    Ok(())
}

/// The state dirs of `decl` whose snapshot is missing.
pub fn unsnapshotted(decl: &Decl) -> Vec<PathBuf> {
    decl.dirs.iter().filter(|d| !d.join(PRE_RUST).is_dir()).cloned().collect()
}
