//! Imports from a metasmith-style index YAML: each manifest path declares a property set, and
//! its instance id is the hash of that set. Content is never read.

use crate::index::{normalize, Entry};
use crate::provider::{Instance, Provider};
use crate::jobs::walk_files;
use anyhow::Context;
use baitan_proto::{instance_id, Id, PropSet};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

#[derive(Deserialize)]
struct IndexFile {
    /// Where manifest paths resolve from; default is the library root (the parent of `_metadata`).
    root: Option<PathBuf>,
    manifest: BTreeMap<String, Item>,
}

#[derive(Deserialize)]
struct Item {
    #[serde(rename = "type")]
    ty: Option<String>,
    #[serde(default)]
    props: PropSet,
    /// The slot this instance answers for a hit; default `out`.
    slot: Option<String>,
}

impl Provider {
    /// Registers every manifest path as a held instance whose id is `instance_id` of its declared
    /// set (`props` plus `type`). Returns the ids in manifest order.
    pub fn import(&self, index: &Path) -> anyhow::Result<Vec<Id>> {
        let file: IndexFile = serde_yaml::from_str(&std::fs::read_to_string(index).with_context(|| format!("reading {}", index.display()))?)?;
        let dir = index.canonicalize()?.parent().map(Path::to_path_buf).unwrap_or_default();
        let root = match file.root {
            Some(r) => dir.join(r),
            None if dir.file_name().is_some_and(|n| n == "_metadata") => dir.parent().unwrap_or(&dir).to_path_buf(),
            None => dir,
        };
        let mut ids = vec![];
        let mut st = self.inner.state.lock().unwrap();
        for (rel, item) in file.manifest {
            let path = root.join(&rel);
            anyhow::ensure!(path.exists(), "{} does not exist", path.display());
            let mut declared = item.props;
            if let Some(t) = item.ty {
                declared.insert("type".into(), t);
            }
            let id = instance_id(&declared);
            let listing = if path.is_dir() { walk_files(&path) } else { vec![] };
            st.index.drop_ids(std::slice::from_ref(&id));
            st.index.insert(Entry {
                pairs: normalize(declared.iter().map(|(k, v)| (k.clone(), v.clone()))),
                slots: BTreeMap::from([(item.slot.unwrap_or_else(|| "out".into()), id.clone())]),
                flight: None,
            });
            st.instances.insert(id.clone(), Instance { path, listing, props: Some(declared) });
            ids.push(id);
        }
        self.inner.persist(&st);
        Ok(ids)
    }
}
