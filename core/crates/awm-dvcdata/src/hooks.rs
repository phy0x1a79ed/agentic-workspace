//! The three git hooks, run natively. git runs them from the worktree root.
//!
//! Three rules each cost a real bug to learn:
//! 1. **The mount list.** A checkout with no targets materialises every pin, so an
//!    explicit list keeps a cold chunk pinned and backed up without landing on disk. An
//!    absent list means everything, and an empty one means nothing.
//! 2. **The failure sentinel.** git ignores a hook's exit status, so a checkout that
//!    cannot be satisfied leaves `<git dir>/awm/data-checkout-failed` for `data_status`
//!    and provisioning to surface.
//! 3. **Shared chunks.** A chunk listed in `data/.awm-shared` is a symlink into the
//!    project's store, repointed here to the copy for its new md5. Building a missing copy
//!    is provisioning's job, and a link to another md5 is never left in place.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::checkout;
use crate::git;
use crate::pin::{normalize, raw_md5_tokens};
use crate::provision::{CHECKOUT_FAILED_FILE, MOUNTS_FILE, SHARED_DECLARATION, STATE_SUBDIR};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HookKind {
    PostMerge,
    PostCommit,
    PostCheckout,
}

impl HookKind {
    pub const ALL: [HookKind; 3] = [HookKind::PostMerge, HookKind::PostCommit, HookKind::PostCheckout];

    pub fn name(self) -> &'static str {
        match self {
            HookKind::PostMerge => "post-merge",
            HookKind::PostCommit => "post-commit",
            HookKind::PostCheckout => "post-checkout",
        }
    }

    pub fn from_name(s: &str) -> Option<HookKind> {
        HookKind::ALL.into_iter().find(|k| k.name() == s)
    }

    pub fn purpose(self) -> &'static str {
        match self {
            HookKind::PostMerge => "Materialise the data this commit pins, after a merge moved it.",
            HookKind::PostCommit => "A conflicted merge is resolved with `git commit`, which fires no post-merge hook, so catch merge commits here.",
            HookKind::PostCheckout => "Repoint shared-chunk links after a branch switch.",
        }
    }

    fn after(self) -> &'static str {
        match self {
            HookKind::PostMerge => "merge",
            HookKind::PostCommit => "a conflicted merge",
            HookKind::PostCheckout => "a branch switch",
        }
    }
}

/// Run one hook in `wt` with git's arguments. Always 0: git ignores a hook's status, and
/// a failure goes to the sentinel instead.
pub fn run(kind: HookKind, wt: &Path, args: &[String]) -> i32 {
    match kind {
        HookKind::PostCommit if git::head_parents(wt) < 2 => return 0,
        HookKind::PostCheckout if args.get(2).map(String::as_str) != Some("1") => return 0,
        _ => {}
    }
    // Hooks live in the common dir and fire in every worktree, converted or not.
    if !wt.join(".dvc").is_dir() {
        return 0;
    }
    let links_only = kind == HookKind::PostCheckout;
    let state = git::git_dir(wt).unwrap_or_else(|| wt.join(".git")).join(STATE_SUBDIR);
    let sentinel = state.join(CHECKOUT_FAILED_FILE);
    if !links_only {
        let _ = fs::remove_file(&sentinel);
    }
    let mut targets: Vec<String> = vec![];
    let mut have_list = false;
    if let Ok(text) = fs::read_to_string(state.join(MOUNTS_FILE)) {
        have_list = true;
        targets.extend(text.lines().filter(|l| !l.is_empty() && !l.starts_with('#')).map(str::to_string));
    }
    if have_list && targets.is_empty() {
        return 0;
    }
    let mut missing = vec![];
    let shared = wt.join("data").join(SHARED_DECLARATION);
    let common = git::common_dir(wt);
    if let (Ok(decl), Some(common)) = (fs::read_to_string(&shared), common.filter(|c| c.file_name().is_some_and(|n| n == ".bare"))) {
        let store = common.parent().map(|p| p.join("data")).unwrap_or_default();
        if !have_list {
            have_list = true;
            targets = git::chunk_pins(wt).into_iter().map(|p| p.strip_suffix(".dvc").unwrap_or(&p).to_string()).collect();
        }
        for line in decl.lines() {
            let c = line.strip_prefix("data/").unwrap_or(line).trim_end_matches('/');
            if c.is_empty() || c.starts_with('#') {
                continue;
            }
            let chunk = format!("data/{c}");
            let pin = wt.join(format!("{chunk}.dvc"));
            let path = wt.join(&chunk);
            if !pin.is_file() {
                continue;
            }
            if path.is_dir() && !path.is_symlink() {
                continue;
            }
            let before = targets.len();
            targets.retain(|t| t.trim_end_matches('/') != chunk);
            if targets.len() == before {
                continue;
            }
            let md5 = fs::read_to_string(&pin).ok().and_then(|t| raw_md5_tokens(&t).into_iter().find_map(|v| hex32(&v)));
            match md5.as_deref().map(|m| store.join(c).join(m)).filter(|copy| copy.is_dir()) {
                Some(copy) => {
                    if let Err(e) = link_dir(&path, &copy) {
                        missing.push(format!("{chunk}: {e}"));
                    }
                }
                None => {
                    if path.is_symlink() {
                        let _ = fs::remove_file(&path);
                    }
                    missing.push(format!("{chunk}@{}", md5.unwrap_or_default()));
                }
            }
        }
    }
    let mut out = String::new();
    let mut failed = false;
    if !links_only && (!have_list || !targets.is_empty()) {
        let names = have_list.then_some(targets.as_slice());
        match checkout::checkout(wt, names) {
            Ok(r) if r.ok() => {}
            Ok(r) => {
                failed = true;
                out = r.failures();
            }
            Err(e) => {
                failed = true;
                out = e.to_string();
            }
        }
    }
    if !missing.is_empty() {
        failed = true;
        out = format!("{out}\nno shared copy on this node for: {}. Run 'scope heal' to build it.", missing.join(" "));
    }
    if failed {
        let _ = fs::create_dir_all(&state);
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&sentinel) {
            let _ = writeln!(f, "dvc checkout FAILED after {}\n{out}", kind.after());
        }
        eprintln!("!! awm: dvc checkout failed -- your data does NOT match this commit.");
        eprintln!("!! {out}");
        eprintln!("!! recorded in {}; run 'scope heal' after restoring the cache.", sentinel.display());
    }
    0
}

/// The 32-hex prefix of an md5 value, as the shell hook's `grep -oE 'md5: [0-9a-f]{32}'`.
fn hex32(v: &str) -> Option<String> {
    let h: String = v.chars().take(32).collect();
    (h.len() == 32 && h.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())).then_some(h)
}

/// Point `link` at `copy` with a relative path, replacing a link already there.
pub fn link_dir(link: &Path, copy: &Path) -> std::io::Result<()> {
    let parent = link.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(parent)?;
    // Both ends resolved, so the relative link holds wherever a symlink sits on the way.
    let real = |p: &Path| fs::canonicalize(p).unwrap_or_else(|_| absolute(p));
    let rel = relative(&real(parent), &real(copy));
    let tmp = parent.join(format!(".{}.awm-link-{}", link.file_name().unwrap_or_default().to_string_lossy(), std::process::id()));
    let _ = fs::remove_file(&tmp);
    std::os::unix::fs::symlink(&rel, &tmp)?;
    fs::rename(&tmp, link)
}

fn absolute(p: &Path) -> PathBuf {
    if p.is_absolute() {
        normalize(p)
    } else {
        normalize(&std::env::current_dir().unwrap_or_default().join(p))
    }
}

/// `to` relative to the directory `from`, lexically, as `realpath -ms --relative-to`.
pub fn relative(from: &Path, to: &Path) -> PathBuf {
    let f: Vec<_> = from.components().collect();
    let t: Vec<_> = to.components().collect();
    let common = f.iter().zip(&t).take_while(|(a, b)| a == b).count();
    let mut out = PathBuf::new();
    for _ in common..f.len() {
        out.push("..");
    }
    for c in &t[common..] {
        out.push(c.as_os_str());
    }
    if out.as_os_str().is_empty() {
        out.push(".");
    }
    out
}
