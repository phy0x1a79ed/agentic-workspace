//! Materialise what a commit pins: the native `dvc checkout`.
//!
//! **CAUTION** A checkout that diverges from DVC can delete data. Three rules guard it:
//! 1. Each output is planned in full before anything on disk changes. A missing cache
//!    object fails the output with its old files still in place, where DVC removes them
//!    first and then fails.
//! 2. A file is removed or replaced only when its content is already in the cache.
//! 3. A link at an output path is never written through. It may point at a shared copy
//!    that other scopes read.

use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::odb::{self, Manifest};
use crate::scope::cache_types;
use crate::status::{self, file_md5, Repo, StatusError, Target};

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Unchanged,
    Updated { linked: usize, removed: usize },
    Failed(String),
}

#[derive(Debug, Clone, Serialize)]
pub struct OutReport {
    pub stage: String,
    pub path: String,
    pub outcome: Outcome,
}

#[derive(Debug, Default, Clone, Serialize)]
pub struct Report {
    pub outs: Vec<OutReport>,
}

impl Report {
    pub fn ok(&self) -> bool {
        !self.outs.iter().any(|o| matches!(o.outcome, Outcome::Failed(_)))
    }

    /// Every failure, one per line: the text the hook's sentinel records.
    pub fn failures(&self) -> String {
        self.outs
            .iter()
            .filter_map(|o| match &o.outcome {
                Outcome::Failed(why) => Some(format!("{}: {why}", o.path)),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// Check out every output the targets name, or every output of the repo.
pub fn checkout(root: &Path, names: Option<&[String]>) -> Result<Report, StatusError> {
    let repo = Repo::open(root);
    let types = cache_types(root);
    let mut report = Report::default();
    for t in status::targets(&repo, names)? {
        let outcome = match checkout_one(&repo, &t, &types) {
            Ok(o) => o,
            Err(why) => Outcome::Failed(why),
        };
        report.outs.push(OutReport { stage: t.stage.clone(), path: repo.rel(&t.path), outcome });
    }
    Ok(report)
}

struct Plan {
    remove: Vec<PathBuf>,
    link: Vec<(PathBuf, String)>,
    /// The output path holds a file where the pin names a directory.
    clear_root: bool,
}

fn checkout_one(repo: &Repo, t: &Target, types: &[String]) -> Result<Outcome, String> {
    t.out.require_md5(&t.file).map_err(|e| e.to_string())?;
    let Some(oid) = t.out.md5.clone() else { return Err("the pin names no hash".into()) };
    if !odb::has_object(&repo.cache, &oid) {
        return Err(format!("cache object {oid} is missing. Is your cache up to date?"));
    }
    let plan = if t.out.is_dir() { plan_dir(repo, t, &oid)? } else { plan_file(repo, t, &oid)? };
    let Some(plan) = plan else { return Ok(Outcome::Unchanged) };
    apply(repo, &t.path, plan, types, t.out.is_dir())
}

fn must_be_saved(repo: &Repo, path: &Path, md5: &str, unsaved: &mut Vec<String>) {
    if !odb::has_object(&repo.cache, md5) {
        unsaved.push(repo.rel(path));
    }
}

fn unsaved_error(unsaved: &[String]) -> String {
    let shown: Vec<&str> = unsaved.iter().take(5).map(String::as_str).collect();
    format!(
        "{} file(s) hold content that is in no cache, so the checkout would lose it: {}. Save them with `dvc add`, or move them aside.",
        unsaved.len(),
        shown.join(", ")
    )
}

fn plan_file(repo: &Repo, t: &Target, oid: &str) -> Result<Option<Plan>, String> {
    let path = &t.path;
    let link = Plan { remove: vec![], link: vec![(path.clone(), oid.to_string())], clear_root: false };
    let Ok(meta) = fs::symlink_metadata(path) else { return Ok(Some(link)) };
    if meta.is_dir() {
        return Err("is a directory where the pin names a file; move it aside".into());
    }
    let Ok(target) = fs::metadata(path) else { return Ok(Some(link)) };
    if !target.is_file() {
        return Err("links to a directory where the pin names a file; move it aside".into());
    }
    let md5 = file_md5(&repo.cache, path, Some(oid)).map_err(|e| e.to_string())?;
    if md5 == oid {
        return Ok(None);
    }
    let mut unsaved = vec![];
    must_be_saved(repo, path, &md5, &mut unsaved);
    if !unsaved.is_empty() {
        return Err(unsaved_error(&unsaved));
    }
    Ok(Some(link))
}

fn plan_dir(repo: &Repo, t: &Target, oid: &str) -> Result<Option<Plan>, String> {
    let path = &t.path;
    let want = Manifest::load(&repo.cache, oid).map_err(|e| e.to_string())?;
    let missing: Vec<&str> = want.entries.iter().filter(|e| !odb::has_object(&repo.cache, &e.md5)).map(|e| e.md5.as_str()).collect();
    if !missing.is_empty() {
        return Err(format!("{} cache objects are missing, e.g. {}. Is your cache up to date?", missing.len(), missing[0]));
    }
    let mut plan = Plan { remove: vec![], link: vec![], clear_root: false };
    let mut unsaved = vec![];
    let mut current = std::collections::HashSet::new();
    match fs::symlink_metadata(path) {
        Err(_) => {}
        Ok(meta) if meta.file_type().is_symlink() => {
            let same = fs::metadata(path).is_ok_and(|m| m.is_dir())
                && status::tree_of(repo, path, Some(&want)).is_ok_and(|m| m.oid() == oid);
            if same {
                return Ok(None);
            }
            return Err("is a link to another copy, and a checkout never writes through it. Run 'scope heal'".into());
        }
        Ok(meta) if !meta.is_dir() => {
            let md5 = file_md5(&repo.cache, path, None).map_err(|e| e.to_string())?;
            must_be_saved(repo, path, &md5, &mut unsaved);
            plan.clear_root = true;
        }
        Ok(_) => {
            let wanted = want.by_path();
            for (rel, abs) in status::walk_out(repo, path).map_err(|e| e.to_string())? {
                let hint = wanted.get(rel.as_str()).map(|e| e.md5.as_str());
                let md5 = file_md5(&repo.cache, &abs, hint).map_err(|e| e.to_string())?;
                if hint == Some(md5.as_str()) {
                    current.insert(rel);
                    continue;
                }
                must_be_saved(repo, &abs, &md5, &mut unsaved);
                if hint.is_none() {
                    plan.remove.push(abs);
                }
            }
        }
    }
    if !unsaved.is_empty() {
        return Err(unsaved_error(&unsaved));
    }
    for e in &want.entries {
        let dest = path.join(&e.relpath);
        if !current.contains(&e.relpath) {
            plan.link.push((dest, e.md5.clone()));
        }
    }
    if plan.remove.is_empty() && plan.link.is_empty() && !plan.clear_root && path.is_dir() {
        return Ok(None);
    }
    Ok(Some(plan))
}

fn apply(repo: &Repo, path: &Path, plan: Plan, types: &[String], is_dir: bool) -> Result<Outcome, String> {
    let err = |p: &Path, e: io::Error| format!("{}: {e}", repo.rel(p));
    if plan.clear_root {
        fs::remove_file(path).map_err(|e| err(path, e))?;
    }
    for p in &plan.remove {
        fs::remove_file(p).map_err(|e| err(p, e))?;
    }
    if is_dir {
        fs::create_dir_all(path).map_err(|e| err(path, e))?;
    }
    for (dest, oid) in &plan.link {
        link_object(&repo.cache, oid, dest, types).map_err(|e| err(dest, e))?;
    }
    Ok(Outcome::Updated { linked: plan.link.len(), removed: plan.remove.len() + usize::from(plan.clear_root) })
}

/// Put object `oid` at `dest` with the first link type that works, replacing what is
/// there in one rename.
pub fn link_object(cache: &Path, oid: &str, dest: &Path, types: &[String]) -> io::Result<&'static str> {
    let obj = odb::object_path(cache, oid);
    let _ = odb::protect(&obj);
    let parent = dest.parent().ok_or_else(|| io::Error::other("no parent"))?;
    fs::create_dir_all(parent)?;
    if fs::symlink_metadata(dest).is_ok_and(|m| m.is_dir()) {
        fs::remove_dir(dest)?;
    }
    let tmp = parent.join(format!(".{}.awm-tmp-{}", dest.file_name().unwrap_or_default().to_string_lossy(), std::process::id()));
    let mut last = io::Error::other("no cache link type is configured");
    for ty in types {
        let _ = fs::remove_file(&tmp);
        let made = match ty.as_str() {
            "hardlink" => fs::hard_link(&obj, &tmp).map(|_| "hardlink"),
            "symlink" => std::os::unix::fs::symlink(&obj, &tmp).map(|_| "symlink"),
            "copy" => fs::copy(&obj, &tmp).and_then(|_| fs::set_permissions(&tmp, fs::Permissions::from_mode(0o644))).map(|_| "copy"),
            other => Err(io::Error::new(io::ErrorKind::Unsupported, format!("link type {other} is not supported"))),
        };
        match made.and_then(|kind| fs::rename(&tmp, dest).map(|_| kind)) {
            Ok(kind) => return Ok(kind),
            Err(e) => last = e,
        }
    }
    let _ = fs::remove_file(&tmp);
    Err(last)
}
