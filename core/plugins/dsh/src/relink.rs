//! Retarget `$DSH_HOME` symlinks into the old plugin folder's `config/`.
//!
//! The tracked harness config moved from `awm/services/dsh/config/` to `core/plugins/dsh/config/`,
//! which broke every link into it. A link whose target, resolved against its own folder, lies
//! in an old `awm/services/dsh/config/` is pointed at the same place under the new folder, kept
//! relative when it was relative. Nothing else in `home/` is touched, and no link is created.

use std::path::{Component, Path, PathBuf};

const OLD: [&str; 4] = ["awm", "services", "dsh", "config"];
const NEW: [&str; 4] = ["core", "plugins", "dsh", "config"];

/// `path` with `.` and `..` folded away, without touching the file system.
fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in path.components() {
        match c {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    out
}

/// The new target for an absolute, normalized old one, or None when it is not in an old config folder.
fn moved(target: &Path) -> Option<PathBuf> {
    let parts: Vec<Component> = target.components().collect();
    let at = parts.windows(OLD.len()).position(|w| w.iter().zip(OLD).all(|(c, o)| c.as_os_str() == o))?;
    let mut out: PathBuf = parts[..at].iter().collect();
    out.extend(NEW);
    out.extend(&parts[at + OLD.len()..]);
    Some(out)
}

/// `to` relative to the folder `from`, both absolute and normalized.
fn relative(to: &Path, from: &Path) -> PathBuf {
    let (t, f): (Vec<_>, Vec<_>) = (to.components().collect(), from.components().collect());
    let common = t.iter().zip(&f).take_while(|(a, b)| a == b).count();
    let mut out: PathBuf = f[common..].iter().map(|_| Component::ParentDir).collect();
    out.extend(&t[common..]);
    out
}

fn links(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let Ok(kind) = entry.file_type() else { continue };
        if kind.is_symlink() {
            out.push(entry.path());
        } else if kind.is_dir() {
            links(&entry.path(), out);
        }
    }
}

/// Retarget every link under `home` that points into an old config folder, and answer the
/// links changed. A link whose new target does not exist is left as it is.
pub fn relink(home: &Path) -> Vec<PathBuf> {
    let mut found = vec![];
    links(home, &mut found);
    let mut changed = vec![];
    for link in found {
        let Ok(old) = std::fs::read_link(&link) else { continue };
        let Some(folder) = link.parent().and_then(|p| p.canonicalize().ok()) else { continue };
        let resolved = normalize(&folder.join(&old));
        let Some(new) = moved(&resolved) else { continue };
        if !new.exists() {
            tracing::warn!("dsh: {} points into the old config folder, and {} does not exist; left as it is", link.display(), new.display());
            continue;
        }
        let target = if old.is_relative() { relative(&new, &folder) } else { new.clone() };
        let tmp = link.with_file_name(format!(".{}.relink", link.file_name().unwrap_or_default().to_string_lossy()));
        let _ = std::fs::remove_file(&tmp);
        let done = std::os::unix::fs::symlink(&target, &tmp).and_then(|_| std::fs::rename(&tmp, &link));
        match done {
            Ok(()) => {
                tracing::info!("dsh: retargeted {} to {}", link.display(), target.display());
                changed.push(link);
            }
            Err(e) => {
                let _ = std::fs::remove_file(&tmp);
                tracing::warn!("dsh: could not retarget {}: {e}", link.display());
            }
        }
    }
    changed
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn only_links_into_the_old_config_folder_move() {
        let tmp = tempfile::tempdir().unwrap();
        let ws = tmp.path().canonicalize().unwrap();
        let new_cfg = ws.join("core/plugins/dsh/config");
        std::fs::create_dir_all(new_cfg.join("agent-presets")).unwrap();
        std::fs::create_dir_all(new_cfg.join("profiles/web/plugins")).unwrap();
        std::fs::write(new_cfg.join("profiles/web/cordis.patch.yml"), "x: 1\n").unwrap();
        std::fs::write(ws.join("CLAUDE.md"), "synthetic\n").unwrap();
        let home = ws.join(".awm/services/dsh/home");
        std::fs::create_dir_all(home.join("profiles/web")).unwrap();
        std::fs::create_dir_all(home.join("sessions")).unwrap();
        std::fs::write(home.join("sessions/s.jsonl"), "{}\n").unwrap();

        // capella's shape: a relative link from home/ into the tracked config.
        symlink("../../../../awm/services/dsh/config/agent-presets", home.join(".agent-presets")).unwrap();
        symlink(ws.join("awm/services/dsh/config/profiles/web/cordis.patch.yml"), home.join("profiles/web/cordis.patch.yml")).unwrap();
        symlink("../../../../../../awm/services/dsh/config/profiles/web/plugins", home.join("profiles/web/plugins")).unwrap();
        symlink(ws.join("CLAUDE.md"), home.join("global-instructions.md")).unwrap();
        symlink("../../../../awm/services/dsh/config/gone", home.join("dangling")).unwrap();

        let mut changed = relink(&home);
        changed.sort();
        let mut want = vec![home.join(".agent-presets"), home.join("profiles/web/cordis.patch.yml"), home.join("profiles/web/plugins")];
        want.sort();
        assert_eq!(changed, want);

        assert_eq!(std::fs::read_link(home.join(".agent-presets")).unwrap(), PathBuf::from("../../../../core/plugins/dsh/config/agent-presets"));
        assert!(home.join(".agent-presets").is_dir());
        assert_eq!(std::fs::read_link(home.join("profiles/web/cordis.patch.yml")).unwrap(), new_cfg.join("profiles/web/cordis.patch.yml"));
        assert!(home.join("profiles/web/plugins").is_dir());
        assert_eq!(std::fs::read_link(home.join("global-instructions.md")).unwrap(), ws.join("CLAUDE.md"));
        assert_eq!(std::fs::read_link(home.join("dangling")).unwrap(), PathBuf::from("../../../../awm/services/dsh/config/gone"));
        assert_eq!(std::fs::read_to_string(home.join("sessions/s.jsonl")).unwrap(), "{}\n");
        assert!(relink(&home).is_empty(), "a second start changes nothing");
    }

    #[test]
    fn paths_fold_and_relate() {
        assert_eq!(normalize(Path::new("/a/b/../c/./d")), PathBuf::from("/a/c/d"));
        assert_eq!(moved(Path::new("/w/awm/services/dsh/config/x/y")), Some(PathBuf::from("/w/core/plugins/dsh/config/x/y")));
        assert_eq!(moved(Path::new("/w/awm/services/dsh/other")), None);
        assert_eq!(relative(Path::new("/w/core/c"), Path::new("/w/.awm/h")), PathBuf::from("../../core/c"));
    }
}
