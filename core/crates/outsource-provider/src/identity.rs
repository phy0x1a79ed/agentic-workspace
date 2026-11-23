//! Material identity without reading content: a file is its path and mtime, a folder its path and a hash of its whole tree listing
//! (names, sizes, mtimes), a git repo its HEAD commit plus a hash of its dirty state when it has any.

use crate::host::Host;
use crate::jobs::q;
use outsource_proto::{instance_id, props, Id};

/// What a probe learns of a material on the host that holds it.
#[derive(Debug, Clone, PartialEq)]
pub struct Probed {
    /// The canonical path.
    pub path: String,
    pub id: Id,
    pub file: bool,
    /// Bytes on disk, or 0 when the probe was not asked to size it.
    pub size: u64,
}

/// A shell script that prints what identity needs of `path`: the canonical path, its size (or 0),
/// `file` or `dir`, then for a repo `git`, HEAD and a hash of its dirty state (empty when clean), else the file's mtime in ns or the folder's tree hash.
fn probe(path: &str, sized: bool) -> String {
    let size = if sized { "du -sbD \"$p\" | cut -f1" } else { "echo 0" };
    format!(
        "set -e; p={}; [ -e \"$p\" ] || {{ echo \"$p does not exist\" >&2; exit 1; }}; readlink -f \"$p\"; {size}\n\
         if [ -f \"$p\" ]; then echo file; else echo dir; fi\n\
         if [ -e \"$p/.git\" ]; then echo git; git -C \"$p\" rev-parse HEAD; s=$(git -C \"$p\" status --porcelain); \
         if [ -n \"$s\" ]; then {{ echo \"$s\"; git -C \"$p\" ls-files -mo --exclude-standard -z | (cd \"$p\" && xargs -0 -r -I{{}} find {{}} -maxdepth 0 -printf '%p %s %T@\\n' 2>/dev/null || true); }} | md5sum | cut -d' ' -f1; else echo; fi\n\
         elif [ -f \"$p\" ]; then date -r \"$p\" +%s%N\n\
         else find \"$p\" -printf '%P %s %T@\\n' | LC_ALL=C sort | md5sum | cut -d' ' -f1; fi\n",
        q(path)
    )
}

fn from_probe(out: &str, origin: &str) -> Result<Probed, String> {
    let mut lines = out.lines();
    let (Some(path), Some(size), Some(kind), Some(stamp)) = (lines.next(), lines.next(), lines.next(), lines.next()) else {
        return Err(format!("unreadable probe: {out:?}"));
    };
    let (file, size) = (kind == "file", size.parse().unwrap_or(0));
    let id = if stamp == "git" {
        let head = lines.next().unwrap_or_default();
        match lines.next().unwrap_or_default() {
            "" => instance_id(&props([("git", head), ("origin", origin)])),
            dirty => instance_id(&props([("git", head), ("dirty", dirty), ("origin", origin)])),
        }
    } else {
        instance_id(&props([("path", path), ("mtime_ns", stamp), ("origin", origin)]))
    };
    Ok(Probed { path: path.into(), id, file, size })
}

/// Probes `path` on `host` in one shell call. `origin` names the node that holds it.
pub async fn identify(host: &Host, path: &str, origin: &str, sized: bool) -> Result<Probed, String> {
    let out = host.sh(&probe(path, sized)).await.map_err(|e| e.to_string())?;
    from_probe(&out, origin)
}
