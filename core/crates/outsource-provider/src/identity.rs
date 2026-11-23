//! Material identity without reading content: a git repo is its HEAD commit, anything else its path and mtime.

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
/// `file` or `dir`, then `git`, HEAD and the first dirty line for a repo, else the mtime in ns.
fn probe(path: &str, sized: bool) -> String {
    let size = if sized { "du -sbD \"$p\" | cut -f1" } else { "echo 0" };
    format!(
        "set -e; p={}; [ -e \"$p\" ] || {{ echo \"$p does not exist\" >&2; exit 1; }}; readlink -f \"$p\"; {size}\n\
         if [ -f \"$p\" ]; then echo file; else echo dir; fi\n\
         if [ -e \"$p/.git\" ]; then echo git; git -C \"$p\" rev-parse HEAD; git -C \"$p\" status --porcelain | head -n1; else date -r \"$p\" +%s%N; fi\n",
        q(path)
    )
}

/// A repo with uncommitted changes keeps its commit id and adds a warning.
fn from_probe(out: &str, origin: &str, warnings: &mut Vec<String>) -> Result<Probed, String> {
    let mut lines = out.lines();
    let (Some(path), Some(size), Some(kind), Some(stamp)) = (lines.next(), lines.next(), lines.next(), lines.next()) else {
        return Err(format!("unreadable probe: {out:?}"));
    };
    let (file, size) = (kind == "file", size.parse().unwrap_or(0));
    let id = if stamp == "git" {
        let head = lines.next().unwrap_or_default();
        if lines.next().is_some_and(|l| !l.is_empty()) {
            warnings.push(format!("repo {path} has uncommitted changes; a prior cached result may be reused"));
        }
        instance_id(&props([("git", head), ("origin", origin)]))
    } else {
        instance_id(&props([("path", path), ("mtime_ns", stamp), ("origin", origin)]))
    };
    Ok(Probed { path: path.into(), id, file, size })
}

/// Probes `path` on `host` in one shell call. `origin` names the node that holds it.
pub async fn identify(host: &Host, path: &str, origin: &str, sized: bool, warnings: &mut Vec<String>) -> Result<Probed, String> {
    let out = host.sh(&probe(path, sized)).await.map_err(|e| e.to_string())?;
    from_probe(&out, origin, warnings)
}
