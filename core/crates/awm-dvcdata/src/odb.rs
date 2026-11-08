//! The cache's objects: DVC 3's `files/md5` layout, file ids, and `.dir` manifests.

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use md5::{Digest, Md5};
use serde_json::{Map, Value};

use crate::pyjson;

/// The mode every cache object carries. A materialised file shares the object's inode.
pub const OBJECT_MODE: u32 = 0o444;
pub const DIR_SUFFIX: &str = ".dir";

/// Cache-relative path for an id: the first two characters shard, the rest name the file.
pub fn object_relpath(oid: &str) -> String {
    let (head, tail) = oid.split_at(oid.len().min(2));
    format!("files/md5/{head}/{tail}")
}

pub fn object_path(cache: &Path, oid: &str) -> PathBuf {
    cache.join(object_relpath(oid))
}

pub fn has_object(cache: &Path, oid: &str) -> bool {
    object_path(cache, oid).is_file()
}

pub fn md5_bytes(bytes: &[u8]) -> String {
    hex::encode(Md5::digest(bytes))
}

/// The plain md5 of a file's bytes, which is a DVC 3 file id.
pub fn md5_file(path: &Path) -> io::Result<String> {
    let mut f = fs::File::open(path)?;
    let mut h = Md5::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex::encode(h.finalize()))
}

/// Clear the write bits of a cache object. Always safe, and DVC sets them when it retries
/// a refused unlink.
pub fn protect(path: &Path) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if meta.is_file() && meta.permissions().mode() & 0o222 != 0 {
        fs::set_permissions(path, fs::Permissions::from_mode(meta.permissions().mode() & !0o222))?;
    }
    Ok(())
}

/// Write `bytes` as object `oid`, unless the cache holds it already.
pub fn save_bytes(cache: &Path, oid: &str, bytes: &[u8]) -> io::Result<PathBuf> {
    let dest = object_path(cache, oid);
    if dest.is_file() {
        return Ok(dest);
    }
    let parent = dest.parent().expect("sharded");
    fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(".{}.{}.tmp", dest.file_name().unwrap_or_default().to_string_lossy(), std::process::id()));
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
    }
    fs::set_permissions(&tmp, fs::Permissions::from_mode(OBJECT_MODE))?;
    fs::rename(&tmp, &dest)?;
    Ok(dest)
}

/// One leaf of a `.dir` manifest. `rest` keeps any key besides `md5` and `relpath`, so a
/// manifest re-serialises to its own bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub relpath: String,
    pub md5: String,
    pub rest: BTreeMap<String, Value>,
}

impl Entry {
    pub fn new(relpath: impl Into<String>, md5: impl Into<String>) -> Self {
        Entry { relpath: relpath.into(), md5: md5.into(), rest: BTreeMap::new() }
    }

    /// The entry without its path: what a three-way merge compares.
    pub fn value(&self) -> (String, BTreeMap<String, Value>) {
        (self.md5.clone(), self.rest.clone())
    }

    fn to_json(&self) -> Value {
        let mut m = Map::new();
        for (k, v) in &self.rest {
            m.insert(k.clone(), v.clone());
        }
        m.insert("md5".into(), Value::String(self.md5.clone()));
        m.insert("relpath".into(), Value::String(self.relpath.clone()));
        Value::Object(m)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ManifestError {
    #[error("manifest {0} is not in the cache")]
    Missing(String),
    #[error("cannot read manifest {oid}: {source}")]
    Io { oid: String, source: io::Error },
    #[error("manifest {oid} is not a list of entries: {detail}")]
    Malformed { oid: String, detail: String },
}

/// A `.dir` object: the files of one directory output, sorted by `relpath`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Manifest {
    pub entries: Vec<Entry>,
}

impl Manifest {
    pub fn from_entries(mut entries: Vec<Entry>) -> Self {
        entries.sort_by(|a, b| a.relpath.cmp(&b.relpath));
        Manifest { entries }
    }

    pub fn parse(bytes: &[u8], oid: &str) -> Result<Self, ManifestError> {
        let bad = |detail: String| ManifestError::Malformed { oid: oid.into(), detail };
        let v: Value = serde_json::from_slice(bytes).map_err(|e| bad(e.to_string()))?;
        let Value::Array(items) = v else { return Err(bad("not a JSON list".into())) };
        let mut entries = Vec::with_capacity(items.len());
        for item in items {
            let Value::Object(mut m) = item else { return Err(bad("an entry is not an object".into())) };
            let relpath = match m.remove("relpath") {
                Some(Value::String(s)) => s,
                _ => return Err(bad("an entry has no relpath".into())),
            };
            let md5 = match m.remove("md5") {
                Some(Value::String(s)) => s,
                _ => return Err(bad(format!("{relpath} has no md5"))),
            };
            entries.push(Entry { relpath, md5, rest: m.into_iter().collect() });
        }
        Ok(Manifest::from_entries(entries))
    }

    pub fn load(cache: &Path, oid: &str) -> Result<Self, ManifestError> {
        let bytes = fs::read(object_path(cache, oid)).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                ManifestError::Missing(oid.into())
            } else {
                ManifestError::Io { oid: oid.into(), source: e }
            }
        })?;
        Manifest::parse(&bytes, oid)
    }

    /// The bytes DVC writes: `json.dumps(sorted entries, sort_keys=True)`.
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut sorted = self.entries.clone();
        sorted.sort_by(|a, b| a.relpath.cmp(&b.relpath));
        pyjson::dumps(&Value::Array(sorted.iter().map(Entry::to_json).collect())).into_bytes()
    }

    pub fn oid(&self) -> String {
        format!("{}{DIR_SUFFIX}", md5_bytes(&self.to_bytes()))
    }

    pub fn save(&self, cache: &Path) -> io::Result<String> {
        let oid = self.oid();
        save_bytes(cache, &oid, &self.to_bytes())?;
        Ok(oid)
    }

    pub fn by_path(&self) -> BTreeMap<&str, &Entry> {
        self.entries.iter().map(|e| (e.relpath.as_str(), e)).collect()
    }

    /// The summed size of the leaf objects, or `None` when one is absent, as DVC's `du`.
    pub fn du(&self, cache: &Path) -> Option<u64> {
        self.entries.iter().map(|e| fs::metadata(object_path(cache, &e.md5)).ok().map(|m| m.len())).sum()
    }
}
