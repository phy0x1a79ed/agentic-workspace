//! The plugin loader: scan the roots, read each manifest, apply the contract's rules.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use awm_proto::manifest::{
    parse_implements, LegacyServiceToml, Manifest, NeedTool, Needs, PluginMeta, ServiceDecl, ToolDecl,
    LEGACY_RUN, LEGACY_SERVICE_TOML,
};
use awm_proto::{valid_tool_name, Tier, MANIFEST_FILE};
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Plugin {
    pub name: String,
    pub version: String,
    pub dir: PathBuf,
    pub core: bool,
    pub legacy: bool,
    pub refresh: Option<String>,
    pub services: Vec<ServiceSpec>,
    pub tools: Vec<ToolSpec>,
    pub pages: Vec<PageSpec>,
    pub needs: Needs,
    /// A digest of the folder's files, names, sizes and mtimes. A reload restarts a plugin
    /// whose stamp moved (R144).
    pub stamp: String,
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
    /// Whether the egress proxy holds the service to its plugin's `needs.network`. A legacy
    /// folder declares nothing, so it is not held.
    pub egress_guarded: bool,
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
}

pub struct LoadInput<'a> {
    /// Whether the install config loads a plugin of this name.
    pub wants: &'a dyn Fn(&str) -> bool,
    pub core_roots: &'a [PathBuf],
    pub plugin_roots: &'a [PathBuf],
    pub page_roots: &'a [PathBuf],
    pub profiles: &'a [String],
    pub reserved: &'a [String],
    /// `{service: enabled}` from `<state>/enabled.json`.
    pub enabled: &'a HashMap<String, bool>,
    /// Tools each peer serves, by peer name.
    pub peer_tools: &'a BTreeMap<String, Vec<RemoteTool>>,
}

struct Candidate {
    dir: PathBuf,
    core: bool,
    legacy: bool,
    manifest: Manifest,
}

pub fn load(input: &LoadInput) -> LoadResult {
    let mut out = LoadResult::default();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut candidates = Vec::new();

    let roots = input.core_roots.iter().map(|r| (r, true)).chain(input.plugin_roots.iter().map(|r| (r, false)));
    for (root, core) in roots {
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
            let (manifest, legacy) = parsed;
            if !seen.insert(manifest.plugin.name.clone()) {
                out.shadowed.push(dir);
                continue;
            }
            candidates.push(Candidate { dir, core, legacy, manifest });
        }
    }

    let reserved: BTreeSet<&str> = input.reserved.iter().map(String::as_str).collect();
    // tool name → (owner plugin, owner is legacy)
    let mut tool_names: HashMap<String, (String, bool)> = HashMap::new();
    let mut service_names: HashMap<String, String> = HashMap::new();
    let mut plugins = Vec::new();

    for cand in candidates {
        let name = cand.manifest.plugin.name.clone();
        if !(input.wants)(&name) {
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
        if let Err(reason) = claim(&plugin, &reserved, &tool_names, &service_names) {
            out.refused.push(Refusal { plugin: name, dir: cand.dir, reason });
            continue;
        }
        for t in &plugin.tools {
            tool_names.entry(t.name.clone()).or_insert((name.clone(), plugin.legacy));
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
            if let Err(reason) = check_needs(&p.needs, &index) {
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

/// `None` when the folder is not a plugin. A legacy folder becomes a one-service manifest.
fn read_folder(dir: &Path) -> Option<Result<(Manifest, bool), String>> {
    let manifest_path = dir.join(MANIFEST_FILE);
    if manifest_path.is_file() {
        let text = match std::fs::read_to_string(&manifest_path) {
            Ok(t) => t,
            Err(e) => return Some(Err(format!("{MANIFEST_FILE}: {e}"))),
        };
        return Some(Manifest::parse(&text).map(|m| (m, false)).map_err(|e| format!("{MANIFEST_FILE}: {e}")));
    }
    if !dir.join(LEGACY_RUN).is_file() {
        return None;
    }
    let name = folder_name(dir);
    let legacy: LegacyServiceToml = match std::fs::read_to_string(dir.join(LEGACY_SERVICE_TOML)) {
        Ok(text) => match toml::from_str(&text) {
            Ok(l) => l,
            // Today's gateway reads a corrupt marker as marked-but-unmatched, never fail-open.
            Err(e) => return Some(Err(format!("{LEGACY_SERVICE_TOML}: {e}"))),
        },
        Err(_) => LegacyServiceToml::default(),
    };
    let tool = legacy.tool.clone().unwrap_or_else(|| name.clone());
    let manifest = Manifest {
        plugin: PluginMeta { name: name.clone(), version: "0.0.0".into(), refresh: None },
        placement: Default::default(),
        service: vec![ServiceDecl {
            name: name.clone(),
            path: ".".into(),
            run: LEGACY_RUN.into(),
            install: "install.sh".into(),
            profiles: legacy.profiles.clone(),
            archive: legacy.archive.clone(),
        }],
        tool: vec![ToolDecl {
            name: tool,
            major: 1,
            service: Some(name),
            services: vec![],
            tier: Tier::from_legacy(legacy.tier.as_deref()),
            implements: vec![],
        }],
        page: vec![],
        needs: Needs::default(),
    };
    Some(Ok((manifest, true)))
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

    if !m.needs.platform.is_empty() && !m.needs.platform.iter().any(|p| p == std::env::consts::OS) {
        return Err(format!("needs platform {:?}, this node is {}", m.needs.platform, std::env::consts::OS));
    }

    let mut services = Vec::new();
    for s in &m.service {
        let cwd = inside(&cand.dir, &s.path)?;
        if !cwd.join(&s.run).is_file() {
            return Err(format!("service {:?}: {} not found in {}", s.name, s.run, cwd.display()));
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
            egress_guarded: !cand.legacy,
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

    let mut pages = Vec::new();
    for p in &m.page {
        let dir = match &p.dir {
            Some(rel) => Some(inside(&cand.dir, rel)?),
            None => find_page(input.page_roots, &p.name),
        };
        match dir {
            Some(dir) => pages.push(PageSpec { name: p.name.clone(), dir, entry: p.entry.clone() }),
            None => warnings.push(format!("plugin {name}: page {:?} has no built bundle; not served", p.name)),
        }
    }

    Ok(Plugin {
        name: name.clone(),
        version: m.plugin.version.clone(),
        dir: cand.dir.clone(),
        core: cand.core,
        legacy: cand.legacy,
        refresh: m.plugin.refresh.clone(),
        services,
        tools,
        pages,
        needs: m.needs.clone(),
        stamp: stamp(&cand.dir),
    })
}

/// Folders a stamp skips: build output, caches and VCS state, which change without the
/// plugin changing.
pub const UNSTAMPED: [&str; 6] = [".git", "node_modules", "target", "__pycache__", ".venv", ".pytest_cache"];

fn stamp(dir: &Path) -> String {
    use std::hash::{Hash, Hasher};
    fn walk(dir: &Path, h: &mut std::collections::hash_map::DefaultHasher) {
        let Ok(entries) = std::fs::read_dir(dir) else { return };
        let mut entries: Vec<_> = entries.filter_map(|e| e.ok()).collect();
        entries.sort_by_key(|e| e.file_name());
        for e in entries {
            let name = e.file_name();
            let Ok(meta) = e.metadata() else { continue };
            if meta.is_dir() {
                if !UNSTAMPED.iter().any(|u| name == *u) {
                    name.hash(h);
                    walk(&e.path(), h);
                }
                continue;
            }
            name.hash(h);
            meta.len().hash(h);
            if let Ok(t) = meta.modified() {
                t.hash(h);
            }
        }
    }
    let mut h = std::collections::hash_map::DefaultHasher::new();
    walk(dir, &mut h);
    format!("{:016x}", h.finish())
}

fn find_page(roots: &[PathBuf], name: &str) -> Option<PathBuf> {
    roots
        .iter()
        .flat_map(|r| [r.join(name).join("dist"), r.join(name)])
        .find(|d| d.join("index.html").is_file())
}

fn claim(
    p: &Plugin,
    reserved: &BTreeSet<&str>,
    tool_names: &HashMap<String, (String, bool)>,
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
        // Legacy folders share a tool as today (cx, keep and reflection all publish
        // `reflection`). The hub refuses a verb two services both publish.
        if let Some((owner, legacy)) = tool_names.get(&t.name) {
            if !(p.legacy && *legacy) {
                return Err(format!("tool {:?} is already served by plugin {owner:?}", t.name));
            }
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
        if index.iter().any(|p| p.owner.name == t.name) {
            continue; // a shared legacy tool belongs to its first plugin
        }
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

/// A needed tool must exist at its major (R117 item 3). A contract may have no provider.
fn check_needs(needs: &Needs, index: &[Provider]) -> Result<(), String> {
    for NeedTool { name, major, verbs } in &needs.tool {
        check_verbs("tool", name, verbs)?;
        let Some(o) = owner(index, name) else {
            return Err(format!("needs tool {name:?}, which no plugin serves"));
        };
        if o.major != *major {
            return Err(format!("needs tool {name:?} at major {major}, plugin {:?} serves major {}", o.plugin, o.major));
        }
    }
    for NeedTool { name, verbs, .. } in &needs.contract {
        if !valid_tool_name(name) {
            return Err(format!("needs contract {name:?}, which is not a valid name"));
        }
        check_verbs("contract", name, verbs)?;
    }
    Ok(())
}

/// A need lists the verbs the plugin calls, since the gateway refuses every other (R116 item 3).
fn check_verbs(kind: &str, name: &str, verbs: &[String]) -> Result<(), String> {
    if verbs.is_empty() {
        return Err(format!("needs {kind} {name:?} but lists no verbs"));
    }
    match verbs.iter().find(|v| !awm_proto::valid_verb_name(v)) {
        Some(bad) => Err(format!("needs {kind} {name:?}: {bad:?} is not a verb name")),
        None => Ok(()),
    }
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

    fn legacy(root: &Path, name: &str, service_toml: Option<&str>) {
        let dir = root.join(name);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("run.sh"), "#!/bin/sh\n").unwrap();
        if let Some(t) = service_toml {
            fs::write(dir.join("service.toml"), t).unwrap();
        }
    }

    fn run(core: &Path, other: &Path, node: &str) -> LoadResult {
        run_with_peers(core, other, node, &BTreeMap::new())
    }

    fn run_with_peers(core: &Path, other: &Path, node: &str, peers: &BTreeMap<String, Vec<RemoteTool>>) -> LoadResult {
        let reserved = vec!["2fa".to_string()];
        let wants = move |p: &str| p != "elsewhere" || node == "altair";
        let enabled = HashMap::from([("off-svc".to_string(), false)]);
        load(&LoadInput {
            wants: &wants,
            core_roots: &[core.to_path_buf()],
            plugin_roots: &[other.to_path_buf()],
            page_roots: &[],
            profiles: &["gamebot".to_string()],
            reserved: &reserved,
            enabled: &enabled,
            peer_tools: peers,
        })
    }

    #[test]
    fn rules() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        legacy(&core, "journal", Some("tool = \"journal\"\ntier = \"on\"\n"));
        legacy(&core, "cx", Some("tool = \"reflection\"\n"));
        legacy(&core, "keep", Some("tool = \"reflection\"\n"));
        legacy(&core, "game", Some("profiles = [\"nope\"]\n"));
        legacy(&core, "off-svc", None);
        legacy(&core, "_hidden", None);
        fs::create_dir_all(core.join("not-a-plugin")).unwrap();
        plugin(&core, "provider",
            "[plugin]\nname='provider'\n[[service]]\nname='provider'\n[[tool]]\nname='prov'\nmajor=2\nimplements=['harness@1']\n");
        plugin(&core, "dependent",
            "[plugin]\nname='dependent'\n[[service]]\nname='dependent'\n[[needs.tool]]\nname='prov'\nmajor=2\nverbs=['get']\n[[needs.contract]]\nname='harness'\nmajor=1\nverbs=['send']\n");
        plugin(&other, "journal", "[plugin]\nname='journal'\n");
        plugin(&other, "dup-name", "[plugin]\nname='dup-name'\n[[service]]\nname='dup-name'\n[[tool]]\nname='prov'\n");
        plugin(&other, "takes-2fa", "[plugin]\nname='takes-2fa'\n[[service]]\nname='takes-2fa'\n[[tool]]\nname='2fa'\n");
        plugin(&other, "missing", "[plugin]\nname='missing'\n[[needs.tool]]\nname='nobody-serves'\nverbs=['get']\n");
        plugin(&other, "no-verbs", "[plugin]\nname='no-verbs'\n[[needs.tool]]\nname='prov'\nmajor=2\n");
        plugin(&other, "bad-verb", "[plugin]\nname='bad-verb'\n[[needs.contract]]\nname='harness'\nverbs=['send-it']\n");
        plugin(&other, "bad-host", "[plugin]\nname='bad-host'\n[needs]\nnetwork=['https://x.org']\n");
        plugin(&other, "wrong-major", "[plugin]\nname='wrong-major'\n[[needs.tool]]\nname='prov'\nmajor=1\nverbs=['get']\n");
        plugin(&other, "escape", "[plugin]\nname='escape'\n[[service]]\nname='escape'\npath='..'\n");
        plugin(&other, "elsewhere", "[plugin]\nname='elsewhere'\n[placement]\nnodes=['showtime']\n");
        plugin(&other, "bad-implements", "[plugin]\nname='bad-implements'\n[[service]]\nname='bad-implements'\n[[tool]]\nname='bi'\nimplements=['harness']\n");
        let two = core.join("ops");
        fs::create_dir_all(two.join("board")).unwrap();
        fs::create_dir_all(two.join("reporting")).unwrap();
        fs::write(two.join("board/run.sh"), "").unwrap();
        fs::write(two.join("reporting/run.sh"), "").unwrap();
        fs::write(two.join(MANIFEST_FILE), "[plugin]\nname='ops'\n[[service]]\nname='board'\npath='board'\n[[service]]\nname='reporting'\npath='reporting'\n[[tool]]\nname='operations'\nservices=['board','reporting']\n").unwrap();
        plugin(&other, "steals-reflection", "[plugin]\nname='steals-reflection'\n[[service]]\nname='steals-reflection'\n[[tool]]\nname='reflection'\n");
        plugin(&other, "windows-only", "[plugin]\nname='windows-only'\n[needs]\nplatform=['windows']\n");
        plugin(&other, "bad-toml", "[plugin]\nname='bad-toml'\nbogus=1\n");

        let r = run(&core, &other, "mizar");
        let names: Vec<_> = r.plugins.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, ["cx", "dependent", "game", "journal", "keep", "off-svc", "ops", "provider"]);
        let ops = r.plugins.iter().find(|p| p.name == "ops").unwrap();
        assert!(ops.services.iter().all(|s| s.tools == ["operations"]));
        let refused: BTreeMap<_, _> = r.refused.iter().map(|x| (x.plugin.as_str(), x.reason.as_str())).collect();
        assert!(refused["dup-name"].contains("already served by plugin \"provider\""));
        assert!(refused["takes-2fa"].contains("reserved"));
        assert!(refused["missing"].contains("\"nobody-serves\""));
        assert!(refused["wrong-major"].contains("major 1"));
        assert!(refused["no-verbs"].contains("lists no verbs"));
        assert!(refused["bad-verb"].contains("\"send-it\" is not a verb name"));
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
        plugin(&other, "b", "[plugin]\nname='b'\n[[needs.tool]]\nname='far'\nverbs=['get']\n");
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

    #[test]
    fn refused_provider_cascades() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        fs::create_dir_all(&core).unwrap();
        plugin(&other, "a", "[plugin]\nname='a'\n[[service]]\nname='a'\n[[tool]]\nname='a'\n[[needs.tool]]\nname='gone'\nverbs=['get']\n");
        plugin(&other, "b", "[plugin]\nname='b'\n[[needs.tool]]\nname='a'\nverbs=['get']\n");
        let r = run(&core, &other, "mizar");
        assert!(r.plugins.is_empty());
        assert_eq!(r.refused.len(), 2);
    }
}
