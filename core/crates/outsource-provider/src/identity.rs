//! Material identity without reading content: a git repo is its HEAD commit, anything else its path and mtime.

use crate::jobs::q;
use outsource_proto::{instance_id, props, Id};
use std::path::Path;

/// A shell script that prints what identity needs of `path`, on whichever host holds it: the
/// canonical path, then `git`, HEAD and the first dirty line for a repo, else the mtime in ns.
pub(crate) fn probe(path: &str) -> String {
    format!(
        "set -e; p={}; [ -e \"$p\" ] || {{ echo \"$p does not exist\" >&2; exit 1; }}; readlink -f \"$p\"\n\
         if [ -e \"$p/.git\" ]; then echo git; git -C \"$p\" rev-parse HEAD; git -C \"$p\" status --porcelain | head -n1; else date -r \"$p\" +%s%N; fi\n",
        q(path)
    )
}

/// The canonical path and id from `probe`'s output. A repo with uncommitted changes keeps its
/// commit id and adds a warning.
pub(crate) fn from_probe(out: &str, origin: &str, warnings: &mut Vec<String>) -> Result<(String, Id), String> {
    let mut lines = out.lines();
    let (Some(path), Some(kind)) = (lines.next(), lines.next()) else { return Err(format!("unreadable probe: {out:?}")) };
    if kind != "git" {
        return Ok((path.into(), instance_id(&props([("path", path), ("mtime_ns", kind), ("origin", origin)]))));
    }
    let head = lines.next().unwrap_or_default();
    if lines.next().is_some_and(|l| !l.is_empty()) {
        warnings.push(format!("repo {path} has uncommitted changes; a prior cached result may be reused"));
    }
    Ok((path.into(), instance_id(&props([("git", head), ("origin", origin)]))))
}

/// The id of `path` on this host. `origin` names the node that holds it.
pub fn material_id(path: &Path, origin: &str, warnings: &mut Vec<String>) -> Result<Id, String> {
    let out = std::process::Command::new("sh").arg("-c").arg(probe(&path.to_string_lossy())).output().map_err(|e| format!("sh: {e}"))?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().to_string());
    }
    Ok(from_probe(&String::from_utf8_lossy(&out.stdout), origin, warnings)?.1)
}
