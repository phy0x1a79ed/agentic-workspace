//! Registers material already on the provider's host as held instances: from a metasmith-style index YAML
//! (each manifest path declares a property set, whose hash is its instance id) or from the
//! `import` verb. Content is never read.

use crate::index::{normalize, Entry};
use crate::identity::identify;
use crate::jobs::q;
use crate::provider::{Instance, Provider, State};
use anyhow::Context;
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
    fn holds(&self, id: &Id, path: &Path) -> bool {
        self.instances.get(id).is_some_and(|n| n.path == path)
    }

    /// Holds `path`, whose `Host::files` are `files`, as instance `id`; with `declared` (a property
    /// set and the slot it answers for) a request whose pairs are a subset of it hits.
    fn hold(&mut self, id: &Id, path: PathBuf, files: &[(String, u64)], declared: Option<(&PropSet, String)>) {
        self.index.drop_ids(std::slice::from_ref(id));
        if let Some((props, slot)) = declared {
            self.index.insert(Entry {
                pairs: normalize(props.iter().map(|(k, v)| (k.clone(), v.clone()))),
                slots: BTreeMap::from([(slot, id.clone())]),
                flight: None,
            });
        }
        self.instances.insert(id.clone(), Instance::of(path, files, |f| !f.is_empty()));
    }
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
            if self.inner.state.lock().unwrap().holds(&id, &path) {
                continue;
            }
            let files = host.files(&path).await.with_context(|| format!("{} does not exist", path.display()))?;
            self.inner.state.lock().unwrap().hold(&id, path, &files, Some((&declared, item.slot.unwrap_or_else(|| "out".into()))));
        }
        self.inner.persist(&self.inner.state.lock().unwrap());
        Ok(ids)
    }

    /// The `import` verb: every path must exist (absolute, on the provider's host) or nothing is
    /// held. Returns the ids, the `at` facts naming each path.
    pub(crate) async fn import_materials(&self, items: &[ImportItem]) -> Result<Value, String> {
        let (host, me) = (&self.inner.host, self.inner.cfg.id.as_str());
        let mut staged = vec![];
        for item in items {
            if !Path::new(&item.path).is_absolute() {
                return Err(format!("{} is not an absolute path", item.path));
            }
            if item.ty.as_ref().is_some_and(PropSet::is_empty) {
                return Err(format!("{} has an empty type", item.path));
            }
            let probed = identify(host, &item.path, me, false).await.map_err(|_| format!("{} does not exist", item.path))?;
            let (path, id) = (PathBuf::from(probed.path), item.ty.as_ref().map_or(probed.id, instance_id));
            let files = if self.inner.state.lock().unwrap().holds(&id, &path) {
                None
            } else {
                Some(host.files(&path).await.map_err(|e| format!("{}: {e}", item.path))?)
            };
            staged.push((id, path, files, item.ty.as_ref()));
        }
        let mut st = self.inner.state.lock().unwrap();
        for (id, path, files, ty) in &staged {
            if let Some(files) = files {
                st.hold(id, path.clone(), files, ty.map(|t| (t, "out".into())));
            }
        }
        self.inner.persist(&st);
        let ids: Vec<&Id> = staged.iter().map(|s| &s.0).collect();
        let facts: Vec<Fact> = staged.iter().map(|(id, path, ..)| Fact::at_path(id, me, &path.to_string_lossy())).collect();
        Ok(json!({"ids": ids, "facts": facts}))
    }
}
