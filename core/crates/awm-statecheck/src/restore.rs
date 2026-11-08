//! The two actions that change a snapshot's dir: restore a damaged store, and prune the
//! snapshot once Tony accepts the port.
//!
//! **CAUTION** The rollback for a land is to revert the code and keep the live state.
//! A wholesale restore of a security store is unsafe: it revives revoked tokens, rewinds
//! a device chain or replays orders. Restore only a store that `check` proves damaged.

use std::fs::{self, DirBuilder};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

use crate::cachetag::{has_cache_tag, write_cache_tag};
use crate::decl::Decl;
use crate::inventory::{PRE_RUST, RUST_ASIDE_PREFIX};

pub const ROLLBACK_RULE: &str = "The rollback rule is to revert the code and keep the live state. \
Restore a store only when `state check` proves it damaged, with the plugin stopped.";

#[derive(Debug, thiserror::Error)]
pub enum RestoreError {
    #[error("restore runs only with --damage-proven. {ROLLBACK_RULE}")]
    NotProven,
    #[error("name each store to restore, as a path in a state dir")]
    NoStores,
    #[error("{0}: no snapshot copy of it in any state dir")]
    NotInSnapshot(PathBuf),
    #[error("{0}: more than one state dir holds a copy; name it by its full path")]
    Ambiguous(PathBuf),
    #[error("prune needs Tony's OK as --tony-ok YYYY-MM-DD, not {0:?}")]
    BadDate(String),
    #[error("Tony's OK of {0} lies in the future")]
    FutureDate(String),
    #[error("{0}: not a tagged snapshot; left as it is")]
    Untagged(PathBuf),
    #[error("{path}: {source}")]
    Io { path: PathBuf, source: std::io::Error },
}

fn io_at(path: &Path) -> impl FnOnce(std::io::Error) -> RestoreError + '_ {
    move |source| RestoreError::Io { path: path.to_path_buf(), source }
}

/// Which state dir holds `store`, and its path relative to that dir.
fn locate(decl: &Decl, store: &Path) -> Result<(PathBuf, PathBuf), RestoreError> {
    let hits: Vec<(PathBuf, PathBuf)> = decl
        .dirs
        .iter()
        .filter_map(|d| {
            let rel = if store.is_absolute() { store.strip_prefix(d).ok()?.to_path_buf() } else { store.to_path_buf() };
            d.join(PRE_RUST).join("state").join(&rel).symlink_metadata().is_ok().then(|| (d.clone(), rel))
        })
        .collect();
    match hits.len() {
        0 => Err(RestoreError::NotInSnapshot(store.to_path_buf())),
        1 => Ok(hits.into_iter().next().expect("one")),
        _ => Err(RestoreError::Ambiguous(store.to_path_buf())),
    }
}

/// Put each named store back from `.pre-rust/`. The live copy moves aside to a tagged
/// `<dir>/.rust-<ts>/`, with a database's `-wal`, `-shm` and `-journal`. Nothing is deleted.
pub fn restore(decl: &Decl, stores: &[PathBuf], damage_proven: bool) -> Result<Vec<String>, RestoreError> {
    if !damage_proven {
        return Err(RestoreError::NotProven);
    }
    if stores.is_empty() {
        return Err(RestoreError::NoStores);
    }
    let located = stores.iter().map(|s| locate(decl, s)).collect::<Result<Vec<_>, _>>()?;
    let ts = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let mut done = vec![ROLLBACK_RULE.to_string()];
    for (dir, rel) in located {
        let aside = dir.join(format!("{RUST_ASIDE_PREFIX}{ts}"));
        if !aside.exists() {
            DirBuilder::new().mode(0o700).create(&aside).map_err(io_at(&aside))?;
            write_cache_tag(&aside, "awm-statecheck restore").map_err(io_at(&aside))?;
        }
        let live = dir.join(&rel);
        let mut moved = vec![];
        for suffix in ["", "-wal", "-shm", "-journal"] {
            let from = PathBuf::from(format!("{}{suffix}", live.display()));
            if from.symlink_metadata().is_err() {
                continue;
            }
            let to = PathBuf::from(format!("{}{suffix}", aside.join(&rel).display()));
            let parent = to.parent().expect("under aside");
            DirBuilder::new().mode(0o700).recursive(true).create(parent).map_err(io_at(parent))?;
            fs::rename(&from, &to).map_err(io_at(&from))?;
            moved.push(to);
        }
        copy_back(&dir.join(PRE_RUST).join("state").join(&rel), &live)?;
        let aside_note = if moved.is_empty() { "no live copy to move aside".to_string() } else { format!("live copy moved to {}", aside.display()) };
        done.push(format!("{}: restored from {PRE_RUST}/; {aside_note}", live.display()));
    }
    Ok(done)
}

fn copy_back(src: &Path, to: &Path) -> Result<(), RestoreError> {
    let meta = src.symlink_metadata().map_err(io_at(src))?;
    if meta.file_type().is_symlink() {
        std::os::unix::fs::symlink(fs::read_link(src).map_err(io_at(src))?, to).map_err(io_at(to))?;
    } else if meta.is_dir() {
        DirBuilder::new().mode(0o700).create(to).map_err(io_at(to))?;
        for d in fs::read_dir(src).map_err(io_at(src))? {
            let d = d.map_err(io_at(src))?;
            copy_back(&d.path(), &to.join(d.file_name()))?;
        }
        fs::set_permissions(to, fs::Permissions::from_mode(meta.mode() & 0o7777)).map_err(io_at(to))?;
    } else {
        if let Some(parent) = to.parent() {
            DirBuilder::new().mode(0o700).recursive(true).create(parent).map_err(io_at(parent))?;
        }
        fs::copy(src, to).map_err(io_at(src))?;
        fs::File::open(to).and_then(|f| f.sync_all()).map_err(io_at(to))?;
    }
    let _ = std::os::unix::fs::lchown(to, Some(meta.uid()), Some(meta.gid()));
    Ok(())
}

/// Delete each state dir's `.pre-rust/`, on Tony's OK dated `ok_date` (YYYY-MM-DD, not after `today`).
pub fn prune(decl: &Decl, ok_date: &str, today: chrono::NaiveDate) -> Result<Vec<PathBuf>, RestoreError> {
    let date = chrono::NaiveDate::parse_from_str(ok_date, "%Y-%m-%d").map_err(|_| RestoreError::BadDate(ok_date.into()))?;
    if date > today {
        return Err(RestoreError::FutureDate(ok_date.into()));
    }
    let pres: Vec<PathBuf> = decl.dirs.iter().map(|d| d.join(PRE_RUST)).filter(|p| p.is_dir()).collect();
    if let Some(p) = pres.iter().find(|p| !has_cache_tag(p)) {
        return Err(RestoreError::Untagged(p.clone()));
    }
    for p in &pres {
        writable(p)?;
        fs::remove_dir_all(p).map_err(io_at(p))?;
    }
    Ok(pres)
}

/// Give the owner write on every dir under `p`, so a copied read-only dir can be removed.
fn writable(p: &Path) -> Result<(), RestoreError> {
    let meta = p.symlink_metadata().map_err(io_at(p))?;
    if meta.is_dir() {
        fs::set_permissions(p, fs::Permissions::from_mode((meta.mode() & 0o7777) | 0o700)).map_err(io_at(p))?;
        for d in fs::read_dir(p).map_err(io_at(p))? {
            writable(&d.map_err(io_at(p))?.path())?;
        }
    }
    Ok(())
}
