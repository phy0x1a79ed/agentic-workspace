//! Cloud file stores (`buckets/`): one per `[bucket.<name>]`. Google Drive is the one backend.
//!
//! Paths are POSIX, relative to the bucket's root. The tree operations (`walk`, `get_tree`,
//! `put_tree`) are built here on a backend's `ls`, `stat`, `get`, `put` and `mkdir`.

use std::path::{Path, PathBuf};

use async_trait::async_trait;
use awm_plugin_sdk::VerbError;
use serde::Serialize;
use serde_json::{json, Value};

pub mod gdrive;

pub type Result<T> = std::result::Result<T, VerbError>;

/// The Python exception classes the bucket verbs raised.
pub fn not_found(msg: impl Into<String>) -> VerbError {
    VerbError::new("FileNotFoundError", msg)
}
pub fn not_a_dir(msg: impl Into<String>) -> VerbError {
    VerbError::new("NotADirectoryError", msg)
}
pub fn is_a_dir(msg: impl Into<String>) -> VerbError {
    VerbError::new("IsADirectoryError", msg)
}
pub fn exists(msg: impl Into<String>) -> VerbError {
    VerbError::new("FileExistsError", msg)
}

/// One file or folder. `path` is relative to the bucket root and is what the verbs take back.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct Entry {
    pub name: String,
    pub path: String,
    pub is_dir: bool,
    pub size: i64,
    pub mime: String,
    pub modified: String,
    pub id: String,
    pub web_url: String,
}

/// A downloaded file: `path` is the local file.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct Got {
    pub filename: String,
    pub mime: String,
    pub size: i64,
    pub path: String,
}

#[async_trait]
pub trait Bucket: Send + Sync {
    fn name(&self) -> &str;
    fn kind(&self) -> &str;

    /// The immediate children of the folder `path` (`""` is the root).
    async fn ls(&self, path: &str) -> Result<Vec<Entry>>;
    async fn stat(&self, path: &str) -> Result<Entry>;
    /// Stream one file into `dest`. A Google-native doc is exported.
    async fn get(&self, path: &str, dest: &Path) -> Result<Got>;
    /// Upload the local file `src` to `path`. Without `parents` a missing parent is not-found.
    async fn put(&self, path: &str, src: &Path, mime: Option<&str>, parents: bool) -> Result<Entry>;
    async fn rm(&self, path: &str) -> Result<()>;
    /// Create the folder and every missing parent. An existing folder is fine.
    async fn mkdir(&self, path: &str) -> Result<Entry>;
    async fn mv(&self, src: &str, dst: &str, overwrite: bool, parents: bool) -> Result<Entry>;
    async fn cp(&self, src: &str, dst: &str, overwrite: bool, parents: bool) -> Result<Entry>;
    async fn search(&self, query: &str, limit: usize, path: &str) -> Result<Vec<Entry>>;

    /// Fail unless the store answers for this identity now.
    async fn probe(&self) -> Result<()> {
        self.stat("").await.map(|_| ())
    }
}

/// A remote name as one local path segment.
pub fn local_segment(name: &str) -> String {
    let seg = name.replace('/', "_").replace('\0', "");
    if seg.is_empty() || seg == "." || seg == ".." {
        "_".into()
    } else {
        seg
    }
}

fn failure(path: &str, e: &VerbError) -> Value {
    json!({"path": path, "error": format!("{}: {}", e.error_class, e.message)})
}

/// Every entry below `path`, breadth first, and whether more than `limit` exist.
pub async fn walk(b: &dyn Bucket, path: &str, limit: usize) -> Result<(Vec<Entry>, bool)> {
    let mut out = vec![];
    let mut queue = std::collections::VecDeque::from([path.to_string()]);
    while let Some(next) = queue.pop_front() {
        for e in b.ls(&next).await? {
            if out.len() >= limit {
                return Ok((out, true));
            }
            if e.is_dir {
                queue.push_back(e.path.clone());
            }
            out.push(e);
        }
    }
    Ok((out, false))
}

/// Download the folder `path` as a local tree under `dest`. A failed file is listed and the
/// rest still download.
pub async fn get_tree(b: &dyn Bucket, path: &str, dest: &Path, limit: usize) -> Result<Value> {
    let root = b.stat(path).await?;
    if !root.is_dir {
        return Err(not_a_dir(format!("bucket {:?}: {path:?} is a file", b.name())));
    }
    let (entries, truncated) = walk(b, path, limit).await?;
    if truncated {
        return Err(VerbError::value(format!("bucket {:?}: {path:?} holds more than {limit} entries (pass a larger limit)", b.name())));
    }
    let base = path.trim_end_matches('/').rsplit('/').next().unwrap_or("");
    let name = [root.name.as_str(), base, b.name()].into_iter().find(|n| !n.is_empty()).unwrap_or("_").to_string();
    let local_root = dest.join(local_segment(&name));
    std::fs::create_dir(&local_root).map_err(|e| VerbError::runtime(e.to_string()))?;
    let prefix = root.path.trim_matches('/').to_string();
    let (mut count, mut size, mut failures) = (0, 0i64, vec![]);
    for e in entries {
        let mut rel = e.path.trim_matches('/').to_string();
        if !prefix.is_empty() {
            match rel.strip_prefix(&format!("{prefix}/")) {
                Some(r) => rel = r.to_string(),
                None => {
                    failures.push(failure(&e.path, &VerbError::value("outside the folder")));
                    continue;
                }
            }
        }
        let local: PathBuf = rel.split('/').fold(local_root.clone(), |p, s| p.join(local_segment(s)));
        let res: Result<()> = async {
            if e.is_dir {
                return std::fs::create_dir_all(&local).map_err(|x| VerbError::runtime(x.to_string()));
            }
            let dir = local.parent().unwrap().to_path_buf();
            std::fs::create_dir_all(&dir).map_err(|x| VerbError::runtime(x.to_string()))?;
            let got = b.get(&e.path, &dir).await?;
            if Path::new(&got.path) != local {
                std::fs::rename(&got.path, &local).map_err(|x| VerbError::runtime(x.to_string()))?;
            }
            count += 1;
            size += got.size;
            Ok(())
        }
        .await;
        if let Err(x) = res {
            failures.push(failure(&e.path, &x));
        }
    }
    Ok(json!({
        "filename": local_root.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
        "mime": "inode/directory", "size": size, "path": local_root.display().to_string(), "is_dir": true,
        "count": count, "failures": failures,
    }))
}

/// Upload the local folder `src` as the folder `path`. Existing files are overwritten.
pub async fn put_tree(b: &dyn Bucket, path: &str, src: &Path, parents: bool) -> Result<Value> {
    let target = path.trim_end_matches('/').to_string();
    let parent = target.rsplit_once('/').map(|(p, _)| p).unwrap_or("");
    if !parents && !parent.is_empty() && parent != "/" && !b.stat(parent).await?.is_dir {
        return Err(not_a_dir(format!("bucket {:?}: {parent:?} is a file", b.name())));
    }
    let root = if target.trim_matches('/').is_empty() { b.stat("").await? } else { b.mkdir(&target).await? };
    let (mut count, mut failures) = (0, vec![]);
    let mut stack = vec![(src.to_path_buf(), target.clone())];
    while let Some((dir, remote)) = stack.pop() {
        let mut items: Vec<_> = std::fs::read_dir(&dir).map_err(|e| VerbError::runtime(e.to_string()))?.flatten().collect();
        items.sort_by_key(|e| e.file_name());
        let (dirs, files): (Vec<_>, Vec<_>) = items.into_iter().partition(|e| e.path().is_dir());
        for d in dirs.iter().rev() {
            let r = format!("{remote}/{}", d.file_name().to_string_lossy());
            match b.mkdir(&r).await {
                Ok(_) => stack.push((d.path(), r)),
                Err(e) => failures.push(failure(&r, &e)),
            }
        }
        for f in files {
            let r = format!("{remote}/{}", f.file_name().to_string_lossy());
            match b.put(&r, &f.path(), None, false).await {
                Ok(_) => count += 1,
                Err(e) => failures.push(failure(&r, &e)),
            }
        }
    }
    Ok(json!({"entry": root, "count": count, "failures": failures}))
}

/// Build one configured bucket.
pub fn build(cfg: &crate::config::BucketConfig) -> anyhow::Result<std::sync::Arc<dyn Bucket>> {
    match cfg.kind.as_str() {
        "google_drive" => gdrive::GoogleDrive::create(cfg.clone()),
        other => anyhow::bail!("no bucket backend for kind {other:?}"),
    }
}
