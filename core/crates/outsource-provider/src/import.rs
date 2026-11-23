//! Registers material already on this host as held instances: from a metasmith-style index YAML
//! (each manifest path declares a property set, whose hash is its instance id) or from the
//! `import` verb. Content is never read.

use crate::index::{normalize, Entry};
use crate::provider::{Instance, Provider, State};
use crate::jobs::walk_files;
use anyhow::Context;
use crate::identity::material_id;
use outsource_proto::{instance_id, Fact, Id, ImportItem, PropSet};
use serde_json::{json, Value};
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

impl State {
    /// Holds `path` as instance `id`; with `declared` (a property set and the slot it answers
    /// for) a request whose pairs are a subset of it hits. Holding the same path again changes nothing.
    pub(crate) fn hold(&mut self, id: &Id, path: PathBuf, declared: Option<(&PropSet, String)>) {
        if self.instances.get(id).is_some_and(|n| n.path == path) {
            return;
        }
        let listing = if path.is_dir() { walk_files(&path) } else { vec![] };
        self.index.drop_ids(std::slice::from_ref(id));
        if let Some((props, slot)) = declared {
            self.index.insert(Entry {
                pairs: normalize(props.iter().map(|(k, v)| (k.clone(), v.clone()))),
                slots: BTreeMap::from([(slot, id.clone())]),
                flight: None,
            });
        }
        self.instances.insert(id.clone(), Instance::new(path, listing));
    }
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
            st.hold(&id, path, Some((&declared, item.slot.unwrap_or_else(|| "out".into()))));
            ids.push(id);
        }
        self.inner.persist(&st);
        Ok(ids)
    }

    /// The `import` verb: every path must exist (absolute, on this host) or nothing is held.
    /// Returns the ids, the `at` facts naming each path, and any repo warnings.
    pub(crate) fn import_materials(&self, items: &[ImportItem]) -> Result<Value, String> {
        let mut warnings = vec![];
        let mut staged = vec![];
        for item in items {
            if !Path::new(&item.path).is_absolute() {
                return Err(format!("{} is not an absolute path", item.path));
            }
            let path = Path::new(&item.path).canonicalize().map_err(|_| format!("{} does not exist", item.path))?;
            let id = match &item.ty {
                Some(t) if t.is_empty() => return Err(format!("{} has an empty type", item.path)),
                Some(t) => instance_id(t),
                None => material_id(&path, &self.inner.cfg.id, &mut warnings)?,
            };
            staged.push((id, path, item.ty.as_ref()));
        }
        let mut st = self.inner.state.lock().unwrap();
        for (id, path, ty) in &staged {
            st.hold(id, path.clone(), ty.map(|t| (t, "out".into())));
        }
        self.inner.persist(&st);
        let ids: Vec<&Id> = staged.iter().map(|s| &s.0).collect();
        let facts: Vec<Fact> = staged.iter().map(|(id, path, _)| Fact::at_path(id, &self.inner.cfg.id, &path.to_string_lossy())).collect();
        Ok(json!({"ids": ids, "facts": facts, "warnings": warnings}))
    }
}
