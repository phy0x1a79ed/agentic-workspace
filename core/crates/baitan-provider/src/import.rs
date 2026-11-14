//! Imports from a metasmith-style index YAML: each manifest path declares a property set, and
//! its instance id is the hash of that set. Content is never read.

use crate::index::{normalize, Entry};
use crate::provider::{Instance, Provider};
use crate::jobs::q;
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
    /// set (`props` plus `type`). Returns the ids in manifest order. `index` and its paths are on
    /// the provider's host.
    pub async fn import(&self, index: &Path) -> anyhow::Result<Vec<Id>> {
        let host = &self.inner.host;
        let out = host.sh(&format!("readlink -f {i} && cat {i}", i = q(&index.to_string_lossy()))).await;
        let out = out.with_context(|| format!("reading {}", index.display()))?;
        let (canonical, yaml) = out.split_once('\n').unwrap_or((&out, ""));
        let file: IndexFile = serde_yaml::from_str(yaml)?;
        let dir = Path::new(canonical).parent().map(Path::to_path_buf).unwrap_or_default();
        let root = match file.root {
            Some(r) => dir.join(r),
            None if dir.file_name().is_some_and(|n| n == "_metadata") => dir.parent().unwrap_or(&dir).to_path_buf(),
            None => dir,
        };
        let mut ids = vec![];
        for (rel, item) in file.manifest {
            let path = root.join(&rel);
            let mut declared = item.props;
            if let Some(t) = item.ty {
                declared.insert("type".into(), t);
            }
            let id = instance_id(&declared);
            ids.push(id.clone());
            if self.inner.state.lock().unwrap().instances.get(&id).is_some_and(|n| n.path == path) {
                continue;
            }
            let files = host.files(&path).await.with_context(|| format!("{} does not exist", path.display()))?;
            let mut st = self.inner.state.lock().unwrap();
            st.index.drop_ids(std::slice::from_ref(&id));
            st.index.insert(Entry {
                pairs: normalize(declared.iter().map(|(k, v)| (k.clone(), v.clone()))),
                slots: BTreeMap::from([(item.slot.unwrap_or_else(|| "out".into()), id.clone())]),
                flight: None,
            });
            st.instances.insert(id, Instance::of(path, &files, |f| !f.is_empty()));
        }
        self.inner.persist(&self.inner.state.lock().unwrap());
        Ok(ids)
    }
}
