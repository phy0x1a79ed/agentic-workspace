//! Collect the cache objects no listed project references. Guarded, and dry by default.
//!
//! **WARNING** The cache is shared by every project on the machine, so a collection
//! against an incomplete project set deletes data other worktrees depend on. Already
//! materialised files survive, so the loss shows only at the next fresh checkout.
//! Nothing is deleted unless every wired project is listed or deliberately excluded,
//! every referenced `.dir` manifest is present or accepted missing, and no other gc runs.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fs;
use std::io::{Read, Seek, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Instant;

use serde_json::{json, Map, Value};

use crate::git;
use crate::odb::{Manifest, ManifestError, DIR_SUFFIX};
use crate::provision::{chmod_dirs_writable, is_dvc_repo, Layout};

/// An unreferenced object younger than this is never collected: a `dvc add` writes its
/// objects before git can see its pin, and this engine takes no repo locks.
pub const GC_GRACE_DAYS: f64 = 14.0;
const PIN_PATHSPECS: [&str; 2] = ["*.dvc", "*dvc.lock"];
const BLOB_MODES: [&str; 2] = ["100644", "100755"];
const NULL_SHA: &str = "0000000000000000000000000000000000000000";

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct GcRefused(pub String);

#[derive(Debug, Clone)]
pub struct GcProject {
    pub name: String,
    pub bare: PathBuf,
    pub worktrees: Vec<PathBuf>,
}

fn git_z(repo: &Path, args: &[&str]) -> Result<Vec<String>, GcRefused> {
    let o = git::run(repo, args).map_err(|e| GcRefused(format!("git {} failed in {}: {e}", args[..2.min(args.len())].join(" "), repo.display())))?;
    if !o.status.success() {
        let err = String::from_utf8_lossy(&o.stderr);
        return Err(GcRefused(format!("git {} failed in {}: {}", args[..2.min(args.len())].join(" "), repo.display(), tail(err.trim(), 400))));
    }
    Ok(String::from_utf8_lossy(&o.stdout).split('\0').map(str::to_string).collect())
}

fn is_pin_path(path: &str) -> bool {
    let name = path.rsplit('/').next().unwrap_or(path);
    name == "dvc.lock" || (name.ends_with(".dvc") && name != ".dvc")
}

/// Every pin blob that any commit reachable from any ref introduced. A first-parent diff
/// of every commit, with `--root`, visits every blob of every reachable tree once.
fn history_pin_blobs(bare: &Path) -> Result<BTreeMap<String, String>, GcRefused> {
    let mut args = vec!["log", "--all", "--root", "--diff-merges=first-parent", "--no-renames", "--raw", "--no-abbrev", "-z", "--format=", "--"];
    args.extend(PIN_PATHSPECS);
    let toks = git_z(bare, &args)?;
    let mut blobs = BTreeMap::new();
    let mut i = 0;
    while i < toks.len() {
        let meta = toks[i].trim_start_matches('\n');
        if !meta.starts_with(':') || i + 1 >= toks.len() {
            i += 1;
            continue;
        }
        let path = toks[i + 1].clone();
        i += 2;
        let f: Vec<&str> = meta[1..].split_whitespace().collect();
        if f.len() >= 4 && BLOB_MODES.contains(&f[1]) && f[3] != NULL_SHA && is_pin_path(&path) {
            blobs.entry(f[3].to_string()).or_insert(path);
        }
    }
    Ok(blobs)
}

fn staged_pin_blobs(wt: &Path) -> Result<BTreeMap<String, String>, GcRefused> {
    let mut args = vec!["ls-files", "-s", "-z", "--"];
    args.extend(PIN_PATHSPECS);
    let mut blobs = BTreeMap::new();
    for rec in git_z(wt, &args)? {
        let Some((info, path)) = rec.split_once('\t') else { continue };
        let f: Vec<&str> = info.split_whitespace().collect();
        if f.len() >= 2 && BLOB_MODES.contains(&f[0]) && is_pin_path(path) {
            blobs.entry(f[1].to_string()).or_insert_with(|| path.to_string());
        }
    }
    Ok(blobs)
}

/// Modified and untracked pins on disk. Gitignored pins are not listed: that walk costs
/// minutes per worktree, and the grace window covers a pin nobody has staged yet.
fn unstaged_pin_files(wt: &Path) -> Result<Vec<PathBuf>, GcRefused> {
    let mut args = vec!["ls-files", "-z", "-m", "-o", "--exclude-standard", "--"];
    args.extend(PIN_PATHSPECS);
    let mut seen = HashSet::new();
    Ok(git_z(wt, &args)?.into_iter().filter(|p| !p.is_empty() && is_pin_path(p) && seen.insert(p.clone())).map(|p| wt.join(p)).collect())
}

fn read_blobs(bare: &Path, shas: &BTreeSet<String>) -> Result<BTreeMap<String, Vec<u8>>, GcRefused> {
    if shas.is_empty() {
        return Ok(BTreeMap::new());
    }
    let mut child = git::command(bare)
        .args(["cat-file", "--batch"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| GcRefused(format!("git cat-file failed in {}: {e}", bare.display())))?;
    let input: String = shas.iter().map(|s| format!("{s}\n")).collect();
    let mut stdin = child.stdin.take().expect("piped");
    let writer = std::thread::spawn(move || stdin.write_all(input.as_bytes()));
    let o = child.wait_with_output().map_err(|e| GcRefused(format!("git cat-file failed in {}: {e}", bare.display())))?;
    let _ = writer.join();
    if !o.status.success() {
        return Err(GcRefused(format!("git cat-file failed in {}: {}", bare.display(), tail(String::from_utf8_lossy(&o.stderr).trim(), 400))));
    }
    let out = o.stdout;
    let mut blobs = BTreeMap::new();
    let mut i = 0;
    while i < out.len() {
        let nl = out[i..].iter().position(|&b| b == b'\n').map(|p| i + p).ok_or_else(|| GcRefused("git cat-file output is cut short".into()))?;
        let header = String::from_utf8_lossy(&out[i..nl]).into_owned();
        let h: Vec<&str> = header.split_whitespace().collect();
        if h.len() < 3 {
            return Err(GcRefused(format!("pin blob {} is {} in {}", h.first().unwrap_or(&""), h.get(1).unwrap_or(&""), bare.display())));
        }
        let size: usize = h[2].parse().map_err(|_| GcRefused(format!("git cat-file sent a bad size: {header}")))?;
        let end = (nl + 1 + size).min(out.len());
        blobs.insert(h[0].to_string(), out[nl + 1..end].to_vec());
        i = nl + 1 + size + 1;
    }
    Ok(blobs)
}

/// The md5 of every output a `.dvc` file or `dvc.lock` names. Outputs only, as DVC's own
/// gc: a dependency is kept, or not, by whichever pin outputs it.
pub fn pin_hashes(text: &[u8], origin: &str) -> Result<BTreeSet<String>, GcRefused> {
    let text = String::from_utf8_lossy(text);
    let doc: serde_yaml::Value = serde_yaml::from_str(&text).map_err(|e| GcRefused(format!("cannot parse pin {origin}: {e}")))?;
    if doc.is_null() {
        return Ok(BTreeSet::new());
    }
    let Some(map) = doc.as_mapping() else { return Err(GcRefused(format!("pin {origin} is not a mapping"))) };
    let mut outs: Vec<serde_yaml::Value> = map.get("outs").and_then(|v| v.as_sequence()).cloned().unwrap_or_default();
    if origin.ends_with("dvc.lock") {
        let stages = if map.contains_key("schema") { map.get("stages").cloned().unwrap_or_default() } else { doc.clone() };
        if let Some(stages) = stages.as_mapping() {
            for stage in stages.values() {
                if let Some(o) = stage.as_mapping().and_then(|s| s.get("outs")).and_then(|v| v.as_sequence()) {
                    outs.extend(o.iter().cloned());
                }
            }
        }
    }
    let raw = crate::pin::raw_md5_tokens(&text);
    let mut hashes = BTreeSet::new();
    for out in &outs {
        let Some(m) = out.as_mapping() else { return Err(GcRefused(format!("pin {origin} has a malformed output: {out:?}"))) };
        let files = m.get("files").and_then(|f| f.as_sequence()).cloned().unwrap_or_default();
        for entry in std::iter::once(out).chain(files.iter()) {
            match entry.get("md5") {
                None | Some(serde_yaml::Value::Null) => {}
                Some(serde_yaml::Value::String(s)) if !s.is_empty() => {
                    hashes.insert(s.to_lowercase());
                }
                Some(serde_yaml::Value::String(_)) => {}
                // YAML read an all-digit hash as a number. Keep every hash the text holds.
                Some(_) => hashes.extend(raw.iter().map(|r| r.to_lowercase())),
            }
        }
    }
    Ok(hashes)
}

/// Add every file a manifest lists. A manifest absent from the cache goes to `missing`:
/// its children are unknown, so nothing is safe.
fn expand_dirs(cache: &Path, hashes: &BTreeMap<String, String>, missing: &mut BTreeMap<String, String>) -> Result<BTreeSet<String>, GcRefused> {
    let mut keep: BTreeSet<String> = hashes.keys().cloned().collect();
    for (oid, origin) in hashes {
        if !oid.ends_with(DIR_SUFFIX) {
            continue;
        }
        match Manifest::load(cache, oid) {
            Ok(m) => keep.extend(m.entries.into_iter().map(|e| e.md5.to_lowercase())),
            Err(ManifestError::Missing(_)) => {
                missing.insert(oid.clone(), origin.clone());
            }
            Err(e) => return Err(GcRefused(format!("cannot read .dir manifest {oid}: {e}"))),
        }
    }
    Ok(keep)
}

/// Every cache object one project still references, read once per bare repo.
pub fn project_keep_set(project: &GcProject, cache: &Path) -> Result<(BTreeSet<String>, Value), GcRefused> {
    let started = Instant::now();
    let mut blobs = history_pin_blobs(&project.bare)?;
    let history_blobs = blobs.len();
    let mut staged = BTreeMap::new();
    let mut on_disk = vec![];
    let mut missing_worktrees = vec![];
    for wt in &project.worktrees {
        if !wt.is_dir() {
            missing_worktrees.push(wt.to_string_lossy().into_owned());
            continue;
        }
        for (k, v) in staged_pin_blobs(wt)? {
            staged.entry(k).or_insert(v);
        }
        // Only a DVC worktree can hold an unstaged pin, and the untracked walk is slow.
        if is_dvc_repo(wt) {
            on_disk.extend(unstaged_pin_files(wt)?);
        }
    }
    for (k, v) in staged {
        blobs.insert(k, v);
    }
    let mut hashes: BTreeMap<String, String> = BTreeMap::new();
    let shas: BTreeSet<String> = blobs.keys().cloned().collect();
    for (sha, body) in read_blobs(&project.bare, &shas)? {
        let origin = format!("{}:{}", project.name, blobs.get(&sha).cloned().unwrap_or_default());
        for oid in pin_hashes(&body, &origin)? {
            hashes.entry(oid).or_insert_with(|| origin.clone());
        }
    }
    for pin in &on_disk {
        let Ok(body) = fs::read(pin) else { continue };
        let origin = pin.to_string_lossy().into_owned();
        for oid in pin_hashes(&body, &origin)? {
            hashes.entry(oid).or_insert_with(|| origin.clone());
        }
    }
    let mut missing = BTreeMap::new();
    let keep = expand_dirs(cache, &hashes, &mut missing)?;
    let info = json!({
        "worktrees": project.worktrees.len(),
        "missing_worktrees": missing_worktrees,
        "pin_blobs": blobs.len(),
        "history_pin_blobs": history_blobs,
        "unstaged_pins": on_disk.len(),
        "referenced": keep.len(),
        "missing_dir_manifests": missing,
        "seconds": round2(started.elapsed().as_secs_f64()),
    });
    Ok((keep, info))
}

/// Refuse a cache this engine does not understand. Only DVC 3's `files/md5` is walked, so
/// a legacy top-level object or a run cache would be invisible to sweep and keep set alike.
fn check_cache_layout(cache: &Path) -> Result<(), GcRefused> {
    if !cache.is_dir() {
        return Err(GcRefused(format!("no cache at {}", cache.display())));
    }
    let names = |d: &Path| -> Vec<String> {
        let mut v: Vec<String> = fs::read_dir(d).map(|rd| rd.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect()).unwrap_or_default();
        v.sort();
        v
    };
    let mut extra: Vec<String> = names(cache).into_iter().filter(|n| n != "files").collect();
    if cache.join("files").is_dir() {
        extra.extend(names(&cache.join("files")).into_iter().filter(|n| n != "md5").map(|n| format!("files/{n}")));
    }
    if !extra.is_empty() {
        let shown: Vec<&str> = extra.iter().take(10).map(String::as_str).collect();
        return Err(GcRefused(format!("unknown cache layout at {}: {}", cache.display(), shown.join(", "))));
    }
    Ok(())
}

pub fn gc_lock_path(cache: &Path) -> PathBuf {
    cache.with_file_name(format!("{}.gc.lock", cache.file_name().unwrap_or_default().to_string_lossy()))
}

/// The gc lock, held while this value lives.
pub struct GcLock(fs::File);

impl Drop for GcLock {
    fn drop(&mut self) {
        unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}

pub fn acquire_gc_lock(cache: &Path) -> Result<GcLock, GcRefused> {
    let path = gc_lock_path(cache);
    let mut f = fs::OpenOptions::new().read(true).append(true).create(true).open(&path).map_err(|e| GcRefused(format!("cannot open {}: {e}", path.display())))?;
    if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        let mut holder = String::new();
        let _ = f.read_to_string(&mut holder);
        let holder = holder.trim();
        return Err(GcRefused(format!("another gc is running ({})", if holder.is_empty() { "unknown holder" } else { holder })));
    }
    let _ = f.set_len(0);
    let _ = f.rewind();
    let _ = writeln!(f, "pid {} since {}", std::process::id(), chrono::Local::now().format("%Y-%m-%dT%H:%M:%S%z"));
    let _ = f.flush();
    Ok(GcLock(f))
}

#[derive(Debug, Default)]
struct Sweep {
    objects: u64,
    referenced: u64,
    grace_protected: u64,
    collected: u64,
    collected_bytes: u64,
    bytes_freed: u64,
    still_linked: u64,
    errors: Vec<String>,
}

fn sweep(cache: &Path, keep: &BTreeSet<String>, dry_run: bool, grace_s: f64) -> Result<Sweep, GcRefused> {
    let cutoff = now_s() - grace_s;
    let mut s = Sweep::default();
    let root = cache.join("files").join("md5");
    if !root.is_dir() {
        return Ok(s);
    }
    let mut shards: Vec<fs::DirEntry> = fs::read_dir(&root).map_err(|e| GcRefused(e.to_string()))?.flatten().collect();
    shards.sort_by_key(|e| e.file_name());
    for shard in shards {
        let name = shard.file_name().to_string_lossy().into_owned();
        let is_dir = shard.file_type().is_ok_and(|t| t.is_dir());
        let entries: Vec<(String, PathBuf)> = if is_dir {
            fs::read_dir(shard.path()).map_err(|e| GcRefused(e.to_string()))?.flatten().map(|o| (format!("{name}{}", o.file_name().to_string_lossy()), o.path())).collect()
        } else if name.starts_with('.') && name.ends_with(".tmp") {
            // An interrupted transfer's partial file: the grace window alone decides.
            vec![(name.clone(), shard.path())]
        } else {
            return Err(GcRefused(format!("unknown cache layout: {} is not a shard dir", shard.path().display())));
        };
        for (oid, path) in entries {
            sweep_one(&oid, &path, keep, cutoff, dry_run, &mut s);
        }
    }
    s.errors.truncate(20);
    Ok(s)
}

fn sweep_one(oid: &str, path: &Path, keep: &BTreeSet<String>, cutoff: f64, dry_run: bool, s: &mut Sweep) {
    s.objects += 1;
    if keep.contains(oid) {
        s.referenced += 1;
        return;
    }
    let Ok(st) = fs::symlink_metadata(path) else { return };
    let mtime = st.mtime() as f64 + st.mtime_nsec() as f64 / 1e9;
    let ctime = st.ctime() as f64 + st.ctime_nsec() as f64 / 1e9;
    if mtime.max(ctime) > cutoff {
        s.grace_protected += 1;
        return;
    }
    s.collected += 1;
    s.collected_bytes += st.len();
    // A second link means a worktree still holds the inode, so deleting frees nothing yet.
    if st.nlink() > 1 {
        s.still_linked += 1;
    } else {
        s.bytes_freed += st.len();
    }
    if dry_run {
        return;
    }
    match fs::remove_file(path) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            if let Some(parent) = path.parent() {
                chmod_dirs_writable(parent);
            }
            if let Err(e) = fs::remove_file(path) {
                s.errors.push(format!("{}: {e}", path.display()));
            }
        }
        Err(e) => s.errors.push(format!("{}: {e}", path.display())),
    }
}

/// What a gc run is asked to do.
#[derive(Debug, Clone)]
pub struct GcRequest {
    pub projects: Vec<GcProject>,
    pub wired: BTreeSet<String>,
    pub exclude: Vec<String>,
    pub accept_missing: Vec<String>,
    pub dry_run: bool,
    pub keep: String,
    pub grace_days: f64,
}

impl GcRequest {
    pub fn new(projects: Vec<GcProject>, wired: BTreeSet<String>) -> Self {
        GcRequest { projects, wired, exclude: vec![], accept_missing: vec![], dry_run: true, keep: "all-commits".into(), grace_days: GC_GRACE_DAYS }
    }
}

fn refused(detail: String, report: &Map<String, Value>) -> Value {
    let mut m = Map::new();
    m.insert("result".into(), "refused".into());
    m.insert("detail".into(), detail.into());
    for (k, v) in report {
        m.insert(k.clone(), v.clone());
    }
    Value::Object(m)
}

/// Delete the cache objects no listed project references.
pub fn collect_garbage(layout: &Layout, req: &GcRequest) -> Value {
    if req.keep != "all-commits" {
        return json!({"result": "refused", "detail": format!(
            "keep='{}' refused. Only 'all-commits' is supported: it keeps every output any commit, staged index or worktree file pins.", req.keep)});
    }
    if req.projects.is_empty() {
        return json!({"result": "refused", "detail": "no projects given — collecting against an incomplete set treats every other project's content as garbage"});
    }
    let listed: BTreeSet<String> = req.projects.iter().map(|p| p.name.clone()).collect();
    let excluded: BTreeSet<String> = req.exclude.iter().cloned().collect();
    let both: Vec<&String> = listed.intersection(&excluded).collect();
    if !both.is_empty() {
        return json!({"result": "refused", "detail": format!("projects both listed and excluded: {both:?}")});
    }
    let unlisted: Vec<&String> = req.wired.iter().filter(|w| !listed.contains(*w) && !excluded.contains(*w)).collect();
    if !unlisted.is_empty() {
        return json!({"result": "refused", "detail": format!(
            "DVC-wired projects neither listed nor excluded: {unlisted:?}. Their objects would be collected. List them in `projects` to keep their data, or in `exclude` to drop it deliberately.")});
    }
    let cache = layout.cache_dir(None);
    let mut accepted: Vec<String> = req.accept_missing.clone();
    accepted.sort();
    let mut report = Map::new();
    report.insert("dry_run".into(), req.dry_run.into());
    report.insert("deleted_anything".into(), false.into());
    report.insert("keep".into(), req.keep.clone().into());
    report.insert("cache".into(), cache.to_string_lossy().into());
    report.insert("grace_days".into(), json!(req.grace_days));
    report.insert("excluded".into(), json!(excluded));
    report.insert("accepted_missing".into(), json!(accepted));
    let started = Instant::now();
    if let Err(e) = check_cache_layout(&cache) {
        return refused(e.0, &report);
    }
    let _lock = match acquire_gc_lock(&cache) {
        Ok(l) => l,
        Err(e) => return refused(e.0, &report),
    };
    let mut keep_set = BTreeSet::new();
    let mut per_project = Map::new();
    for p in &req.projects {
        match project_keep_set(p, &cache) {
            Ok((refs, info)) => {
                keep_set.extend(refs);
                per_project.insert(p.name.clone(), info);
            }
            Err(e) => return refused(e.0, &report),
        }
    }
    report.insert("projects".into(), Value::Object(per_project.clone()));
    let accepted: HashSet<&String> = req.accept_missing.iter().collect();
    let mut missing: Vec<(String, String)> = vec![];
    for info in per_project.values() {
        if let Some(m) = info.get("missing_dir_manifests").and_then(Value::as_object) {
            for (oid, origin) in m {
                if !accepted.contains(oid) {
                    missing.push((oid.clone(), origin.as_str().unwrap_or_default().to_string()));
                }
            }
        }
    }
    if !missing.is_empty() {
        let shown: Vec<String> = missing.iter().take(10).map(|(o, w)| format!("'{o}': '{w}'")).collect();
        return refused(
            format!(
                "{} referenced .dir manifests are absent from the cache, so their files cannot be told apart from garbage: {{{}}}. Restore them from the archive, or pass their ids in `accept_missing` once you know the files are gone too.",
                missing.len(),
                shown.join(", ")
            ),
            &report,
        );
    }
    let s = match sweep(&cache, &keep_set, req.dry_run, req.grace_days * 86400.0) {
        Ok(s) => s,
        Err(e) => return refused(e.0, &report),
    };
    report.insert("deleted_anything".into(), (!req.dry_run && s.collected > 0).into());
    report.insert(
        "sweep".into(),
        json!({
            "objects": s.objects, "referenced": s.referenced, "grace_protected": s.grace_protected,
            "collected": s.collected, "collected_bytes": s.collected_bytes, "bytes_freed": s.bytes_freed,
            "still_linked": s.still_linked, "errors": s.errors,
        }),
    );
    report.insert("seconds".into(), json!(round2(started.elapsed().as_secs_f64())));
    let mut out = Map::new();
    out.insert("result".into(), "ok".into());
    out.extend(report);
    Value::Object(out)
}

fn now_s() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or_default()
}

fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

fn tail(s: &str, n: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    chars[chars.len().saturating_sub(n)..].iter().collect()
}
