//! Pinned background jobs, which the daemon retires last.
//!
//! `jobs/pins.json` is a JSON array of job ids. The Claude CLI guards it with a `pins.json.lock`
//! directory that it treats as stale after five seconds; this takes the same lock. The lock wait
//! sleeps on tokio's timer, so these functions are safe on a runtime worker.

use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use serde_json::Value;

use super::ClaudeHome;

/// A lock directory older than this is stale and broken.
pub const STALE: Duration = Duration::from_secs(5);
const RETRY: Duration = Duration::from_millis(20);
const TRIES: usize = 100;

#[derive(Debug, thiserror::Error)]
pub enum PinsError {
    #[error("{} is not a JSON array; leaving it alone", .0.display())]
    NotArray(PathBuf),
    #[error("{}: {source}", .path.display())]
    Json { path: PathBuf, source: serde_json::Error },
    #[error("{} stayed locked", .0.display())]
    Locked(PathBuf),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

struct Lock(PathBuf);

impl Drop for Lock {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir(&self.0);
    }
}

fn lock_path(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(".lock");
    PathBuf::from(name)
}

async fn is_stale(lock: &Path) -> std::io::Result<bool> {
    let modified = tokio::fs::metadata(lock).await?.modified()?;
    Ok(SystemTime::now().duration_since(modified).is_ok_and(|age| age > STALE))
}

async fn locked(path: &Path) -> Result<Lock, PinsError> {
    let lock = lock_path(path);
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    for _ in 0..TRIES {
        match tokio::fs::create_dir(&lock).await {
            Ok(()) => return Ok(Lock(lock)),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e.into()),
        }
        if is_stale(&lock).await.unwrap_or(false) && tokio::fs::remove_dir(&lock).await.is_ok() {
            continue;
        }
        tokio::time::sleep(RETRY).await;
    }
    Err(PinsError::Locked(lock))
}

async fn read(path: &Path) -> Result<Vec<Value>, PinsError> {
    let text = match tokio::fs::read_to_string(path).await {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e.into()),
    };
    match serde_json::from_str(&text) {
        Ok(Value::Array(items)) => Ok(items),
        Ok(_) => Err(PinsError::NotArray(path.to_path_buf())),
        Err(source) => Err(PinsError::Json { path: path.to_path_buf(), source }),
    }
}

async fn write(path: &Path, jobs: &[Value]) -> Result<(), PinsError> {
    let mut name = path.file_name().unwrap_or_default().to_owned();
    name.push(format!(".{}.tmp", std::process::id()));
    let tmp = path.with_file_name(name);
    tokio::fs::write(&tmp, serde_json::to_string(jobs).expect("a JSON array serializes")).await?;
    tokio::fs::rename(&tmp, path).await?;
    Ok(())
}

/// The pinned job ids, or none when the file is missing or unreadable.
pub async fn pinned(home: &ClaudeHome) -> Vec<String> {
    read(&home.pins())
        .await
        .map(|items| items.into_iter().filter_map(|j| j.as_str().map(str::to_string)).collect())
        .unwrap_or_default()
}

/// Add `job` to the pin list, under the CLI's lock. A file that is not an array is left alone.
pub async fn pin(home: &ClaudeHome, job: &str) -> Result<(), PinsError> {
    let path = home.pins();
    let _lock = locked(&path).await?;
    let mut current = read(&path).await?;
    if !current.iter().any(|j| j.as_str() == Some(job)) {
        current.push(Value::from(job));
        write(&path, &current).await?;
    }
    Ok(())
}

/// Drop `jobs` from the pin list, under the CLI's lock, and return the ones that were pinned.
pub async fn unpin<I, S>(home: &ClaudeHome, jobs: I) -> Result<Vec<String>, PinsError>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let drop: Vec<String> = jobs.into_iter().map(|j| j.as_ref().to_string()).collect();
    let dropping = |j: &Value| j.as_str().is_some_and(|s| drop.iter().any(|d| d == s));
    let path = home.pins();
    let _lock = locked(&path).await?;
    let current = read(&path).await?;
    let dropped: Vec<String> =
        current.iter().filter(|j| dropping(j)).filter_map(|j| j.as_str().map(str::to_string)).collect();
    if !dropped.is_empty() {
        let kept: Vec<Value> = current.into_iter().filter(|j| !dropping(j)).collect();
        write(&path, &kept).await?;
    }
    Ok(dropped)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn home_with_pins(dir: &Path) -> ClaudeHome {
        let home = ClaudeHome::at(dir.join("claude"), dir.join("claude.json"));
        std::fs::create_dir_all(home.jobs()).unwrap();
        std::fs::write(home.pins(), "[]").unwrap();
        home
    }

    fn on_disk(home: &ClaudeHome) -> Value {
        serde_json::from_str(&std::fs::read_to_string(home.pins()).unwrap()).unwrap()
    }

    #[tokio::test]
    async fn pin_and_unpin_edit_the_array_under_the_cli_lock() {
        let dir = tempfile::tempdir().unwrap();
        let home = home_with_pins(dir.path());
        pin(&home, "aaaa1111").await.unwrap();
        pin(&home, "bbbb2222").await.unwrap();
        pin(&home, "aaaa1111").await.unwrap();
        assert_eq!(on_disk(&home), serde_json::json!(["aaaa1111", "bbbb2222"]));
        assert_eq!(unpin(&home, ["aaaa1111"]).await.unwrap(), ["aaaa1111"]);
        assert_eq!(pinned(&home).await, ["bbbb2222"]);
        assert!(unpin(&home, vec!["cccc3333".to_string()]).await.unwrap().is_empty());
        assert!(!home.jobs().join("pins.json.lock").exists());
    }

    #[tokio::test]
    async fn a_stale_lock_is_broken() {
        let dir = tempfile::tempdir().unwrap();
        let home = home_with_pins(dir.path());
        let lock = home.jobs().join("pins.json.lock");
        std::fs::create_dir(&lock).unwrap();
        let old = SystemTime::now() - Duration::from_secs(60);
        std::fs::File::open(&lock).unwrap().set_modified(old).unwrap();
        pin(&home, "aaaa1111").await.unwrap();
        assert_eq!(pinned(&home).await, ["aaaa1111"]);
    }

    #[tokio::test]
    async fn a_fresh_lock_is_waited_for_then_given_up_on() {
        let dir = tempfile::tempdir().unwrap();
        let home = home_with_pins(dir.path());
        std::fs::create_dir(home.jobs().join("pins.json.lock")).unwrap();
        let err = pin(&home, "aaaa1111").await.unwrap_err();
        assert!(matches!(err, PinsError::Locked(_)), "{err}");
        assert_eq!(on_disk(&home), serde_json::json!([]));
    }

    #[tokio::test]
    async fn a_file_that_is_not_an_array_is_left_alone() {
        let dir = tempfile::tempdir().unwrap();
        let home = home_with_pins(dir.path());
        std::fs::write(home.pins(), r#"{"x": 1}"#).unwrap();
        assert!(matches!(pin(&home, "aaaa1111").await, Err(PinsError::NotArray(_))));
        assert_eq!(std::fs::read_to_string(home.pins()).unwrap(), r#"{"x": 1}"#);
        assert!(pinned(&home).await.is_empty());
    }

    #[tokio::test]
    async fn a_missing_file_is_made() {
        let dir = tempfile::tempdir().unwrap();
        let home = ClaudeHome::at(dir.path().join("fresh"), dir.path().join("claude.json"));
        pin(&home, "aaaa1111").await.unwrap();
        assert_eq!(on_disk(&home), serde_json::json!(["aaaa1111"]));
    }
}
