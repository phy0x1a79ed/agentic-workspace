//! The plugin loader: scan the roots, read each manifest, apply the contract's rules.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use awm_proto::manifest::{parse_implements, Manifest, NeedTool, Needs};
use awm_proto::{valid_tool_name, Tier, MANIFEST_FILE};
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Plugin {
    pub name: String,
    pub version: String,
    pub dir: PathBuf,
    pub core: bool,
    pub refresh: Option<String>,
    pub services: Vec<ServiceSpec>,
    pub tools: Vec<ToolSpec>,
    pub pages: Vec<PageSpec>,
    pub needs: Needs,
    /// The `install` hooks of its services, in manifest order. Empty: no install step.
    pub install: Vec<PathBuf>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ServiceSpec {
    pub name: String,
    pub plugin: String,
    pub cwd: PathBuf,
    pub run: String,
    pub enabled: bool,
    pub tools: Vec<String>,
    /// The plugin's `needs.secrets`: the only secrets its processes get (R116 item 1).
    pub secrets: Vec<String>,
    /// The service's `[[service.archive]]` tables, for the search plugin's sources.
    pub archive: Vec<serde_json::Value>,
    /// The service's `[[service.front]]` tables, for auth's TLS listeners (ports/auth.md C2).
    pub front: Vec<awm_proto::manifest::FrontDecl>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ToolSpec {
    /// Unique across the fleet. Its services publish functions as `<name>_<verb>`.
    pub name: String,
    pub major: u32,
    pub services: Vec<String>,
    pub tier: Tier,
    pub implements: Vec<(String, u32)>,
    pub plugin: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct PageSpec {
    pub name: String,
    pub dir: PathBuf,
    pub entry: String,
}

/// One row of the name table: the plugin that owns a tool, and the node it runs on.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Owner {
    pub name: String,
    pub major: u32,
    pub plugin: String,
    pub peer: Option<String>,
}

/// A tool another node serves, as its `/hub/table` reports it.
#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct RemoteTool {
    pub name: String,
    pub major: u32,
    pub plugin: String,
    #[serde(default)]
    pub implements: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Refusal {
    pub plugin: String,
    pub dir: PathBuf,
    pub reason: String,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct LoadResult {
    pub plugins: Vec<Plugin>,
    pub refused: Vec<Refusal>,
    /// Plugins the install config does not load on this node.
    pub skipped: Vec<String>,
    /// Folders a same-named folder in an earlier root shadows.
    pub shadowed: Vec<PathBuf>,
    pub warnings: Vec<String>,
    /// The archive tables of every plugin the roots hold, loaded, skipped or refused, so a
    /// reader such as search knows a kind that only another node serves.
    pub archives: Vec<ArchiveDecl>,
    /// The folder of each skipped plugin, by name.
    #[serde(skip)]
    pub skipped_dirs: BTreeMap<String, PathBuf>,
    /// The lifecycle version of each loaded, refused and skipped plugin, by name. The gateway
    /// fills it ([`crate::lifecycle::stamp_all`]).
    #[serde(skip)]
    pub stamps: BTreeMap<String, String>,
}

/// One service's `[[service.archive]]` tables.
#[derive(Debug, Clone, Serialize)]
pub struct ArchiveDecl {
    pub plugin: String,
    pub service: String,
    pub archive: Vec<toml::Value>,
}

pub struct LoadInput<'a> {
    /// Whether the install config loads a plugin of this name.
    pub wants: &'a dyn Fn(&str) -> bool,
    /// Whether it runs this service of a loaded plugin: `(plugin, service)`.
    pub wants_service: &'a dyn Fn(&str, &str) -> bool,
    pub core_roots: &'a [PathBuf],
    pub plugin_roots: &'a [PathBuf],
    pub profiles: &'a [String],
    pub reserved: &'a [String],
    /// `{service: enabled}` from `<state>/enabled.json`.
    pub enabled: &'a HashMap<String, bool>,
    /// Tools each peer serves, by peer name.
    pub peer_tools: &'a BTreeMap<String, Vec<RemoteTool>>,
    /// The singletons this node reaches through their hosts' doors (fleet.md R89). A need for
    /// one holds whether or not the door answers now.
    pub doors: &'a [String],
    /// `{tool: host}` for each singleton another node hosts. Its name is taken there, so no
    /// plugin here may claim it (fleet.md R117.2, R117.3).
    pub elsewhere: &'a BTreeMap<String, String>,
    /// `{plugin: (install stamp, reason)}` for each plugin whose install hook failed. It stays
    /// refused until its folder's stamp moves.
    pub failed_installs: &'a BTreeMap<String, (String, String)>,
}

struct Candidate {
    dir: PathBuf,
    core: bool,
    manifest: Manifest,
}

pub fn load(input: &LoadInput) -> LoadResult {
    let mut out = LoadResult::default();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut candidates = Vec::new();

    let roots = input.core_roots.iter().map(|r| (r, true)).chain(input.plugin_roots.iter().map(|r| (r, false)));
    for (root, core) in roots {
        // A root may be absent, such as `core/plugins` on a tree that has no Rust plugin yet.
        if !root.exists() {
            continue;
        }
        let Ok(entries) = std::fs::read_dir(root) else {
            out.warnings.push(format!("plugin root {} is not a directory; skipped", root.display()));
            continue;
        };
        let mut dirs: Vec<PathBuf> = entries
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.is_dir() && valid_folder_name(p))
            .collect();
        dirs.sort();
        for dir in dirs {
            let parsed = match read_folder(&dir) {
                None => continue,
                Some(Err(reason)) => {
                    out.refused.push(Refusal { plugin: folder_name(&dir), dir, reason });
                    continue;
                }
                Some(Ok(parsed)) => parsed,
            };
            let manifest = parsed;
            if !seen.insert(manifest.plugin.name.clone()) {
                out.shadowed.push(dir);
                continue;
            }
            out.archives.extend(manifest.service.iter().filter(|s| !s.archive.is_empty()).map(|s| ArchiveDecl {
                plugin: manifest.plugin.name.clone(),
                service: s.name.clone(),
                archive: s.archive.clone(),
            }));
            candidates.push(Candidate { dir, core, manifest });
        }
    }

    let reserved: BTreeSet<&str> = input.reserved.iter().map(String::as_str).collect();
    // tool name → owner plugin
    let mut tool_names: HashMap<String, String> = HashMap::new();
    let mut service_names: HashMap<String, String> = HashMap::new();
    let mut plugins = Vec::new();

    for cand in candidates {
        let name = cand.manifest.plugin.name.clone();
        if !(input.wants)(&name) {
            out.skipped_dirs.insert(name.clone(), cand.dir);
            out.skipped.push(name);
            continue;
        }
        if cand.manifest.placement.is_some() {
            out.warnings.push(format!("plugin {name}: [placement] is ignored, the install config places plugins (fleet.md R140)"));
        }
        let plugin = match build_plugin(&cand, input, &mut out.warnings) {
            Ok(p) => p,
            Err(reason) => {
                out.refused.push(Refusal { plugin: name, dir: cand.dir, reason });
                continue;
            }
        };
        if let Some((_, reason)) = input.failed_installs.get(&name).filter(|(at, _)| *at == stamp(&cand.dir)) {
            out.refused.push(Refusal { plugin: name, dir: cand.dir, reason: reason.clone() });
            continue;
        }
        if let Err(reason) = claim(&plugin, &reserved, input.elsewhere, &tool_names, &service_names) {
            out.refused.push(Refusal { plugin: name, dir: cand.dir, reason });
            continue;
        }
        for t in &plugin.tools {
            tool_names.insert(t.name.clone(), name.clone());
        }
        for s in &plugin.services {
            service_names.insert(s.name.clone(), name.clone());
        }
        plugins.push(plugin);
    }

    // A refused provider can strand its dependents, so resolve to a fixed point.
    loop {
        let index = table(&plugins, input.peer_tools);
        let mut refused_now = Vec::new();
        for (i, p) in plugins.iter().enumerate() {
            if let Err(reason) = check_needs(&p.needs, &index, input.doors) {
                refused_now.push((i, reason));
            }
        }
        if refused_now.is_empty() {
            break;
        }
        for (i, reason) in refused_now.into_iter().rev() {
            let p = plugins.remove(i);
            out.refused.push(Refusal { plugin: p.name, dir: p.dir, reason });
        }
    }
    out.plugins = plugins;
    out
}

fn valid_folder_name(p: &Path) -> bool {
    p.file_name()
        .and_then(|n| n.to_str())
        .is_some_and(|n| !n.is_empty() && !n.starts_with('.') && !n.starts_with('_'))
}

fn folder_name(dir: &Path) -> String {
    dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// `None` when the folder holds no manifest, so it is not a plugin.
fn read_folder(dir: &Path) -> Option<Result<Manifest, String>> {
    let manifest_path = dir.join(MANIFEST_FILE);
    if !manifest_path.is_file() {
        return None;
    }
    Some(
        std::fs::read_to_string(&manifest_path)
            .map_err(|e| e.to_string())
            .and_then(|text| Manifest::parse(&text).map_err(|e| e.to_string()))
            .map_err(|e| format!("{MANIFEST_FILE}: {e}")),
    )
}

/// Join `rel` onto `base` and refuse a result outside `base`.
fn inside(base: &Path, rel: &str) -> Result<PathBuf, String> {
    let joined = base.join(rel);
    let canon = joined.canonicalize().map_err(|e| format!("{}: {e}", joined.display()))?;
    let base = base.canonicalize().map_err(|e| format!("{}: {e}", base.display()))?;
    if !canon.starts_with(&base) {
        return Err(format!("{rel:?} leaves the plugin folder"));
    }
    Ok(canon)
}

fn build_plugin(cand: &Candidate, input: &LoadInput, warnings: &mut Vec<String>) -> Result<Plugin, String> {
    let m = &cand.manifest;
    let name = &m.plugin.name;
    if !valid_tool_name(name) {
        return Err(format!("plugin name {name:?} must be letters, digits and hyphens"));
    }

    if let Some(bad) = m.needs.network.iter().find_map(|e| awm_proto::NetworkEntry::parse(e).err()) {
        return Err(format!("needs {bad}"));
    }

    let retired = m.needs.retired_keys();
    if !retired.is_empty() {
        warnings.push(format!(
            "plugin {}: retired needs keys ignored: {}. A plugin may call any tool, and every need is hard (R117 item 8)",
            m.plugin.name,
            retired.join(", ")
        ));
    }
    if !m.needs.platform.is_empty() && !m.needs.platform.iter().any(|p| p == std::env::consts::OS) {
        return Err(format!("needs platform {:?}, this node is {}", m.needs.platform, std::env::consts::OS));
    }

    let mut services = Vec::new();
    let mut install = Vec::new();
    for s in &m.service {
        let cwd = inside(&cand.dir, &s.path)?;
        let mut front_names = std::collections::BTreeSet::new();
        for f in &s.front {
            f.check().map_err(|e| format!("service {:?}: {e}", s.name))?;
            if !front_names.insert(f.name.as_str()) {
                return Err(format!("service {:?}: two fronts named {:?}", s.name, f.name));
            }
        }
        if !cwd.join(&s.run).is_file() {
            return Err(format!("service {:?}: {} not found in {}", s.name, s.run, cwd.display()));
        }
        if let Some(hook) = &s.install {
            let path = inside(&cand.dir, &format!("{}/{hook}", s.path)).map_err(|e| format!("service {:?}: install {e}", s.name))?;
            if !path.is_file() {
                return Err(format!("service {:?}: install hook {hook} is not a file", s.name));
            }
            install.push((s.name.clone(), path));
        }
        let by_profile = match &s.profiles {
            None => true,
            Some(want) => want.iter().any(|p| input.profiles.contains(p)),
        };
        let enabled = input.enabled.get(&s.name).copied().unwrap_or(by_profile);
        services.push(ServiceSpec {
            name: s.name.clone(),
            plugin: name.clone(),
            cwd,
            run: s.run.clone(),
            enabled,
            tools: vec![],
            secrets: m.needs.secrets.clone(),
            archive: s.archive.iter().filter_map(|t| serde_json::to_value(t).ok()).collect(),
            front: s.front.clone(),
        });
    }

    let mut tools = Vec::new();
    for t in &m.tool {
        if !valid_tool_name(&t.name) {
            return Err(format!("tool {:?} must be letters, digits and hyphens", t.name));
        }
        let served_by: Vec<String> = match (&t.service, t.services.is_empty(), services.len()) {
            (Some(_), false, _) => return Err(format!("tool {:?} sets both service and services", t.name)),
            (Some(s), true, _) => vec![s.clone()],
            (None, false, _) => t.services.clone(),
            (None, true, 1) => vec![services[0].name.clone()],
            (None, true, n) => return Err(format!("tool {:?} names no service and the plugin has {n}", t.name)),
        };
        for service in &served_by {
            let Some(svc) = services.iter_mut().find(|s| &s.name == service) else {
                return Err(format!("tool {:?} names service {service:?}, which the plugin does not declare", t.name));
            };
            svc.tools.push(t.name.clone());
        }
        let mut implements = Vec::new();
        for raw in &t.implements {
            implements.push(parse_implements(raw).ok_or_else(|| {
                format!("tool {:?}: implements {raw:?} is not <contract>@<major>", t.name)
            })?);
        }
        tools.push(ToolSpec {
            name: t.name.clone(),
            major: t.major,
            services: served_by,
            tier: t.tier,
            implements,
            plugin: name.clone(),
        });
    }

    // A service the install config leaves off this node takes the tools only it serves along.
    let off: Vec<String> = services.iter().filter(|s| !(input.wants_service)(name, &s.name)).map(|s| s.name.clone()).collect();
    services.retain(|s| !off.contains(&s.name));
    install.retain(|(s, _)| !off.contains(s));
    for t in &mut tools {
        t.services.retain(|s| !off.contains(s));
    }
    tools.retain(|t| !t.services.is_empty());

    let mut pages = Vec::new();
    for p in &m.page {
        let rel = p.dir.clone().unwrap_or_else(|| format!("pages/{}/dist", p.name));
        if Path::new(&rel).is_absolute() || Path::new(&rel).components().any(|c| c == std::path::Component::ParentDir) {
            return Err(format!("page {:?}: {rel:?} leaves the plugin folder", p.name));
        }
        // A page that is not built yet leaves the rest of its plugin running.
        if !cand.dir.join(&rel).join(&p.entry).is_file() {
            warnings.push(format!("plugin {name}: page {:?} has no built bundle at {rel}; not served", p.name));
            continue;
        }
        pages.push(PageSpec { name: p.name.clone(), dir: inside(&cand.dir, &rel)?, entry: p.entry.clone() });
    }

    Ok(Plugin {
        name: name.clone(),
        version: m.plugin.version.clone(),
        dir: cand.dir.clone(),
        core: cand.core,
        refresh: m.plugin.refresh.clone(),
        services,
        tools,
        pages,
        needs: m.needs.clone(),
        install: install.into_iter().map(|(_, path)| path).collect(),
    })
}

/// Folders an install copy and the install stamp skip: build output, caches and VCS state.
pub const UNSTAMPED: [&str; 6] = [".git", "node_modules", "target", "__pycache__", ".venv", ".pytest_cache"];

/// A digest of the names, sizes and mtimes of a plugin folder's files, less [`UNSTAMPED`]. The
/// install hook runs again when it moves (plugin-contract § Runtime).
pub fn stamp(dir: &Path) -> String {
    fn walk(dir: &Path, out: &mut Vec<u8>) {
        let Ok(entries) = std::fs::read_dir(dir) else { return };
        let mut entries: Vec<_> = entries.filter_map(|e| e.ok()).collect();
        entries.sort_by_key(|e| e.file_name());
        for e in entries {
            let name = e.file_name();
            let Ok(meta) = e.metadata() else { continue };
            if meta.is_dir() && UNSTAMPED.iter().any(|u| name == *u) {
                continue;
            }
            out.extend_from_slice(name.as_encoded_bytes());
            out.push(0);
            if meta.is_dir() {
                walk(&e.path(), out);
                out.push(b'/');
                continue;
            }
            out.extend_from_slice(&meta.len().to_le_bytes());
            let mtime = meta.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).unwrap_or_default();
            out.extend_from_slice(&mtime.as_nanos().to_le_bytes());
        }
    }
    let mut listing = Vec::new();
    walk(dir, &mut listing);
    // A name-based uuid is a stable digest: the stamp is kept on disk across gateway builds.
    uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_OID, &listing).simple().to_string()
}

fn claim(
    p: &Plugin,
    reserved: &BTreeSet<&str>,
    elsewhere: &BTreeMap<String, String>,
    tool_names: &HashMap<String, String>,
    service_names: &HashMap<String, String>,
) -> Result<(), String> {
    let mut own = BTreeSet::new();
    for t in &p.tools {
        if !own.insert(&t.name) {
            return Err(format!("tool {:?} is declared twice", t.name));
        }
        if reserved.contains(t.name.as_str()) && !p.core {
            return Err(format!("tool {:?} is reserved for core plugins", t.name));
        }
        if let Some(host) = elsewhere.get(&t.name) {
            return Err(format!("tool {:?} is already served by {host}", t.name));
        }
        if let Some(owner) = tool_names.get(&t.name) {
            return Err(format!("tool {:?} is already served by plugin {owner:?}", t.name));
        }
    }
    for s in &p.services {
        if let Some(owner) = service_names.get(&s.name) {
            return Err(format!("service {:?} is already run by plugin {owner:?}", s.name));
        }
    }
    Ok(())
}

/// A row of the name table, with the contracts the tool implements.
pub struct Provider {
    pub owner: Owner,
    pub implements: Vec<(String, u32)>,
}

/// The name table: every tool this node's plugins serve, then each peer's. A local owner wins.
pub fn table(plugins: &[Plugin], peers: &BTreeMap<String, Vec<RemoteTool>>) -> Vec<Provider> {
    let mut index: Vec<Provider> = Vec::new();
    for t in plugins.iter().flat_map(|p| p.tools.iter()) {
        index.push(Provider {
            owner: Owner { name: t.name.clone(), major: t.major, plugin: t.plugin.clone(), peer: None },
            implements: t.implements.clone(),
        });
    }
    for (peer, tools) in peers {
        for t in tools {
            if index.iter().any(|p| p.owner.name == t.name && p.owner.peer.is_none()) {
                continue;
            }
            index.push(Provider {
                owner: Owner { name: t.name.clone(), major: t.major, plugin: t.plugin.clone(), peer: Some(peer.clone()) },
                implements: t.implements.iter().filter_map(|r| parse_implements(r)).collect(),
            });
        }
    }
    index
}

/// The owner of a tool name: this node's plugin, else the first peer that serves it.
pub fn owner<'a>(index: &'a [Provider], name: &str) -> Option<&'a Owner> {
    index.iter().find(|p| p.owner.name == name).map(|p| &p.owner)
}

/// Every provider of a contract at a major, on this node and its peers.
pub fn providers(index: &[Provider], contract: &str, major: u32) -> Vec<Owner> {
    index
        .iter()
        .filter(|p| p.implements.iter().any(|(c, m)| c == contract && *m == major))
        .map(|p| p.owner.clone())
        .collect()
}

/// A needed tool must exist at its major (R117 item 3), or be a singleton behind a door. A
/// needed contract must have a provider at its major, here or on a peer (R117 item 8).
fn check_needs(needs: &Needs, index: &[Provider], doors: &[String]) -> Result<(), String> {
    for NeedTool { name, major, .. } in &needs.tool {
        let Some(o) = owner(index, name) else {
            if doors.contains(name) {
                continue;
            }
            return Err(format!("needs tool {name:?}, which no plugin serves"));
        };
        if o.major != *major {
            return Err(format!("needs tool {name:?} at major {major}, plugin {:?} serves major {}", o.plugin, o.major));
        }
    }
    for NeedTool { name, major, .. } in &needs.contract {
        if !valid_tool_name(name) {
            return Err(format!("needs contract {name:?}, which is not a valid name"));
        }
        if providers(index, name, *major).is_empty() {
            return Err(format!("needs contract \"{name}@{major}\", which no plugin implements"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn plugin(root: &Path, name: &str, manifest: &str) {
        let dir = root.join(name);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("run.sh"), "#!/bin/sh\n").unwrap();
        fs::write(dir.join(MANIFEST_FILE), manifest).unwrap();
    }

    /// A one-service plugin whose tool is `tool`, with extra `[[service]]` keys.
    fn single(root: &Path, name: &str, tool: &str, service_keys: &str) {
        plugin(root, name, &format!("[plugin]\nname='{name}'\n[[service]]\nname='{name}'\n{service_keys}[[tool]]\nname='{tool}'\n"));
    }

    fn run(core: &Path, other: &Path, node: &str) -> LoadResult {
        run_with_peers(core, other, node, &BTreeMap::new())
    }

    fn run_with_peers(core: &Path, other: &Path, node: &str, peers: &BTreeMap<String, Vec<RemoteTool>>) -> LoadResult {
        run_with(core, other, node, peers, &[])
    }

    fn run_with(core: &Path, other: &Path, node: &str, peers: &BTreeMap<String, Vec<RemoteTool>>, doors: &[String]) -> LoadResult {
        let reserved = vec!["auth".to_string()];
        let wants = move |p: &str| p != "elsewhere" || node == "altair";
        let enabled = HashMap::from([("off-svc".to_string(), false)]);
        let elsewhere: BTreeMap<String, String> =
            if node == "alcor" { BTreeMap::new() } else { BTreeMap::from([("ibkr".to_string(), "alcor".to_string())]) };
        load(&LoadInput {
            wants: &wants,
            wants_service: &|plugin, service| !(plugin == "ops" && service == "reporting"),
            core_roots: &[core.to_path_buf()],
            plugin_roots: &[other.to_path_buf()],
            profiles: &["gamebot".to_string()],
            reserved: &reserved,
            enabled: &enabled,
            peer_tools: peers,
            doors,
            elsewhere: &elsewhere,
            failed_installs: &BTreeMap::new(),
        })
    }

    /// A singleton loads from any root on its host. Every other node refuses its name, and a
    /// trust name stays core-only everywhere.
    #[test]
    fn a_singleton_name_is_taken_by_its_host() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        fs::create_dir_all(&core).unwrap();
        single(&other, "ibkr", "ibkr", "");
        single(&other, "takes-auth", "auth", "");
        let none = BTreeMap::new();
        let r = run_with(&core, &other, "alcor", &none, &[]);
        assert_eq!(r.plugins.iter().map(|p| p.name.as_str()).collect::<Vec<_>>(), ["ibkr"]);
        assert!(r.refused.iter().any(|f| f.plugin == "takes-auth" && f.reason.contains("reserved")));
        let r = run_with(&core, &other, "mizar", &none, &[]);
        assert!(r.refused.iter().any(|f| f.plugin == "ibkr" && f.reason.contains("already served by alcor")));
    }

    #[test]
    fn rules() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        single(&core, "journal", "journal", "");
        single(&core, "cx", "reflection", "");
        single(&core, "game", "game", "profiles=['nope']\n");
        single(&core, "off-svc", "off-svc", "");
        single(&core, "_hidden", "hidden", "");
        fs::create_dir_all(core.join("not-a-plugin")).unwrap();
        fs::write(core.join("not-a-plugin/run.sh"), "").unwrap();
        fs::write(core.join("not-a-plugin/service.toml"), "tool = 'x'\n").unwrap();
        plugin(&core, "provider",
            "[plugin]\nname='provider'\n[[service]]\nname='provider'\n[[tool]]\nname='prov'\nmajor=2\nimplements=['harness@1']\n");
        plugin(&core, "dependent",
            "[plugin]\nname='dependent'\n[[service]]\nname='dependent'\n[[needs.tool]]\nname='prov'\nmajor=2\n[[needs.contract]]\nname='harness'\nmajor=1\n");
        plugin(&other, "journal", "[plugin]\nname='journal'\n");
        plugin(&other, "dup-name", "[plugin]\nname='dup-name'\n[[service]]\nname='dup-name'\n[[tool]]\nname='prov'\n");
        plugin(&other, "takes-auth", "[plugin]\nname='takes-auth'\n[[service]]\nname='takes-auth'\n[[tool]]\nname='auth'\n");
        plugin(&other, "missing", "[plugin]\nname='missing'\n[[needs.tool]]\nname='nobody-serves'\n");
        plugin(&other, "lists-verbs", "[plugin]\nname='lists-verbs'\n[[needs.tool]]\nname='prov'\nmajor=2\nverbs=['get']\noptional=true\n");
        plugin(&other, "bad-contract", "[plugin]\nname='bad-contract'\n[[needs.contract]]\nname='har.ness'\n");
        plugin(&other, "bad-host", "[plugin]\nname='bad-host'\n[needs]\nnetwork=['https://x.org']\n");
        plugin(&other, "wrong-major", "[plugin]\nname='wrong-major'\n[[needs.tool]]\nname='prov'\nmajor=1\n");
        plugin(&other, "escape", "[plugin]\nname='escape'\n[[service]]\nname='escape'\npath='..'\n");
        plugin(&other, "elsewhere", "[plugin]\nname='elsewhere'\n[placement]\nnodes=['showtime']\n");
        plugin(&other, "bad-implements", "[plugin]\nname='bad-implements'\n[[service]]\nname='bad-implements'\n[[tool]]\nname='bi'\nimplements=['harness']\n");
        let two = core.join("ops");
        fs::create_dir_all(two.join("board")).unwrap();
        fs::create_dir_all(two.join("reporting")).unwrap();
        fs::write(two.join("board/run.sh"), "").unwrap();
        fs::write(two.join("reporting/run.sh"), "").unwrap();
        fs::write(two.join(MANIFEST_FILE), "[plugin]\nname='ops'\n[[service]]\nname='board'\npath='board'\n[[service]]\nname='reporting'\npath='reporting'\n[[tool]]\nname='operations'\nservices=['board','reporting']\n[[tool]]\nname='ops-report'\nservice='reporting'\n").unwrap();
        plugin(&other, "steals-reflection", "[plugin]\nname='steals-reflection'\n[[service]]\nname='steals-reflection'\n[[tool]]\nname='reflection'\n");
        plugin(&other, "windows-only", "[plugin]\nname='windows-only'\n[needs]\nplatform=['windows']\n");
        plugin(&other, "bad-toml", "[plugin]\nname='bad-toml'\nbogus=1\n");

        let r = run(&core, &other, "mizar");
        let names: Vec<_> = r.plugins.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, ["cx", "dependent", "game", "journal", "off-svc", "ops", "provider", "lists-verbs"]);
        let ops = r.plugins.iter().find(|p| p.name == "ops").unwrap();
        assert_eq!(ops.services.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["board"], "the config leaves reporting off");
        assert_eq!(ops.services[0].tools, ["operations"]);
        assert_eq!(ops.tools.iter().map(|t| t.name.as_str()).collect::<Vec<_>>(), ["operations"], "a tool only reporting serves goes with it");
        let refused: BTreeMap<_, _> = r.refused.iter().map(|x| (x.plugin.as_str(), x.reason.as_str())).collect();
        assert!(refused["dup-name"].contains("already served by plugin \"provider\""));
        assert!(refused["takes-auth"].contains("reserved"));
        assert!(refused["missing"].contains("\"nobody-serves\""));
        assert!(refused["wrong-major"].contains("major 1"));
        assert_eq!(r.warnings.iter().filter(|w| w.contains("lists-verbs")).collect::<Vec<_>>(),
            ["plugin lists-verbs: retired needs keys ignored: verbs, optional. A plugin may call any tool, and every need is hard (R117 item 8)"],
            "a retired key warns once and refuses nothing");
        assert!(refused["bad-contract"].contains("not a valid name"));
        assert!(refused["bad-host"].contains("network entry \"https://x.org\""), "{}", refused["bad-host"]);
        assert!(refused["escape"].contains("leaves the plugin folder"));
        assert!(refused["bad-implements"].contains("<contract>@<major>"));
        assert!(refused.contains_key("bad-toml"));
        assert!(refused["windows-only"].contains("platform"));
        assert!(refused["steals-reflection"].contains("already served"));
        assert_eq!(r.skipped, ["elsewhere"]);
        assert_eq!(r.shadowed.len(), 1);

        let by = |n: &str| r.plugins.iter().find(|p| p.name == n).unwrap();
        assert_eq!(by("cx").tools[0].name, "reflection");
        assert!(!by("game").services[0].enabled);
        assert!(!by("off-svc").services[0].enabled);
        assert!(by("journal").services[0].enabled);
        let index = table(&r.plugins, &BTreeMap::new());
        assert_eq!(owner(&index, "prov").unwrap().plugin, "provider");
        assert_eq!(owner(&index, "reflection").unwrap().plugin, "cx");
        assert_eq!(providers(&index, "harness", 1)[0].name, "prov");
        assert!(providers(&index, "harness", 2).is_empty());

        let r = run(&core, &other, "altair");
        assert!(r.plugins.iter().any(|p| p.name == "elsewhere"));
        assert!(r.warnings.iter().any(|w| w.contains("R140")));
    }

    #[test]
    fn a_need_resolves_on_a_peer() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        fs::create_dir_all(&core).unwrap();
        plugin(&other, "b", "[plugin]\nname='b'\n[[needs.tool]]\nname='far'\n");
        let peers = BTreeMap::from([(
            "altair".to_string(),
            vec![RemoteTool { name: "far".into(), major: 1, plugin: "farp".into(), implements: vec!["harness@1".into()] }],
        )]);
        let r = run_with_peers(&core, &other, "mizar", &peers);
        assert_eq!(r.plugins.len(), 1, "{:?}", r.refused);
        let index = table(&r.plugins, &peers);
        assert_eq!(owner(&index, "far").unwrap().peer.as_deref(), Some("altair"));
        assert_eq!(providers(&index, "harness", 1).len(), 1);
    }

    /// A singleton behind a door counts as served with no answer from the door, so a door
    /// that is down refuses no plugin. A need for any other missing tool still refuses.
    #[test]
    fn a_need_for_a_door_singleton_holds_with_the_door_down() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        fs::create_dir_all(&core).unwrap();
        plugin(&other, "ops", "[plugin]\nname='ops'\n[[service]]\nname='ops'\n[[tool]]\nname='ops'\n[[needs.tool]]\nname='vault'\n");
        plugin(&other, "board", "[plugin]\nname='board'\n[[needs.tool]]\nname='ops'\n");
        let r = run_with(&core, &other, "mizar", &BTreeMap::new(), &["vault".to_string()]);
        assert!(r.refused.is_empty(), "{:?}", r.refused);
        assert_eq!(r.plugins.len(), 2);
        let r = run(&core, &other, "mizar");
        assert_eq!(r.refused.len(), 2, "with no door, vault is unknown and ops strands board");
    }

    /// A contract need is a hard dependency: no provider refuses the plugin, and a provider on
    /// a peer satisfies it. A door singleton implements nothing the loader can see.
    #[test]
    fn a_contract_need_with_no_provider_refuses() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        fs::create_dir_all(&core).unwrap();
        plugin(&other, "reflection", "[plugin]\nname='reflection'\n[[needs.contract]]\nname='harness'\nmajor=1\n");
        plugin(&other, "channel", "[plugin]\nname='channel'\n[[service]]\nname='channel'\n[[tool]]\nname='channel'\n[[needs.contract]]\nname='harness'\nmajor=2\n");
        let r = run_with(&core, &other, "mizar", &BTreeMap::new(), &["harness".to_string()]);
        let refused: BTreeMap<_, _> = r.refused.iter().map(|x| (x.plugin.as_str(), x.reason.as_str())).collect();
        assert!(refused["reflection"].contains("\"harness@1\", which no plugin implements"), "{refused:?}");
        assert!(refused["channel"].contains("harness@2"));

        let peers = BTreeMap::from([(
            "altair".to_string(),
            vec![RemoteTool { name: "harness-cc".into(), major: 1, plugin: "cch".into(), implements: vec!["harness@1".into()] }],
        )]);
        let r = run_with_peers(&core, &other, "mizar", &peers);
        assert_eq!(r.plugins.iter().map(|p| p.name.as_str()).collect::<Vec<_>>(), ["reflection"], "{:?}", r.refused);
        assert!(r.refused.iter().any(|f| f.plugin == "channel"), "a provider at another major does not count");
    }

    /// Every plugin's archive tables are listed, whether it loaded, was skipped or was refused.
    /// A shadowed folder's are not.
    #[test]
    fn archives_list_every_plugin_in_the_roots() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        let table = |kind: &str| format!("[[service.archive]]\nname='{kind}'\nmode='index'\nexport='x_export'\n");
        single(&core, "journal", "journal", &table("journals"));
        single(&core, "elsewhere", "elsewhere", &table("far"));
        single(&core, "kb", "kb", &format!("{}[[needs.tool]]\nname='nobody-serves'\n", table("papers")));
        single(&other, "journal", "journal-2", &table("shadowed"));
        let r = run(&core, &other, "mizar");
        assert_eq!(r.skipped, ["elsewhere"]);
        assert!(r.refused.iter().any(|f| f.plugin == "kb"));
        let kinds: Vec<(&str, &str, &str)> = r.archives.iter()
            .flat_map(|a| a.archive.iter().map(move |t| (a.plugin.as_str(), a.service.as_str(), t["name"].as_str().unwrap())))
            .collect();
        assert_eq!(kinds, [("elsewhere", "elsewhere", "far"), ("journal", "journal", "journals"), ("kb", "kb", "papers")]);
    }

    /// A service's front tables reach its spec. A bad one refuses the plugin.
    #[test]
    fn fronts_pass_through_and_a_bad_one_refuses() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        let front = |body: &str| format!("[[service.front]]\n{body}\n");
        single(&core, "sci", "sci", &front("port=12201\nupstream='http://127.0.0.1:12200/'\nrewrite_origin=true\npaths={'/_signin'='http://127.0.0.1:12210/'}"));
        single(&core, "far", "far", &front("port=12301\nupstream='http://10.0.0.5:80/'"));
        single(&core, "twice", "twice", &format!("{}{}", front("port=1\nupstream='http://localhost:2/'"), front("port=3\nupstream='http://[::1]:4/'")));
        single(&core, "stray", "stray", &front("port=1\nupstream='http://127.0.0.1:2/'\nhost='x'"));
        single(&core, "slash", "slash", &front("port=1\nupstream='http://127.0.0.1:2/'\npaths={'x'='http://127.0.0.1:3/'}"));
        fs::create_dir_all(&other).unwrap();
        let r = run(&core, &other, "mizar");
        let sci = r.plugins.iter().flat_map(|p| &p.services).find(|s| s.name == "sci").expect("sci loads");
        assert_eq!(sci.front.len(), 1);
        let f = &sci.front[0];
        assert_eq!((f.name.as_str(), f.port, f.upstream.as_str(), f.rewrite_origin), ("gui", 12201, "http://127.0.0.1:12200/", true));
        assert_eq!(f.paths.get("/_signin").map(String::as_str), Some("http://127.0.0.1:12210/"));
        let mut refused: Vec<&str> = r.refused.iter().map(|f| f.plugin.as_str()).collect();
        refused.sort();
        assert_eq!(refused, ["far", "slash", "stray", "twice"]);
    }

    /// A plugin that needs a contract whose only provider is refused goes with it.
    #[test]
    fn a_refused_contract_provider_cascades() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        fs::create_dir_all(&core).unwrap();
        plugin(&other, "cch", "[plugin]\nname='cch'\n[[service]]\nname='cch'\n[[tool]]\nname='harness-cc'\nimplements=['harness@1']\n[[needs.tool]]\nname='gone'\n");
        plugin(&other, "reflection", "[plugin]\nname='reflection'\n[[needs.contract]]\nname='harness'\nmajor=1\n");
        let r = run(&core, &other, "mizar");
        assert!(r.plugins.is_empty(), "{:?}", r.plugins.iter().map(|p| &p.name).collect::<Vec<_>>());
        assert_eq!(r.refused.len(), 2);
    }

    #[test]
    fn refused_provider_cascades() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        fs::create_dir_all(&core).unwrap();
        plugin(&other, "a", "[plugin]\nname='a'\n[[service]]\nname='a'\n[[tool]]\nname='a'\n[[needs.tool]]\nname='gone'\n");
        plugin(&other, "b", "[plugin]\nname='b'\n[[needs.tool]]\nname='a'\n");
        let r = run(&core, &other, "mizar");
        assert!(r.plugins.is_empty());
        assert_eq!(r.refused.len(), 2);
    }
}
