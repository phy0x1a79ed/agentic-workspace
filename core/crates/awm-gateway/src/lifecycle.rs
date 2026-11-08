//! Plugin lifecycle events (plugin-contract § Events): each load that changes the plugin set
//! publishes one `{plugin, event, version, root}` per change on the gateway's own emit topic
//! `awm/plugins`.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::loader::{stamp, LoadResult};

/// The service name a subscriber opens the gateway's own topics under: `/hub/emit/awm/<topic>`.
pub const SERVICE: &str = crate::gateway::NATIVE_TOOL;
pub const TOPIC: &str = "plugins";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Event {
    Installed,
    Updated,
    Enabled,
    Disabled,
    Removed,
    Refused,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Change {
    pub plugin: String,
    pub event: Event,
    /// The plugin's install stamp, or its pin's version for a core plugin.
    pub version: String,
    /// The plugin root its folder sits in.
    pub root: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Loaded,
    Skipped,
    Refused,
}

struct State<'a> {
    kind: Kind,
    version: &'a str,
    dir: &'a Path,
}

fn states(load: &LoadResult) -> BTreeMap<&str, State<'_>> {
    let version = |name: &str| load.stamps.get(name).map(String::as_str).unwrap_or_default();
    let mut out = BTreeMap::new();
    for (name, dir) in &load.skipped_dirs {
        out.insert(name.as_str(), State { kind: Kind::Skipped, version: version(name), dir });
    }
    for r in &load.refused {
        out.insert(r.plugin.as_str(), State { kind: Kind::Refused, version: version(&r.plugin), dir: &r.dir });
    }
    for p in &load.plugins {
        out.insert(p.name.as_str(), State { kind: Kind::Loaded, version: version(&p.name), dir: &p.dir });
    }
    out
}

/// One change per plugin whose state moved from `before` to `after`:
/// a new name is installed, a changed stamp is updated, excluded to included is enabled,
/// included to excluded is disabled, a name gone is removed, a new refusal is refused.
/// A refused plugin that loads now is installed, as a new one is.
pub fn diff(before: &LoadResult, after: &LoadResult) -> Vec<Change> {
    let (was, now) = (states(before), states(after));
    let mut out = Vec::new();
    let mut push = |name: &str, event: Event, s: &State| {
        let root = s.dir.parent().unwrap_or(s.dir).to_path_buf();
        out.push(Change { plugin: name.to_string(), event, version: s.version.to_string(), root });
    };
    for (name, old) in &was {
        if !now.contains_key(name) {
            push(name, Event::Removed, old);
        }
    }
    for (name, new) in &now {
        let event = match (was.get(name).map(|s| (s.kind, s.version)), new.kind) {
            (Some((Kind::Refused, _)), Kind::Refused) => continue,
            (_, Kind::Refused) => Event::Refused,
            (Some((Kind::Skipped, _)), Kind::Loaded) => Event::Enabled,
            (Some((Kind::Loaded | Kind::Refused, _)), Kind::Skipped) => Event::Disabled,
            (None | Some((Kind::Refused, _)), _) => Event::Installed,
            (Some((_, version)), _) if version != new.version => Event::Updated,
            _ => continue,
        };
        push(name, event, new);
    }
    out
}

/// Fill `load.stamps` for every plugin it names. A core plugin with a pin at
/// `<pins>/<name>.pin` takes the pin's `version`: its binary lives outside its folder.
pub fn stamp_all(load: &mut LoadResult, core_roots: &[PathBuf], pins: &Path) {
    let dirs: Vec<(String, PathBuf)> = load
        .plugins
        .iter()
        .map(|p| (p.name.clone(), p.dir.clone()))
        .chain(load.refused.iter().map(|r| (r.plugin.clone(), r.dir.clone())))
        .chain(load.skipped_dirs.iter().map(|(n, d)| (n.clone(), d.clone())))
        .collect();
    for (name, dir) in dirs {
        let pinned = core_roots.iter().any(|r| dir.starts_with(r)).then(|| pin_version(&pins.join(format!("{name}.pin")))).flatten();
        load.stamps.insert(name, pinned.unwrap_or_else(|| stamp(&dir)));
    }
}

/// The `version=` line of a pin file.
fn pin_version(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    text.lines().filter_map(|l| l.trim().strip_prefix("version=")).map(|v| v.trim().to_string()).find(|v| !v.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::loader::{Plugin, Refusal};
    use awm_proto::manifest::Needs;

    fn plugin(name: &str, root: &str) -> Plugin {
        Plugin {
            name: name.into(),
            version: "0.1.0".into(),
            dir: Path::new(root).join(name),
            core: false,
            refresh: None,
            services: vec![],
            tools: vec![],
            pages: vec![],
            needs: Needs::default(),
            install: vec![],
        }
    }

    /// `loaded`, `skipped` and `refused` as `(name, stamp)`, all under `/r`.
    fn load(loaded: &[(&str, &str)], skipped: &[(&str, &str)], refused: &[(&str, &str)]) -> LoadResult {
        let mut out = LoadResult::default();
        for (n, v) in loaded {
            out.plugins.push(plugin(n, "/r"));
            out.stamps.insert(n.to_string(), v.to_string());
        }
        for (n, v) in skipped {
            out.skipped.push(n.to_string());
            out.skipped_dirs.insert(n.to_string(), Path::new("/r").join(n));
            out.stamps.insert(n.to_string(), v.to_string());
        }
        for (n, v) in refused {
            out.refused.push(Refusal { plugin: n.to_string(), dir: Path::new("/r").join(n), reason: "no".into() });
            out.stamps.insert(n.to_string(), v.to_string());
        }
        out
    }

    fn events(changes: &[Change]) -> Vec<(&str, Event, &str)> {
        let mut v: Vec<_> = changes.iter().map(|c| (c.plugin.as_str(), c.event, c.version.as_str())).collect();
        v.sort_by_key(|e| e.0);
        v
    }

    #[test]
    fn each_change_is_one_event() {
        let before = load(
            &[("same", "s1"), ("bumped", "b1"), ("excluded", "e1"), ("breaks", "k1"), ("gone", "g1")],
            &[("included", "i1"), ("skip-gone", "x1")],
            &[("still-bad", "r1"), ("fixed", "f1"), ("dropped", "d1")],
        );
        let after = load(
            &[("same", "s1"), ("bumped", "b2"), ("included", "i1"), ("new", "n1"), ("fixed", "f2")],
            &[("excluded", "e1"), ("new-off", "o1")],
            &[("still-bad", "r2"), ("breaks", "k2"), ("fresh-bad", "z1")],
        );
        assert_eq!(
            events(&diff(&before, &after)),
            vec![
                ("breaks", Event::Refused, "k2"),
                ("bumped", Event::Updated, "b2"),
                ("dropped", Event::Removed, "d1"),
                ("excluded", Event::Disabled, "e1"),
                ("fixed", Event::Installed, "f2"),
                ("fresh-bad", Event::Refused, "z1"),
                ("gone", Event::Removed, "g1"),
                ("included", Event::Enabled, "i1"),
                ("new", Event::Installed, "n1"),
                ("new-off", Event::Installed, "o1"),
                ("skip-gone", Event::Removed, "x1"),
            ]
        );
        let c = diff(&before, &after).into_iter().find(|c| c.plugin == "new").unwrap();
        assert_eq!(c.root, PathBuf::from("/r"), "the root is the folder's plugin root");
        assert!(diff(&after, &after).is_empty(), "no change, no event");
    }

    #[test]
    fn a_core_plugin_with_a_pin_takes_its_version() {
        let t = tempfile::tempdir().unwrap();
        let (core, other, pins) = (t.path().join("core"), t.path().join("other"), t.path().join("pins"));
        for d in [core.join("pinned"), core.join("bare"), other.join("pinned-too")] {
            std::fs::create_dir_all(&d).unwrap();
            std::fs::write(d.join("run.sh"), "x").unwrap();
        }
        std::fs::create_dir_all(&pins).unwrap();
        std::fs::write(pins.join("pinned.pin"), "# a pin\nversion=plugin-abc123\nfile=x\n").unwrap();
        std::fs::write(pins.join("pinned-too.pin"), "version=not-core\n").unwrap();
        let mut load = LoadResult::default();
        load.plugins.push(plugin("pinned", core.to_str().unwrap()));
        load.plugins.push(plugin("bare", core.to_str().unwrap()));
        load.plugins.push(plugin("pinned-too", other.to_str().unwrap()));
        stamp_all(&mut load, std::slice::from_ref(&core), &pins);
        assert_eq!(load.stamps["pinned"], "plugin-abc123");
        assert_eq!(load.stamps["bare"], stamp(&core.join("bare")));
        assert_eq!(load.stamps["pinned-too"], stamp(&other.join("pinned-too")), "only a core plugin is pinned");
    }
}
