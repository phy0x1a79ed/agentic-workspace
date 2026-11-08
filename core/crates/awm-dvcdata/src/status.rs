//! Does the workspace match what the commit pins? The native `dvc status`.
//!
//! A materialised file is a hardlink to its cache object, so a shared inode counts as
//! unchanged with no hashing. Anything else is compared by size, then by md5.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::dvcignore::DvcIgnore;
use crate::odb::{self, Entry, Manifest, ManifestError};
use crate::pin::{self, Out, Pin, PinError, LOCK_NAME, PIN_SUFFIX};
use crate::scope::cache_dir_for;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub enum State {
    #[serde(rename = "modified")]
    Modified,
    #[serde(rename = "new")]
    New,
    #[serde(rename = "deleted")]
    Deleted,
    #[serde(rename = "not in cache")]
    NotInCache,
}

impl State {
    pub fn as_str(self) -> &'static str {
        match self {
            State::Modified => "modified",
            State::New => "new",
            State::Deleted => "deleted",
            State::NotInCache => "not in cache",
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum StatusError {
    #[error(transparent)]
    Pin(#[from] PinError),
    #[error(transparent)]
    Manifest(#[from] ManifestError),
    #[error("{path}: {source}")]
    Io { path: PathBuf, source: io::Error },
    #[error("no pin outputs {0}")]
    NoTarget(String),
}

pub(crate) fn io_at(path: &Path) -> impl FnOnce(io::Error) -> StatusError + '_ {
    move |source| StatusError::Io { path: path.to_path_buf(), source }
}

/// One DVC worktree: its root, its cache and its ignore rules.
pub struct Repo {
    pub root: PathBuf,
    pub cache: PathBuf,
    pub ignore: DvcIgnore,
}

impl Repo {
    pub fn open(root: &Path) -> Repo {
        Repo { root: root.to_path_buf(), cache: cache_dir_for(root), ignore: DvcIgnore::new(root) }
    }

    pub fn rel(&self, p: &Path) -> String {
        p.strip_prefix(&self.root).unwrap_or(p).to_string_lossy().into_owned()
    }
}

/// One output a stage names, with the file that names it.
#[derive(Debug, Clone)]
pub struct Target {
    /// The pin's path relative to the repo, or `<dir>/dvc.yaml:<stage>` for a lock stage.
    pub stage: String,
    pub file: PathBuf,
    pub out: Out,
    pub path: PathBuf,
}

/// Every tracked or untracked, unignored pin and lock file. Read from git, so no walk
/// enters a gitignored data tree.
pub fn stage_files(root: &Path) -> (Vec<PathBuf>, Vec<PathBuf>) {
    let listed = crate::git::ls_files(root, &["-c", "-o", "--exclude-standard"], &["*.dvc", "*dvc.lock"]);
    let files: Vec<PathBuf> = match listed {
        Some(v) => v.into_iter().map(|p| root.join(p)).collect(),
        None => crate::scope::find_pins(root),
    };
    let mut pins = vec![];
    let mut locks = vec![];
    for f in files {
        if !f.is_file() || f.strip_prefix(root).is_ok_and(|r| r.parent().is_some_and(|p| p.components().any(|c| c.as_os_str() == ".dvc"))) {
            continue;
        }
        let name = f.file_name().unwrap_or_default().to_string_lossy().into_owned();
        if name == LOCK_NAME {
            locks.push(f);
        } else if name.ends_with(PIN_SUFFIX) && name != PIN_SUFFIX {
            pins.push(f);
        }
    }
    pins.sort();
    pins.dedup();
    locks.sort();
    locks.dedup();
    (pins, locks)
}

fn pin_targets(repo: &Repo, file: &Path) -> Result<Vec<Target>, StatusError> {
    let p = Pin::load(file)?;
    Ok(p.outs.iter().map(|o| Target { stage: repo.rel(file), file: file.into(), path: p.out_path(o), out: o.clone() }).collect())
}

fn lock_targets(repo: &Repo, file: &Path) -> Result<Vec<Target>, StatusError> {
    let text = fs::read_to_string(file).map_err(io_at(file))?;
    let base = file.parent().unwrap_or(Path::new(""));
    let rel_dir = repo.rel(base);
    let mut out = vec![];
    for (stage, outs) in pin::lock_stages(&text, file)? {
        let key = if rel_dir.is_empty() { format!("dvc.yaml:{stage}") } else { format!("{rel_dir}/dvc.yaml:{stage}") };
        for o in outs {
            out.push(Target { stage: key.clone(), file: file.into(), path: pin::normalize(&base.join(&o.path)), out: o });
        }
    }
    Ok(out)
}

/// Every output of the repo, or those the targets name. A target is a pin, or an output
/// path, with or without its trailing slash.
pub fn targets(repo: &Repo, names: Option<&[String]>) -> Result<Vec<Target>, StatusError> {
    let (pins, locks) = stage_files(&repo.root);
    let mut all = None::<Vec<Target>>;
    let mut load_all = |repo: &Repo| -> Result<Vec<Target>, StatusError> {
        if all.is_none() {
            let mut v = vec![];
            for p in &pins {
                v.extend(pin_targets(repo, p)?);
            }
            for l in &locks {
                v.extend(lock_targets(repo, l)?);
            }
            all = Some(v);
        }
        Ok(all.clone().unwrap_or_default())
    };
    let Some(names) = names else { return load_all(repo) };
    let mut out: Vec<Target> = vec![];
    for name in names {
        let t = name.trim_end_matches('/');
        let path = pin::normalize(&repo.root.join(t));
        let as_pin = if t.ends_with(PIN_SUFFIX) { path.clone() } else { PathBuf::from(format!("{}{PIN_SUFFIX}", path.display())) };
        let found = if as_pin.is_file() {
            pin_targets(repo, &as_pin)?
        } else {
            load_all(repo)?.into_iter().filter(|x| x.path == path).collect()
        };
        if found.is_empty() {
            return Err(StatusError::NoTarget(name.clone()));
        }
        for f in found {
            if !out.iter().any(|o| o.file == f.file && o.path == f.path) {
                out.push(f);
            }
        }
    }
    Ok(out)
}

/// The md5 of the file at `path`, taken from `hint` without hashing when it shares the
/// object's inode.
pub fn file_md5(cache: &Path, path: &Path, hint: Option<&str>) -> io::Result<String> {
    if let Some(h) = hint {
        let meta = fs::metadata(path)?;
        if let Ok(obj) = fs::metadata(odb::object_path(cache, h)) {
            if obj.ino() == meta.ino() && obj.dev() == meta.dev() {
                return Ok(h.to_string());
            }
        }
    }
    odb::md5_file(path)
}

/// The files DVC sees under `dir`, as (relpath, path). Symlinked files are followed and
/// symlinked dirs are not, as DVC's walk does. `dir` itself may be a link.
pub fn walk_out(repo: &Repo, dir: &Path) -> io::Result<Vec<(String, PathBuf)>> {
    let mut out = vec![];
    let mut stack = vec![(String::new(), dir.to_path_buf())];
    while let Some((rel, d)) = stack.pop() {
        for e in fs::read_dir(&d)? {
            let e = e?;
            let abs = e.path();
            let name = e.file_name().to_string_lossy().into_owned();
            let child = if rel.is_empty() { name } else { format!("{rel}/{name}") };
            let ft = e.file_type()?;
            let (is_dir, is_file) = if ft.is_symlink() {
                match fs::metadata(&abs) {
                    Ok(m) => (false, m.is_file()),
                    Err(_) => (false, false),
                }
            } else {
                (ft.is_dir(), ft.is_file())
            };
            if repo.ignore.entry_ignored(&repo.root.join(dir.strip_prefix(&repo.root).unwrap_or(dir)).join(&child), is_dir) {
                continue;
            }
            if is_dir {
                stack.push((child, abs));
            } else if is_file {
                out.push((child, abs));
            }
        }
    }
    out.sort();
    Ok(out)
}

/// The manifest of the tree at `dir`, reusing `hint`'s ids for files that share an inode.
pub fn tree_of(repo: &Repo, dir: &Path, hint: Option<&Manifest>) -> io::Result<Manifest> {
    let known = hint.map(|m| m.by_path()).unwrap_or_default();
    let mut entries = vec![];
    for (rel, abs) in walk_out(repo, dir)? {
        let md5 = file_md5(&repo.cache, &abs, known.get(rel.as_str()).map(|e| e.md5.as_str()))?;
        entries.push(Entry::new(rel, md5));
    }
    Ok(Manifest::from_entries(entries))
}

/// Whether the cache lacks the output's object, or any leaf of its manifest.
pub fn changed_cache(cache: &Path, oid: &str) -> bool {
    if !odb::has_object(cache, oid) {
        return true;
    }
    if !oid.ends_with(odb::DIR_SUFFIX) {
        return false;
    }
    match Manifest::load(cache, oid) {
        Ok(m) => m.entries.iter().any(|e| !odb::has_object(cache, &e.md5)),
        Err(_) => true,
    }
}

/// One output's state, or `None` when the workspace matches its pin.
pub fn out_status(repo: &Repo, t: &Target) -> Result<Option<State>, StatusError> {
    t.out.require_md5(&t.file)?;
    if let Some(oid) = &t.out.md5 {
        if changed_cache(&repo.cache, oid) {
            return Ok(Some(State::NotInCache));
        }
    }
    let Ok(meta) = fs::metadata(&t.path) else { return Ok(Some(State::Deleted)) };
    let Some(oid) = &t.out.md5 else { return Ok(Some(State::Modified)) };
    let same = if t.out.is_dir() {
        meta.is_dir() && {
            let pinned = Manifest::load(&repo.cache, oid)?;
            tree_of(repo, &t.path, Some(&pinned)).map_err(io_at(&t.path))?.oid() == *oid
        }
    } else {
        meta.is_file() && file_md5(&repo.cache, &t.path, Some(oid)).map_err(io_at(&t.path))? == *oid
    };
    Ok((!same).then_some(State::Modified))
}

/// Every output that differs from its pin, by stage then output path: the
/// `changed outs` of `dvc status --json`.
pub fn changed_outs(root: &Path, names: Option<&[String]>) -> Result<BTreeMap<String, BTreeMap<String, State>>, StatusError> {
    let repo = Repo::open(root);
    let mut out: BTreeMap<String, BTreeMap<String, State>> = BTreeMap::new();
    for t in targets(&repo, names)? {
        if let Some(s) = out_status(&repo, &t)? {
            out.entry(t.stage.clone()).or_default().insert(repo.rel(&t.path), s);
        }
    }
    Ok(out)
}

/// Whether every output the targets name matches its pin.
pub fn in_sync(root: &Path, names: Option<&[String]>) -> Result<bool, StatusError> {
    Ok(changed_outs(root, names)?.is_empty())
}
