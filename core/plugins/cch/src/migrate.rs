//! Copies the Python cch's promise store and the keeper's hold record into cch's own state dir,
//! once, before any verb is answered. Additive only: a source file is never edited or deleted, so
//! [`rollback`] can hand the promises back and reflection's sweep never declares one lost.

use std::io;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::paths::Paths;

/// The marker a migrated source dir carries: the ISO time and the destination path.
pub const MARKER: &str = "MIGRATED";

/// What one step of the migration did.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Moved {
    /// The files copied to the destination.
    pub copied: Vec<PathBuf>,
    /// The files left alone because the destination already had them.
    pub kept: Vec<PathBuf>,
    /// Why the step did nothing: `marked` (done before) or `absent` (no source).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub skipped: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// What [`migrate`] did, one step per legacy record.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Report {
    /// Reflection's `pending/` into `<service dir>/pending`, `unsent.jsonl` with it.
    pub promises: Moved,
    /// `services/keep/hold.json` into `<service dir>/hold.json`.
    pub hold: Moved,
}

/// Copies `from` to `to` keeping its mode, unless `to` exists. Through a temp file and a hard
/// link, so a crash never leaves a partial `to` and a file already there is never overwritten.
fn copy_new(from: &Path, to: &Path) -> io::Result<bool> {
    if std::fs::symlink_metadata(to).is_ok() {
        return Ok(false);
    }
    let dir = to.parent().unwrap_or(Path::new("."));
    let tmp = dir.join(format!(".migrate-{}", uuid::Uuid::new_v4().simple()));
    let linked = std::fs::copy(from, &tmp).and_then(|_| std::fs::hard_link(&tmp, to));
    let _ = std::fs::remove_file(&tmp);
    match linked {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => Ok(false),
        Err(e) => Err(e),
    }
}

/// Copies `from` over `to` keeping its mode, through a temp file renamed into place.
fn copy_over(from: &Path, to: &Path) -> io::Result<()> {
    let dir = to.parent().unwrap_or(Path::new("."));
    let tmp = dir.join(format!(".rollback-{}", uuid::Uuid::new_v4().simple()));
    let result = std::fs::copy(from, &tmp).and_then(|_| std::fs::rename(&tmp, to));
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

fn mark(source_dir: &Path, destination: &Path) -> io::Result<()> {
    let text = format!("{}\n{}\n", chrono::Utc::now().to_rfc3339(), destination.display());
    std::fs::write(source_dir.join(MARKER), text)
}

/// The regular files in `dir` other than the marker, sorted.
fn files(dir: &Path) -> io::Result<Vec<PathBuf>> {
    let mut out = Vec::new();
    for item in std::fs::read_dir(dir)? {
        let item = item?;
        if item.file_type()?.is_file() && item.file_name() != MARKER {
            out.push(item.path());
        }
    }
    out.sort();
    Ok(out)
}

fn move_files(sources: &[PathBuf], to_dir: &Path, out: &mut Moved) -> io::Result<()> {
    std::fs::create_dir_all(to_dir)?;
    for from in sources {
        let to = to_dir.join(from.file_name().expect("a listed file has a name"));
        if copy_new(from, &to)? {
            out.copied.push(to);
        } else {
            out.kept.push(to);
        }
    }
    Ok(())
}

fn promises(paths: &Paths) -> Moved {
    let (from, to) = (paths.legacy_promises_dir(), paths.promises_dir());
    let mut out = Moved::default();
    if !from.is_dir() {
        out.skipped = Some("absent");
    } else if from.join(MARKER).exists() {
        out.skipped = Some("marked");
    } else if let Err(e) = files(&from).and_then(|f| move_files(&f, &to, &mut out)).and_then(|()| mark(&from, &to)) {
        out.error = Some(format!("{} into {}: {e}", from.display(), to.display()));
    }
    out
}

fn hold(paths: &Paths) -> Moved {
    let (from, to) = (paths.legacy_hold_path(), paths.hold_path());
    let source_dir = from.parent().expect("the hold record sits in a dir").to_path_buf();
    let to_dir = to.parent().expect("the hold record sits in a dir").to_path_buf();
    let mut out = Moved::default();
    if !from.is_file() {
        out.skipped = Some("absent");
    } else if source_dir.join(MARKER).exists() {
        out.skipped = Some("marked");
    } else {
        let copied = std::fs::create_dir_all(&to_dir).and_then(|()| copy_new(&from, &to));
        match copied.and_then(|c| mark(&source_dir, &to).map(|()| c)) {
            Ok(true) => out.copied.push(to),
            Ok(false) => out.kept.push(to),
            Err(e) => out.error = Some(format!("{} to {}: {e}", from.display(), to.display())),
        }
    }
    out
}

/// Copies reflection's old `pending/` and the keep service's `hold.json` into cch's service dir,
/// keeping modes and never overwriting, then marks each source with a [`MARKER`]. A marked or
/// absent source is skipped. Run it at start, before any verb is answered.
pub async fn migrate(paths: &Paths) -> Report {
    let paths = paths.clone();
    let report = tokio::task::spawn_blocking(move || Report { promises: promises(&paths), hold: hold(&paths) })
        .await
        .unwrap_or_else(|e| {
            let failed = Moved { error: Some(format!("the migration panicked: {e}")), ..Moved::default() };
            Report { promises: failed.clone(), hold: failed }
        });
    for (what, step) in [("promises", &report.promises), ("hold", &report.hold)] {
        match &step.error {
            Some(e) => tracing::warn!("cch: migrating the {what} failed: {e}"),
            None if !step.copied.is_empty() => tracing::info!("cch: migrated {} {what} file(s)", step.copied.len()),
            None => {}
        }
    }
    report
}

/// The rollback to the Python cch: copies `<service dir>/pending/` back over reflection's
/// `pending/`, keeping modes, and removes that dir's [`MARKER`]. Answers the files it copied.
pub fn rollback(paths: &Paths) -> io::Result<Vec<PathBuf>> {
    let (from, to) = (paths.promises_dir(), paths.legacy_promises_dir());
    std::fs::create_dir_all(&to)?;
    let mut copied = Vec::new();
    if from.is_dir() {
        for file in files(&from)? {
            let dest = to.join(file.file_name().expect("a listed file has a name"));
            copy_over(&file, &dest)?;
            copied.push(dest);
        }
    }
    match std::fs::remove_file(to.join(MARKER)) {
        Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e),
        _ => {}
    }
    Ok(copied)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn mode(path: &Path) -> io::Result<u32> {
        Ok(std::fs::metadata(path)?.permissions().mode() & 0o7777)
    }

    fn setup() -> (tempfile::TempDir, Paths) {
        let tmp = tempfile::tempdir().unwrap();
        let paths = Paths::under(tmp.path());
        let legacy = paths.legacy_promises_dir();
        std::fs::create_dir_all(&legacy).unwrap();
        std::fs::write(legacy.join("4242.json"), r#"{"session_id": "00000000-0000-4000-8000-000000000001"}"#).unwrap();
        std::fs::set_permissions(legacy.join("4242.json"), std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::write(legacy.join("4242.done"), "1700000000000").unwrap();
        std::fs::write(legacy.join(crate::resume::UNSENT_FILE), "{\"kind\": \"made-up\"}\n").unwrap();
        let hold = paths.legacy_hold_path();
        std::fs::create_dir_all(hold.parent().unwrap()).unwrap();
        std::fs::write(&hold, r#"{"held": true}"#).unwrap();
        std::fs::set_permissions(&hold, std::fs::Permissions::from_mode(0o640)).unwrap();
        (tmp, paths)
    }

    fn names(paths: &[PathBuf]) -> Vec<String> {
        paths.iter().map(|p| p.file_name().unwrap().to_string_lossy().into_owned()).collect()
    }

    #[tokio::test]
    async fn the_first_run_copies_with_modes_and_marks_each_source() {
        let (_tmp, paths) = setup();
        let report = migrate(&paths).await;
        assert_eq!(report.promises.error, None);
        assert_eq!(names(&report.promises.copied), ["4242.done", "4242.json", "unsent.jsonl"]);
        assert_eq!(report.hold.copied, [paths.hold_path()]);
        let dest = paths.promises_dir();
        assert_eq!(std::fs::read(dest.join("4242.json")).unwrap(), std::fs::read(paths.legacy_promises_dir().join("4242.json")).unwrap());
        assert_eq!(mode(&dest.join("4242.json")).unwrap(), 0o600);
        assert_eq!(mode(&paths.hold_path()).unwrap(), 0o640);
        let marker = std::fs::read_to_string(paths.legacy_promises_dir().join(MARKER)).unwrap();
        let mut lines = marker.lines();
        assert!(chrono::DateTime::parse_from_rfc3339(lines.next().unwrap()).is_ok());
        assert_eq!(lines.next().unwrap(), dest.display().to_string());
        let hold_marker = paths.legacy_hold_path().parent().unwrap().join(MARKER);
        assert!(std::fs::read_to_string(hold_marker).unwrap().contains(&paths.hold_path().display().to_string()));
        assert!(!dest.join(MARKER).exists());
        assert!(paths.legacy_promises_dir().join("4242.json").exists(), "the source is never deleted");
        assert!(paths.legacy_hold_path().exists());
    }

    #[tokio::test]
    async fn the_rollback_round_trips() {
        let (_tmp, paths) = setup();
        migrate(&paths).await;
        std::fs::write(paths.promises_dir().join("4242.json"), r#"{"phase": "resume_sent"}"#).unwrap();
        std::fs::write(paths.promises_dir().join("6363.json"), "{}").unwrap();
        let copied = rollback(&paths).unwrap();
        assert_eq!(names(&copied), ["4242.done", "4242.json", "6363.json", "unsent.jsonl"]);
        let legacy = paths.legacy_promises_dir();
        assert!(!legacy.join(MARKER).exists());
        assert_eq!(std::fs::read_to_string(legacy.join("4242.json")).unwrap(), r#"{"phase": "resume_sent"}"#);
        assert!(legacy.join("6363.json").exists());
        let again = migrate(&paths).await;
        assert_eq!(again.promises.skipped, None);
        assert!(again.promises.copied.is_empty(), "every file is already at the destination");
        assert!(legacy.join(MARKER).exists());
    }

}
