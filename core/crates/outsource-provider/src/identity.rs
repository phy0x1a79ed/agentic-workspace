//! Material identity without reading content: a git repo is its HEAD commit, anything else its path and mtime.

use outsource_proto::{instance_id, props, Id};
use std::path::Path;

/// `path` must be canonical. `origin` names the node that holds it. A repo with uncommitted
/// changes keeps its commit id and adds a warning.
pub fn material_id(path: &Path, origin: &str, warnings: &mut Vec<String>) -> Result<Id, String> {
    let shown = path.to_string_lossy();
    if path.join(".git").exists() {
        let head = git(path, &["rev-parse", "HEAD"])?;
        if !git(path, &["status", "--porcelain"])?.is_empty() {
            warnings.push(format!("repo {shown} has uncommitted changes; a prior cached result may be reused"));
        }
        return Ok(instance_id(&props([("git", head.as_str()), ("origin", origin)])));
    }
    use std::os::unix::fs::MetadataExt;
    let md = std::fs::metadata(path).map_err(|e| format!("{shown}: {e}"))?;
    let mtime_ns = (md.mtime() as i128 * 1_000_000_000 + md.mtime_nsec() as i128).to_string();
    Ok(instance_id(&props([("path", &*shown), ("mtime_ns", &mtime_ns), ("origin", origin)])))
}

fn git(dir: &Path, args: &[&str]) -> Result<String, String> {
    let out = std::process::Command::new("git").arg("-C").arg(dir).args(args).output().map_err(|e| format!("git: {e}"))?;
    if !out.status.success() {
        return Err(format!("git {} in {}: {}", args.join(" "), dir.display(), String::from_utf8_lossy(&out.stderr).trim()));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}
