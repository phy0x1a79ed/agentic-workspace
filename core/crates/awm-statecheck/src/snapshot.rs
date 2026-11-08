//! The one copy of a plugin's pre-redux state, taken before its first Rust start.
//!
//! Each state dir gets `<dir>/.pre-rust/`: a `CACHEDIR.TAG`, `inventory.json`, the copy
//! under `state/`, and in the first dir the extras under `extra/<name>/` and the size_only
//! records. The copy is built in `.pre-rust.tmp/` and renamed only once every file is
//! fsync'd, so a cut-short copy is never taken for a whole one.

use std::fs::{self, DirBuilder, File};
use std::io;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::cachetag::{has_cache_tag, write_cache_tag};
use crate::decl::Decl;
use crate::inventory::{self, open_ro, Entry, ExtraInv, Inventory, Kind, Tree, PRE_RUST, PRE_RUST_TMP};

#[derive(Debug, thiserror::Error)]
pub enum SnapError {
    #[error("{path}: {free} bytes free, and the copy needs {need} (its size plus 10%)")]
    NoSpace { path: PathBuf, free: u64, need: u64 },
    #[error("{0}: holds files this crate did not write; move it aside by hand")]
    ForeignTmp(PathBuf),
    #[error("{path}: {source}")]
    Io { path: PathBuf, source: io::Error },
    #[error("{path}: VACUUM INTO failed: {source}")]
    Db { path: PathBuf, source: rusqlite::Error },
}

#[derive(Debug, Default, Serialize)]
pub struct Outcome {
    /// The snapshots this call took.
    pub taken: Vec<PathBuf>,
    /// The snapshots that already existed, left as they are.
    pub existing: Vec<PathBuf>,
    pub notes: Vec<String>,
}

fn io_at(path: &Path) -> impl FnOnce(io::Error) -> SnapError + '_ {
    move |source| SnapError::Io { path: path.to_path_buf(), source }
}

/// Free bytes for an unprivileged writer on the filesystem holding `path`.
pub fn free_bytes(path: &Path) -> io::Result<u64> {
    let c = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(st.f_bavail as u64 * st.f_frsize as u64)
}

/// Take each state dir's snapshot that does not exist yet. Never overwrites one.
pub fn snapshot_once(plugin: &str, decl: &Decl) -> Result<Outcome, SnapError> {
    snapshot_with(plugin, decl, &free_bytes, &mut |_| Ok(()))
}

fn copy_size(tree: &Tree) -> u64 {
    tree.entries.iter().filter(|e| e.kind == Kind::File && !e.db_side).map(|e| e.size).sum()
}

fn existing_ancestor(path: &Path) -> &Path {
    path.ancestors().find(|a| a.exists()).unwrap_or(Path::new("/"))
}

/// `snapshot_once` with the free-space probe and a hook run before each copied item, for tests.
pub(crate) fn snapshot_with(
    plugin: &str,
    decl: &Decl,
    avail: &dyn Fn(&Path) -> io::Result<u64>,
    hook: &mut dyn FnMut(&Path) -> io::Result<()>,
) -> Result<Outcome, SnapError> {
    let mut out = Outcome::default();
    let mut pending = Vec::new();
    for (i, dir) in decl.dirs.iter().enumerate() {
        let pre = dir.join(PRE_RUST);
        if pre.symlink_metadata().is_ok() {
            out.existing.push(pre);
        } else {
            pending.push((i, dir));
        }
    }
    if pending.is_empty() {
        return Ok(out);
    }
    let extras: Vec<ExtraInv> = decl
        .extra
        .iter()
        .map(|e| {
            let matches = inventory::expand_pattern(&e.path).iter().map(|m| inventory::tree(m, decl)).collect::<io::Result<Vec<_>>>()?;
            Ok(ExtraInv { name: e.name.clone(), pattern: e.path.clone(), matches: matches.into_iter().filter(|t| t.exists).collect() })
        })
        .collect::<io::Result<_>>()
        .map_err(io_at(Path::new("extra")))?;
    let mut plans = Vec::new();
    for (i, dir) in pending {
        let tree = inventory::tree(dir, decl).map_err(io_at(dir))?;
        let mut need = copy_size(&tree);
        if i == 0 {
            need += extras.iter().flat_map(|e| &e.matches).map(copy_size).sum::<u64>();
        }
        plans.push((i, dir, tree, need));
    }
    let mut by_fs: Vec<(u64, PathBuf, u64)> = Vec::new();
    for (_, dir, _, need) in &plans {
        let probe = existing_ancestor(dir);
        let dev = std::os::unix::fs::MetadataExt::dev(&probe.metadata().map_err(io_at(probe))?);
        match by_fs.iter_mut().find(|(d, ..)| *d == dev) {
            Some(slot) => slot.2 += need,
            None => by_fs.push((dev, probe.to_path_buf(), *need)),
        }
    }
    for (_, probe, need) in &by_fs {
        let free = avail(probe).map_err(io_at(probe))?;
        let need = need + need / 10;
        if free < need {
            return Err(SnapError::NoSpace { path: probe.clone(), free, need });
        }
    }
    let size_only: Vec<_> = decl.size_only.iter().flat_map(|p| inventory::expand_pattern(p)).map(|p| inventory::size_only(&p)).collect();
    for (i, dir, tree, _) in plans {
        let first = i == 0;
        let inv = Inventory {
            plugin: plugin.into(),
            taken_at: chrono::Utc::now().to_rfc3339(),
            dirs: vec![tree],
            size_only: if first { size_only.clone() } else { vec![] },
            extra: if first { extras.clone() } else { vec![] },
        };
        out.taken.push(snapshot_dir(dir, &inv, &mut out.notes, hook)?);
    }
    Ok(out)
}

fn mkdir(path: &Path, mode: u32) -> Result<(), SnapError> {
    DirBuilder::new().mode(mode).create(path).map_err(io_at(path))
}

fn sync_dir(path: &Path) -> Result<(), SnapError> {
    File::open(path).and_then(|f| f.sync_all()).map_err(io_at(path))
}

fn snapshot_dir(dir: &Path, inv: &Inventory, notes: &mut Vec<String>, hook: &mut dyn FnMut(&Path) -> io::Result<()>) -> Result<PathBuf, SnapError> {
    if !dir.exists() {
        DirBuilder::new().mode(0o700).recursive(true).create(dir).map_err(io_at(dir))?;
    }
    let tmp = dir.join(PRE_RUST_TMP);
    if tmp.symlink_metadata().is_ok() {
        let empty = fs::read_dir(&tmp).map(|mut d| d.next().is_none()).unwrap_or(false);
        if !has_cache_tag(&tmp) && !empty {
            return Err(SnapError::ForeignTmp(tmp));
        }
        fs::remove_dir_all(&tmp).map_err(io_at(&tmp))?;
    }
    mkdir(&tmp, 0o700)?;
    write_cache_tag(&tmp, "awm-statecheck").map_err(io_at(&tmp))?;
    sync_dir(&tmp)?;
    let state = tmp.join("state");
    mkdir(&state, 0o700)?;
    let mut dirs = vec![];
    copy_tree(&inv.dirs[0], &state, notes, hook, &mut dirs)?;
    for e in &inv.extra {
        let base = inventory::pattern_base(&e.pattern);
        for m in &e.matches {
            let rel = m.path.strip_prefix(&base).unwrap_or(m.path.file_name().map(Path::new).unwrap_or(Path::new("match")));
            let dest = tmp.join("extra").join(&e.name).join(rel);
            // A file match copies to `dest` itself. A directory match copies into it.
            let is_file_match = m.entries.iter().any(|e| e.rel.as_os_str().is_empty());
            let target = if is_file_match { dest.parent().expect("under tmp") } else { dest.as_path() };
            DirBuilder::new().mode(0o700).recursive(true).create(target).map_err(io_at(target))?;
            copy_tree(m, &dest, notes, hook, &mut dirs)?;
        }
    }
    let inv_path = tmp.join("inventory.json");
    let mut f = File::create(&inv_path).map_err(io_at(&inv_path))?;
    serde_json::to_writer_pretty(&mut f, inv).map_err(|e| SnapError::Io { path: inv_path.clone(), source: e.into() })?;
    f.sync_all().map_err(io_at(&inv_path))?;
    // Modes last and deepest first, so a read-only dir does not block its own copy.
    dirs.sort_by_key(|(p, _): &(PathBuf, u32)| std::cmp::Reverse(p.components().count()));
    for (p, mode) in &dirs {
        sync_dir(p)?;
        fs::set_permissions(p, fs::Permissions::from_mode(*mode)).map_err(io_at(p))?;
    }
    sync_dir(&state)?;
    sync_dir(&tmp)?;
    let pre = dir.join(PRE_RUST);
    fs::rename(&tmp, &pre).map_err(io_at(&pre))?;
    sync_dir(dir)?;
    Ok(pre)
}

/// Copy `tree` to `dest`: each database by `VACUUM INTO`, other files with their bytes,
/// symlinks as symlinks. Owners carry over where the process may set them.
fn copy_tree(
    tree: &Tree,
    dest: &Path,
    notes: &mut Vec<String>,
    hook: &mut dyn FnMut(&Path) -> io::Result<()>,
    dirs: &mut Vec<(PathBuf, u32)>,
) -> Result<(), SnapError> {
    for e in &tree.entries {
        let (src, to) = if e.rel.as_os_str().is_empty() { (tree.path.clone(), dest.to_path_buf()) } else { (tree.path.join(&e.rel), dest.join(&e.rel)) };
        hook(&src).map_err(io_at(&src))?;
        match e.kind {
            Kind::Dir => {
                mkdir(&to, 0o700)?;
                dirs.push((to.clone(), e.mode));
            }
            Kind::File if e.db_side => continue,
            Kind::File if e.db => vacuum_into(&src, &to, e)?,
            Kind::File => copy_file(&src, &to, e)?,
            Kind::Symlink => {
                let target = fs::read_link(&src).map_err(io_at(&src))?;
                std::os::unix::fs::symlink(&target, &to).map_err(io_at(&to))?;
            }
            Kind::Other => {
                notes.push(format!("{}: not a file, dir or symlink; recorded, not copied", src.display()));
                continue;
            }
        }
        own(&to, e, notes);
    }
    Ok(())
}

fn own(path: &Path, e: &Entry, notes: &mut Vec<String>) {
    if let Err(err) = std::os::unix::fs::lchown(path, Some(e.uid), Some(e.gid)) {
        if !notes.iter().any(|n| n.starts_with("owner")) {
            notes.push(format!("owner not kept ({err}); the copy belongs to this process's user, and inventory.json records each original owner"));
        }
    }
}

fn copy_file(src: &Path, to: &Path, e: &Entry) -> Result<(), SnapError> {
    fs::copy(src, to).map_err(io_at(src))?;
    fs::set_permissions(to, fs::Permissions::from_mode(e.mode)).map_err(io_at(to))?;
    File::open(to).and_then(|f| f.sync_all()).map_err(io_at(to))
}

fn vacuum_into(src: &Path, to: &Path, e: &Entry) -> Result<(), SnapError> {
    let db = |source| SnapError::Db { path: src.to_path_buf(), source };
    let c = open_ro(src).map_err(db)?;
    c.execute("VACUUM INTO ?1", [to.to_string_lossy()]).map_err(db)?;
    drop(c);
    fs::set_permissions(to, fs::Permissions::from_mode(e.mode)).map_err(io_at(to))?;
    File::open(to).and_then(|f| f.sync_all()).map_err(io_at(to))
}
