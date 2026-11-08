//! A scope's data view: provisioning, mounts, shared chunks and status.
//!
//! There is no data branch and no promote verb. A code commit is the data commit and a
//! code merge is the promote, so provisioning is only wiring plus checkout.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use crate::cachetag::has_cache_tag;
use crate::checkout;
use crate::git;
use crate::hooks::link_dir;
use crate::odb;
use crate::pin::pin_md5;
use crate::status::{self, State};
use crate::wiring;

pub const DATA_SUBDIR: &str = "data";
pub const CACHE_DIRNAME: &str = ".dvc_cache";
/// Per worktree, in its git dir: the chunks it materialises, one per line.
pub const MOUNTS_FILE: &str = "data-mounts";
/// Written by a hook when a checkout could not be satisfied.
pub const CHECKOUT_FAILED_FILE: &str = "data-checkout-failed";
pub const STATE_SUBDIR: &str = "awm";
/// The tracked file that lists the chunks each node materialises once, as a shared copy.
pub const SHARED_DECLARATION: &str = ".awm-shared";
pub const SHARED_STORE_DIRNAME: &str = "data";

/// Where the workspace keeps its shared cache.
#[derive(Debug, Clone)]
pub struct Layout {
    pub workspace: PathBuf,
    /// Where DVC's rebuildable per-worktree index goes when it is writable.
    pub tmpfs: PathBuf,
}

impl Layout {
    pub fn new(workspace: impl Into<PathBuf>) -> Self {
        Layout { workspace: workspace.into(), tmpfs: PathBuf::from("/dev/shm") }
    }

    /// `AWM_WORKSPACE`, else `~/agentic_workspace`.
    pub fn from_env() -> Self {
        let ws = std::env::var_os("AWM_WORKSPACE").map(PathBuf::from).unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join("agentic_workspace")
        });
        Layout::new(ws)
    }

    pub fn data_dir(&self) -> PathBuf {
        self.workspace.join(DATA_SUBDIR)
    }

    /// The cache a worktree's objects live in: the shared one, unless its project opted
    /// out of every remote and keeps its own beside its worktrees.
    pub fn cache_dir(&self, repo: Option<&Path>) -> PathBuf {
        if let Some(r) = repo {
            if no_remote(r) {
                if let Some(p) = project_dir(r) {
                    return p.join(CACHE_DIRNAME);
                }
            }
        }
        self.data_dir().join(CACHE_DIRNAME)
    }

    /// DVC rebuilds its SQLite index per worktree, row by row. On a busy disk one 68-pin
    /// checkout took 400 s against 2 s on tmpfs.
    pub fn site_cache_dir(&self) -> Option<PathBuf> {
        let ok = fs::metadata(&self.tmpfs).is_ok_and(|m| m.is_dir()) && access_w(&self.tmpfs);
        ok.then(|| self.tmpfs.join(format!("awm-dvc-{}", unsafe { libc::getuid() })))
    }
}

fn access_w(p: &Path) -> bool {
    let c = std::ffi::CString::new(p.as_os_str().as_encoded_bytes()).unwrap_or_default();
    unsafe { libc::access(c.as_ptr(), libc::W_OK) == 0 }
}

/// The kill switch: `AWM_DATA_DVC=0` provisions no project's data.
pub fn enabled() -> bool {
    !matches!(std::env::var("AWM_DATA_DVC").unwrap_or_else(|_| "1".into()).to_lowercase().as_str(), "0" | "off" | "false" | "no")
}

/// `projects/<p>/` for a worktree: the directory that holds its common git dir.
pub fn project_dir(repo: &Path) -> Option<PathBuf> {
    git::common_dir(repo).and_then(|c| c.parent().map(Path::to_path_buf))
}

/// Whether the project opted out of every remote, with a `CACHEDIR.TAG` at `projects/<p>/`.
pub fn no_remote(repo: &Path) -> bool {
    project_dir(repo).is_some_and(|p| has_cache_tag(&p))
}

pub fn data_dir(repo: &Path) -> PathBuf {
    repo.join(DATA_SUBDIR)
}

/// Whether the checkout carries a tracked `.dvc/config`. The commit answers it, so a
/// branch that predates the conversion reports false even with a `.dvc/` dir left over.
pub fn is_dvc_repo(repo: &Path) -> bool {
    repo.join(".dvc").join("config").is_file()
}

pub fn is_dvc_project(repo: &Path) -> bool {
    enabled() && is_dvc_repo(repo)
}

pub use git::chunk_pins;

/// `projects/<project>/data`, or `None` outside the bare-repo layout.
pub fn shared_store_root(repo: &Path) -> Option<PathBuf> {
    let common = git::common_dir(repo)?;
    (common.file_name()? == ".bare").then(|| common.parent().map(|p| p.join(SHARED_STORE_DIRNAME))).flatten()
}

/// The chunks this checkout declares shared, as `data/<chunk>` to md5. An entry with no
/// tracked pin, or no single hashed output, has nothing to share.
pub fn shared_chunks(repo: &Path) -> BTreeMap<String, String> {
    let Ok(text) = fs::read_to_string(data_dir(repo).join(SHARED_DECLARATION)) else { return BTreeMap::new() };
    let tracked: std::collections::HashSet<String> = chunk_pins(repo).into_iter().collect();
    let mut out = BTreeMap::new();
    for line in text.lines() {
        let rel = line.trim();
        let rel = rel.strip_prefix("data/").unwrap_or(rel).trim_matches('/');
        if rel.is_empty() || rel.starts_with('#') || rel.split('/').any(|p| p == "..") {
            continue;
        }
        let target = format!("{DATA_SUBDIR}/{rel}");
        if !tracked.contains(&format!("{target}.dvc")) {
            continue;
        }
        if let Some(md5) = pin_md5(&repo.join(format!("{target}.dvc"))) {
            out.insert(target, md5);
        }
    }
    out
}

fn shared_copy(store: &Path, target: &str, md5: &str) -> PathBuf {
    store.join(target.strip_prefix("data/").unwrap_or(target)).join(md5)
}

fn links_to(link: &Path, copy: &Path) -> bool {
    copy.is_dir() && matches!((fs::canonicalize(link), fs::canonicalize(copy)), (Ok(a), Ok(b)) if a == b)
}

/// Give the owner write on directories only, so a teardown can finish.
///
/// **WARNING** Never a file. A materialised file is a hardlink to the shared cache
/// object, so `chmod +w` on it unprotects that object for every scope, every project and
/// every commit that pins it. `rm -rf` needs write on the containing directory only.
pub fn chmod_dirs_writable(path: &Path) {
    let mut stack = vec![path.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(meta) = fs::symlink_metadata(&d) else { continue };
        if !meta.is_dir() {
            continue;
        }
        let _ = fs::set_permissions(&d, fs::Permissions::from_mode((meta.mode() & 0o7777) | 0o200));
        if let Ok(rd) = fs::read_dir(&d) {
            stack.extend(rd.flatten().map(|e| e.path()));
        }
    }
}

fn remove_tree(path: &Path) -> io::Result<()> {
    chmod_dirs_writable(path);
    fs::remove_dir_all(path)
}

/// Make a shared copy's directories read-only, so nothing rewrites it through a link.
fn seal_dirs(copy: &Path) -> io::Result<()> {
    let mut dirs = vec![];
    let mut stack = vec![copy.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in fs::read_dir(&d)?.flatten() {
            if e.file_type()?.is_dir() {
                stack.push(e.path());
            }
        }
        dirs.push(d);
    }
    for d in dirs.iter().rev() {
        let mode = fs::metadata(d)?.mode() & 0o7777;
        fs::set_permissions(d, fs::Permissions::from_mode(mode & !0o222))?;
    }
    Ok(())
}

/// Clear write bits a DVC checkout set on a shared copy's files. Always safe.
fn reprotect_files(copy: &Path) {
    let mut stack = vec![copy.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(rd) = fs::read_dir(&d) else { continue };
        for e in rd.flatten() {
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                stack.push(e.path());
            } else if ft.is_file() {
                let _ = odb::protect(&e.path());
            }
        }
    }
}

fn tree_matches_pin(repo: &Path, target: &str) -> bool {
    status::in_sync(repo, Some(&[target.to_string()])).unwrap_or(false)
}

struct DirLock(fs::File);

impl DirLock {
    fn take(path: &Path) -> io::Result<DirLock> {
        let f = fs::OpenOptions::new().create(true).truncate(false).write(true).open(path)?;
        use std::os::fd::AsRawFd;
        if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(DirLock(f))
    }
}

impl Drop for DirLock {
    fn drop(&mut self) {
        use std::os::fd::AsRawFd;
        unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}

/// Make `<repo>/<target>` a symlink to the node's shared copy of `md5`, building the copy
/// on first use by checking the chunk out here once and renaming it into the store. A
/// clean private tree is adopted the same way, or removed once the copy exists. A
/// modified one is left alone.
pub fn link_shared_chunk(repo: &Path, store: &Path, target: &str, md5: &str) -> String {
    let path = repo.join(target);
    let copy = shared_copy(store, target, md5);
    if path.is_symlink() {
        if links_to(&path, &copy) {
            reprotect_files(&copy);
            return "linked".into();
        }
        let _ = fs::remove_file(&path);
    } else if path.exists() {
        if !tree_matches_pin(repo, target) {
            return "private:modified".into();
        }
        if copy.is_dir() {
            if let Err(e) = remove_tree(&path) {
                return format!("private: could not remove {}: {e}", path.display());
            }
        }
    }
    let parent = copy.parent().expect("under the store");
    if let Err(e) = fs::create_dir_all(parent) {
        return format!("private: could not create {}: {e}", parent.display());
    }
    {
        let _lock = match DirLock::take(&parent.join(format!(".{md5}.lock"))) {
            Ok(l) => l,
            Err(e) => return format!("private: could not lock {}: {e}", parent.display()),
        };
        if !copy.is_dir() {
            if !path.exists() {
                match checkout::checkout(repo, Some(&[target.to_string()])) {
                    Ok(r) if r.ok() => {}
                    Ok(r) => return format!("missing-copy: {}", tail(&r.failures(), 300)),
                    Err(e) => return format!("missing-copy: {}", tail(&e.to_string(), 300)),
                }
            }
            if let Err(e) = fs::rename(&path, &copy) {
                return format!("private: could not move into {}: {e}", copy.display());
            }
            let _ = seal_dirs(&copy);
        } else if path.is_dir() && !path.is_symlink() {
            let _ = remove_tree(&path);
        }
    }
    match link_dir(&path, &copy) {
        Ok(()) => "linked".into(),
        Err(e) => format!("private: could not link {}: {e}", path.display()),
    }
}

/// `linked`, `stale-link`, `missing-copy`, `private` or `absent`, without hashing.
pub fn shared_link_state(repo: &Path, store: &Path, target: &str, md5: &str) -> &'static str {
    let path = repo.join(target);
    let copy = shared_copy(store, target, md5);
    if path.is_symlink() {
        if links_to(&path, &copy) {
            return "linked";
        }
        return if copy.is_dir() { "stale-link" } else { "missing-copy" };
    }
    if path.exists() {
        return "private";
    }
    if copy.is_dir() {
        "absent"
    } else {
        "missing-copy"
    }
}

fn in_view(target: &str, mounts: Option<&[String]>) -> bool {
    mounts.is_none_or(|m| m.iter().any(|x| x.trim_end_matches('/') == target))
}

/// The ignore rule that would swallow this repo's pins, if one would. Under it `dvc add`
/// writes the pin, `git add -A` skips it, and the commit records no data. Nothing errors.
pub fn pin_would_be_ignored(repo: &Path) -> Option<String> {
    let o = git::run(repo, &["check-ignore", "-v", &format!("{DATA_SUBDIR}/.awm-pin-probe.dvc")]).ok()?;
    if !o.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&o.stdout);
    let rule = s.trim().split('\t').next().unwrap_or_default().to_string();
    Some(if rule.is_empty() { "an ignore rule".into() } else { rule })
}

/// Why `<repo>/data` cannot become the tracked data folder. The answer is always to stop
/// and report, never to delete what a human put there.
pub fn data_path_conflict(repo: &Path) -> Option<String> {
    let data = data_dir(repo);
    if data.is_symlink() {
        let to = fs::read_link(&data).map(|p| p.display().to_string()).unwrap_or_default();
        return Some(format!(
            "{} is a symlink to '{to}'. It predates this layer; move it aside so `data/` can become the tracked data folder.",
            data.display()
        ));
    }
    if data.is_dir() {
        let tracked = git::out(repo, &["ls-files", "--error-unmatch", DATA_SUBDIR]).is_some_and(|s| !s.is_empty());
        let has_pin = fs::read_dir(&data).map(|rd| rd.flatten().any(|e| e.file_name().to_string_lossy().ends_with(".dvc"))).unwrap_or(false);
        if tracked && !has_pin && chunk_pins(repo).is_empty() {
            return Some(format!(
                "{} is already tracked by git and holds no DVC pins. `dvc add` refuses an SCM-tracked output; migrate or move it before converting this project.",
                data.display()
            ));
        }
    }
    None
}

/// `<git dir>/awm` of a worktree: the hooks' local state, which never travels.
pub fn state_dir(wt: &Path) -> PathBuf {
    git::git_dir(wt).unwrap_or_else(|| wt.join(".git")).join(STATE_SUBDIR)
}

/// The chunks this scope wants on disk, or `None` for all of them. Mounting is a local
/// choice, so the list lives in the worktree's git dir while the pins travel.
pub fn read_mounts(wt: &Path) -> Option<Vec<String>> {
    let text = fs::read_to_string(state_dir(wt).join(MOUNTS_FILE)).ok()?;
    Some(text.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')).map(str::to_string).collect())
}

/// Declare the chunks to materialise. `None` removes the list, which means all of them.
pub fn write_mounts(wt: &Path, chunks: Option<&[String]>) -> io::Result<PathBuf> {
    let path = state_dir(wt).join(MOUNTS_FILE);
    let Some(chunks) = chunks else {
        match fs::remove_file(&path) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e),
            _ => return Ok(path),
        }
    };
    fs::create_dir_all(path.parent().expect("in the state dir"))?;
    let mut body = String::from(
        "# AWM-managed, worktree-local. One chunk path per line.\n\
         # These are the chunks materialised on disk here. Every chunk this\n\
         # commit pins stays pinned and backed up whether or not it is listed --\n\
         # this file only decides what costs you inodes and checkout time.\n\
         # Delete this file to materialise everything.\n",
    );
    for c in chunks {
        body.push_str(c);
        body.push('\n');
    }
    fs::write(&path, body)?;
    Ok(path)
}

fn tail(s: &str, n: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    chars[chars.len().saturating_sub(n)..].iter().collect()
}

fn mounts_value(m: &Option<Vec<String>>) -> Value {
    match m {
        Some(v) => json!(v),
        None => json!("all"),
    }
}

/// Materialise a scope's data view. The single entry point, and idempotent: scope
/// creation and the repair path both run it. `mode` is `dvc`, `none` or `unknown`.
pub fn provision_scope_data(layout: &Layout, _project: &str, _scope: &str, wt: &Path, hook_bin: &Path) -> Value {
    if !wt.is_absolute() {
        return json!({"mode": "unknown", "path": wt.to_string_lossy(), "detail": format!(
            "refusing a relative worktree path ({}): it would resolve against the wrong directory. Pass an absolute worktree path.", wt.display())});
    }
    let path = data_dir(wt).to_string_lossy().into_owned();
    if !is_dvc_project(wt) {
        let detail = if enabled() { "project not DVC-backed" } else { "data provisioning is off (AWM_DATA_DVC=0)" };
        return json!({"mode": "none", "path": path, "detail": detail});
    }
    if let Some(rule) = pin_would_be_ignored(wt) {
        return json!({"mode": "unknown", "path": path, "detail": format!(
            "refusing: {rule} makes git ignore `{DATA_SUBDIR}/*.dvc`, so every data pin would be written and then silently never committed. Remove the rule covering `{DATA_SUBDIR}/` before converting.")});
    }
    if let Some(conflict) = data_path_conflict(wt) {
        return json!({"mode": "unknown", "path": path, "detail": conflict});
    }
    // The cache config first: a checkout against the wrong cache builds a second one.
    if let Err(e) = wiring::ensure_cache_config(wt, layout) {
        return json!({"mode": "unknown", "path": path, "detail": format!("cannot write the cache config: {e}")});
    }
    let wiring = wiring::ensure_repo_wiring(wt, hook_bin);
    let mut report = Map::new();
    report.insert("mode".into(), "dvc".into());
    report.insert("path".into(), path.into());
    report.insert("cache".into(), layout.cache_dir(Some(wt)).to_string_lossy().into());
    report.insert("rev".into(), git::head_rev(wt).into());
    report.insert("branch".into(), git::current_branch(wt).into());
    report.insert("wiring".into(), wiring.get("actions").cloned().unwrap_or_else(|| json!({})));

    let pins = chunk_pins(wt);
    report.insert("chunks".into(), pins.len().into());
    let mounts = read_mounts(wt);
    report.insert("mounts".into(), mounts_value(&mounts));
    let store = shared_store_root(wt);
    let shared = if store.is_some() { shared_chunks(wt) } else { BTreeMap::new() };
    // `None` means everything and an empty list means nothing. Shared chunks never go
    // through the per-scope checkout, so once a project declares any the list is explicit.
    let mut targets = mounts.clone();
    if !shared.is_empty() && mounts.as_deref() != Some(&[]) {
        let base = mounts.clone().unwrap_or_else(|| pins.iter().map(|p| p.strip_suffix(".dvc").unwrap_or(p).to_string()).collect());
        targets = Some(base.into_iter().filter(|t| !shared.contains_key(t.trim_end_matches('/'))).collect());
    }
    let checkout_state = if !pins.is_empty() && targets.as_deref() != Some(&[]) && mounts.as_deref() != Some(&[]) {
        match checkout::checkout(wt, targets.as_deref()) {
            Ok(r) if r.ok() => "ok",
            Ok(r) => {
                report.insert("checkout_detail".into(), tail(&r.failures(), 400).into());
                "partial"
            }
            Err(e) => {
                report.insert("checkout_detail".into(), tail(&e.to_string(), 400).into());
                "partial"
            }
        }
    } else {
        "skipped"
    };
    report.insert("checkout".into(), checkout_state.into());

    let mut linked = Map::new();
    if let (Some(store), false) = (&store, shared.is_empty()) {
        for (t, md5) in &shared {
            if in_view(t, mounts.as_deref()) {
                linked.insert(t.clone(), link_shared_chunk(wt, store, t, md5).into());
            }
        }
        report.insert("shared".into(), Value::Object(linked.clone()));
    }

    // A sentinel means an earlier merge advanced a pin the cache could not supply.
    let sentinel = state_dir(wt).join(CHECKOUT_FAILED_FILE);
    if sentinel.exists() {
        let checked_out = checkout_state == "ok" || (!shared.is_empty() && checkout_state == "skipped");
        let all_linked = linked.values().all(|v| v.as_str().is_some_and(|s| s == "linked" || s.starts_with("private")));
        if checked_out && all_linked {
            let _ = fs::remove_file(&sentinel);
        } else {
            report.insert("stale_checkout".into(), tail(&fs::read_to_string(&sentinel).unwrap_or_default(), 400).into());
        }
    }
    Value::Object(report)
}

/// A scope's data view: its mode, the commit that pins it, and any drift between the
/// workspace and that commit.
pub fn data_status(layout: &Layout, project: &str, scope: &str, wt: &Path) -> Value {
    let path = data_dir(wt).to_string_lossy().into_owned();
    if !is_dvc_repo(wt) {
        return json!({"project": project, "scope": scope, "mode": "none", "path": path});
    }
    let pins = chunk_pins(wt);
    let mounts = read_mounts(wt);
    let mut out = Map::new();
    for (k, v) in [
        ("project", json!(project)),
        ("scope", json!(scope)),
        ("mode", json!("dvc")),
        ("path", json!(path)),
        ("branch", json!(git::current_branch(wt))),
        ("rev", json!(git::head_rev(wt))),
        ("chunks", json!(pins)),
        ("mounts", mounts_value(&mounts)),
        ("cache", json!(layout.cache_dir(Some(wt)).to_string_lossy())),
        ("engine", json!("native")),
    ] {
        out.insert(k.into(), v);
    }
    let sentinel = state_dir(wt).join(CHECKOUT_FAILED_FILE);
    if let Ok(text) = fs::read_to_string(&sentinel) {
        out.insert("checkout_failed".into(), tail(&text, 600).into());
    }
    let store = shared_store_root(wt);
    let shared = if store.is_some() { shared_chunks(wt) } else { BTreeMap::new() };
    let mut targets: Vec<String> = vec![];
    if let (Some(store), false) = (&store, shared.is_empty()) {
        let states: BTreeMap<&String, &str> = shared.iter().map(|(t, md5)| (t, shared_link_state(wt, store, t, md5))).collect();
        out.insert("shared".into(), json!(states));
        // A shared copy is checked once, when it is built. Hashing it from every scope
        // would cost what sharing it saves.
        targets = pins
            .iter()
            .map(|p| p.strip_suffix(".dvc").unwrap_or(p).to_string())
            .filter(|t| !shared.contains_key(t) || states.get(t) == Some(&"private"))
            .collect();
    }
    if !targets.is_empty() || shared.is_empty() {
        let names = (!targets.is_empty()).then_some(targets.as_slice());
        match status::changed_outs(wt, names) {
            Ok(changed) => {
                out.insert("in_sync".into(), changed.is_empty().into());
                let mut by_state: BTreeMap<&str, Vec<String>> = BTreeMap::new();
                for outs in changed.values() {
                    for (p, s) in outs {
                        by_state.entry(s.as_str()).or_default().push(p.clone());
                    }
                }
                if !changed.is_empty() {
                    let lines: Vec<String> = changed.values().flat_map(|o| o.iter().map(|(p, s)| format!("{p} ({})", s.as_str()))).collect();
                    out.insert("drift".into(), tail(&lines.join("\n"), 600).into());
                }
                let missing: Vec<&String> = by_state.get(State::NotInCache.as_str()).map(|v| v.iter().collect()).unwrap_or_default();
                let summary = json!({"not_in_cache": missing, "changed": by_state});
                out.insert("data_status".into(), tail(&summary.to_string(), 2000).into());
            }
            Err(e) => {
                out.insert("in_sync".into(), false.into());
                out.insert("drift".into(), tail(&e.to_string(), 600).into());
            }
        }
    }
    Value::Object(out)
}
