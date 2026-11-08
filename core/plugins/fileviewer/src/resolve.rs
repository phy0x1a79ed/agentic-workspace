//! Which file a request path names, if it may be served at all. A path outside the root,
//! a masked one, a missing one and a symlink loop all yield `None`, so all look alike.

use std::ffi::OsString;
use std::fs::File;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};

use crate::mask::Mask;

/// The kernel's limit on symlinks followed in one lookup. More hops count as a loop.
const MAX_HOPS: usize = 40;

/// `os.path.realpath` without `strict`: every symlink resolved, a missing tail kept as
/// written, and `..` taken against the path resolved so far. `None` for a symlink loop.
pub fn realpath(path: &Path) -> Option<PathBuf> {
    let mut pending: Vec<OsString> = Vec::new();
    push_front(&mut pending, path);
    let mut out = PathBuf::from("/");
    let mut hops = 0;
    while let Some(c) = pending.pop() {
        if c == "." || c.is_empty() {
            continue;
        }
        if c == ".." {
            out.pop();
            continue;
        }
        let next = out.join(&c);
        let link = std::fs::symlink_metadata(&next).ok().filter(|m| m.file_type().is_symlink()).and_then(|_| std::fs::read_link(&next).ok());
        match link {
            Some(target) => {
                hops += 1;
                if hops > MAX_HOPS {
                    return None;
                }
                if target.is_absolute() {
                    out = PathBuf::from("/");
                }
                push_front(&mut pending, &target);
            }
            None => out = next,
        }
    }
    Some(out)
}

/// Queue `p`'s components to be walked before the rest. `pending` is a stack, last first.
fn push_front(pending: &mut Vec<OsString>, p: &Path) {
    let parts: Vec<OsString> = p
        .components()
        .filter_map(|c| match c {
            Component::Normal(n) => Some(n.to_os_string()),
            Component::ParentDir => Some("..".into()),
            Component::CurDir | Component::RootDir | Component::Prefix(_) => None,
        })
        .collect();
    pending.extend(parts.into_iter().rev());
}

/// `target` relative to `root` as the mask reads it: `/`-separated, and `.` for the root.
fn rel_of(target: &Path, root: &Path) -> Option<String> {
    let rel = target.strip_prefix(root).ok()?;
    Some(if rel.as_os_str().is_empty() { ".".into() } else { rel.to_string_lossy().into_owned() })
}

fn servable(target: &Path, root: &Path, mask: &Mask) -> bool {
    rel_of(target, root).is_some_and(|rel| !mask.hides(&rel))
}

/// The file the decoded request path `rel` names under `root` (already resolved), or `None`.
/// A directory serves its `index.html`, which is resolved and checked again in its own right.
pub fn resolve(rel: &str, root: &Path, mask: &Mask) -> Option<PathBuf> {
    let rel = rel.trim_start_matches('/');
    if rel.contains('\0') {
        return None;
    }
    let target = realpath(&root.join(rel))?;
    if !servable(&target, root, mask) {
        return None;
    }
    let target = if target.is_dir() {
        let index = target.join("index.html");
        if !servable(&index, root, mask) {
            return None;
        }
        let index = realpath(&index)?;
        if !servable(&index, root, mask) {
            return None;
        }
        index
    } else {
        target
    };
    target.is_file().then_some(target)
}

/// Open a resolved path for reading, refusing a symlink in its last component and anything
/// but a regular file, so a swap after [`resolve`] cannot redirect the read.
pub fn open_regular(path: &Path) -> Option<(File, u64)> {
    let f = std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path).ok()?;
    let meta = f.metadata().ok()?;
    meta.is_file().then_some((f, meta.len()))
}
