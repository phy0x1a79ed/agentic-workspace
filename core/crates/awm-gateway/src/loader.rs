//! The plugin loader: scan the roots, read each manifest, apply the contract's rules.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use awm_proto::manifest::{
    parse_implements, LegacyServiceToml, Manifest, NeedTool, Needs, PluginMeta, ServiceDecl, ToolDecl,
    LEGACY_RUN, LEGACY_SERVICE_TOML,
};
use awm_proto::{legacy_tool_uuid, valid_tool_name, Tier, Trust, MANIFEST_FILE};
use serde::Serialize;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize)]
pub struct Plugin {
    pub name: String,
    pub version: String,
    pub dir: PathBuf,
    pub core: bool,
    pub legacy: bool,
    pub trust: Trust,
    pub refresh: Option<String>,
    pub services: Vec<ServiceSpec>,
    pub tools: Vec<ToolSpec>,
    pub pages: Vec<PageSpec>,
    pub needs: Needs,
    pub resolved: Resolved,
}

#[derive(Debug, Clone, Serialize)]
pub struct ServiceSpec {
    pub name: String,
    pub plugin: String,
    pub cwd: PathBuf,
    pub run: String,
    pub enabled: bool,
    pub tools: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ToolSpec {
    /// The public name, after any rename in the install config.
    pub name: String,
    /// The name the plugin declares. Its service publishes functions as `<declared>_<verb>`.
    pub declared: String,
    pub uuid: Uuid,
    pub major: u32,
    pub services: Vec<String>,
    pub tier: Tier,
    pub implements: Vec<(Uuid, u32)>,
    pub plugin: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct PageSpec {
    pub name: String,
    pub dir: PathBuf,
    pub entry: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ResolvedTool {
    pub name: String,
    pub major: u32,
    pub plugin: String,
    pub peer: Option<String>,
}

/// What `AWM_RESOLVED_TOOLS` holds for one plugin.
#[derive(Debug, Clone, Default, Serialize)]
pub struct Resolved {
    pub tools: BTreeMap<Uuid, ResolvedTool>,
    pub contracts: BTreeMap<Uuid, Vec<ResolvedTool>>,
}

/// A tool another node serves, as its `/hub/uuids` reports it.
#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct RemoteTool {
    pub uuid: Uuid,
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
    /// Tool renames from the install config: declared name to public name.
    pub rename: &'a BTreeMap<String, String>,
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
    // tool name or uuid → (owner plugin, owner is legacy)
    let mut tool_names: HashMap<String, (String, bool)> = HashMap::new();
    let mut tool_uuids: HashMap<Uuid, (String, bool)> = HashMap::new();
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
        if let Err(reason) = claim(&plugin, &reserved, &tool_names, &tool_uuids, &service_names) {
            out.refused.push(Refusal { plugin: name, dir: cand.dir, reason });
            continue;
        }
        for t in &plugin.tools {
            tool_names.entry(t.name.clone()).or_insert((name.clone(), plugin.legacy));
            tool_uuids.entry(t.uuid).or_insert((name.clone(), plugin.legacy));
        }
        for s in &plugin.services {
            service_names.insert(s.name.clone(), name.clone());
        }
        plugins.push(plugin);
    }

    // A refused provider can strand its dependents, so resolve to a fixed point.
    loop {
        let index = tool_index(&plugins, input.peer_tools);
        let mut refused_now = Vec::new();
        for (i, p) in plugins.iter_mut().enumerate() {
            match resolve(&p.needs, &index) {
                Ok(resolved) => p.resolved = resolved,
                Err(reason) => refused_now.push((i, reason)),
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
        plugin: PluginMeta { name: name.clone(), version: "0.0.0".into(), trust: Trust::Own, refresh: None },
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
            uuid: legacy_tool_uuid(&tool),
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
            implements.push(parse_implements(raw).ok_or_else(|| format!("tool {:?}: bad implements {raw:?}", t.name))?);
        }
        tools.push(ToolSpec {
            name: input.rename.get(&t.name).cloned().unwrap_or_else(|| t.name.clone()),
            declared: t.name.clone(),
            uuid: t.uuid,
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
        trust: m.plugin.trust,
        refresh: m.plugin.refresh.clone(),
        services,
        tools,
        pages,
        needs: m.needs.clone(),
        resolved: Resolved::default(),
    })
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
    tool_uuids: &HashMap<Uuid, (String, bool)>,
    service_names: &HashMap<String, String>,
) -> Result<(), String> {
    let mut own = BTreeSet::new();
    for t in &p.tools {
        if !own.insert(&t.name) {
            return Err(format!("tool {:?} is declared twice", t.name));
        }
        if reserved.contains(t.name.as_str()) && !(p.core && p.trust == Trust::Own) {
            return Err(format!("tool {:?} is reserved for core plugins", t.name));
        }
        // Legacy folders share a tool as today (cx, keep and reflection all publish
        // `reflection`). The hub refuses a verb two services both publish.
        if let Some((owner, legacy)) = tool_names.get(&t.name) {
            if !(p.legacy && *legacy) {
                return Err(format!("tool {:?} is already served by plugin {owner:?}", t.name));
            }
        }
        if let Some((owner, legacy)) = tool_uuids.get(&t.uuid) {
            if !(p.legacy && *legacy) {
                return Err(format!("tool uuid {} is already served by plugin {owner:?}", t.uuid));
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

struct Provider {
    uuid: Uuid,
    tool: ResolvedTool,
    implements: Vec<(Uuid, u32)>,
}

fn tool_index(plugins: &[Plugin], peers: &BTreeMap<String, Vec<RemoteTool>>) -> Vec<Provider> {
    let mut index: Vec<Provider> = Vec::new();
    for t in plugins.iter().flat_map(|p| p.tools.iter()) {
        if index.iter().any(|p| p.uuid == t.uuid) {
            continue; // a shared legacy tool resolves to its first plugin
        }
        index.push(Provider {
            uuid: t.uuid,
            tool: ResolvedTool { name: t.name.clone(), major: t.major, plugin: t.plugin.clone(), peer: None },
            implements: t.implements.clone(),
        });
    }
    for (peer, tools) in peers {
        for t in tools {
            if index.iter().any(|p| p.uuid == t.uuid) {
                continue; // a local provider wins
            }
            index.push(Provider {
                uuid: t.uuid,
                tool: ResolvedTool { name: t.name.clone(), major: t.major, plugin: t.plugin.clone(), peer: Some(peer.clone()) },
                implements: t.implements.iter().filter_map(|r| parse_implements(r)).collect(),
            });
        }
    }
    index
}

fn resolve(needs: &Needs, index: &[Provider]) -> Result<Resolved, String> {
    let mut out = Resolved::default();
    for NeedTool { uuid, major } in &needs.tool {
        let Some(p) = index.iter().find(|p| p.uuid == *uuid) else {
            return Err(format!("needs tool {uuid}, which no plugin serves"));
        };
        if p.tool.major != *major {
            return Err(format!("needs tool {uuid} ({}) at major {major}, served at {}", p.tool.name, p.tool.major));
        }
        out.tools.insert(*uuid, p.tool.clone());
    }
    for NeedTool { uuid, major } in &needs.contract {
        let providers = index
            .iter()
            .filter(|p| p.implements.contains(&(*uuid, *major)))
            .map(|p| p.tool.clone())
            .collect();
        out.contracts.insert(*uuid, providers);
    }
    Ok(out)
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
        let reserved = vec!["2fa".to_string()];
        let rename = BTreeMap::from([("clash".to_string(), "clash-two".to_string())]);
        let wants = move |p: &str| p != "elsewhere" || node == "altair";
        let enabled = HashMap::from([("off-svc".to_string(), false)]);
        let peers = BTreeMap::new();
        load(&LoadInput {
            wants: &wants,
            rename: &rename,
            core_roots: &[core.to_path_buf()],
            plugin_roots: &[other.to_path_buf()],
            page_roots: &[],
            profiles: &["gamebot".to_string()],
            reserved: &reserved,
            enabled: &enabled,
            peer_tools: &peers,
        })
    }

    const U1: &str = "11111111-1111-4111-8111-111111111111";
    const U2: &str = "22222222-2222-4222-8222-222222222222";
    const U3: &str = "33333333-0000-4000-8000-000000000003";
    const C1: &str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

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
        plugin(&core, "provider", &format!(
            "[plugin]\nname='provider'\n[[service]]\nname='provider'\n[[tool]]\nname='prov'\nuuid='{U1}'\nmajor=2\nimplements=['{C1}@1']\n"));
        plugin(&core, "dependent", &format!(
            "[plugin]\nname='dependent'\n[[service]]\nname='dependent'\n[[needs.tool]]\nuuid='{U1}'\nmajor=2\n[[needs.contract]]\nuuid='{C1}'\nmajor=1\n"));
        plugin(&other, "journal", "[plugin]\nname='journal'\n");
        plugin(&other, "dup-name", &format!("[plugin]\nname='dup-name'\n[[service]]\nname='dup-name'\n[[tool]]\nname='prov'\nuuid='{U2}'\n"));
        plugin(&other, "dup-uuid", &format!("[plugin]\nname='dup-uuid'\n[[service]]\nname='dup-uuid'\n[[tool]]\nname='other'\nuuid='{U1}'\n"));
        plugin(&other, "takes-2fa", &format!("[plugin]\nname='takes-2fa'\n[[service]]\nname='takes-2fa'\n[[tool]]\nname='2fa'\nuuid='{U2}'\n"));
        plugin(&other, "missing", "[plugin]\nname='missing'\n[[needs.tool]]\nuuid='33333333-3333-4333-8333-333333333333'\n");
        plugin(&other, "wrong-major", &format!("[plugin]\nname='wrong-major'\n[[needs.tool]]\nuuid='{U1}'\nmajor=1\n"));
        plugin(&other, "escape", "[plugin]\nname='escape'\n[[service]]\nname='escape'\npath='..'\n");
        plugin(&other, "elsewhere", "[plugin]\nname='elsewhere'\n[placement]\nnodes=['showtime']\n");
        plugin(&core, "clash1", &format!("[plugin]\nname='clash1'\n[[service]]\nname='clash1'\n[[tool]]\nname='clash'\nuuid='{U3}'\n"));
        let two = core.join("ops");
        fs::create_dir_all(two.join("board")).unwrap();
        fs::create_dir_all(two.join("reporting")).unwrap();
        fs::write(two.join("board/run.sh"), "").unwrap();
        fs::write(two.join("reporting/run.sh"), "").unwrap();
        fs::write(two.join(MANIFEST_FILE), "[plugin]\nname='ops'\n[[service]]\nname='board'\npath='board'\n[[service]]\nname='reporting'\npath='reporting'\n[[tool]]\nname='operations'\nuuid='44444444-4444-4444-8444-444444444444'\nservices=['board','reporting']\n").unwrap();
        plugin(&other, "steals-reflection", "[plugin]\nname='steals-reflection'\n[[service]]\nname='steals-reflection'\n[[tool]]\nname='reflection'\nuuid='55555555-5555-4555-8555-555555555555'\n");
        plugin(&other, "windows-only", "[plugin]\nname='windows-only'\n[needs]\nplatform=['windows']\n");
        plugin(&other, "bad-toml", "[plugin]\nname='bad-toml'\nbogus=1\n");

        let r = run(&core, &other, "mizar");
        let names: Vec<_> = r.plugins.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, ["clash1", "cx", "dependent", "game", "journal", "keep", "off-svc", "ops", "provider"]);
        let ops = r.plugins.iter().find(|p| p.name == "ops").unwrap();
        assert!(ops.services.iter().all(|s| s.tools == ["operations"]));
        let clash = &r.plugins[0].tools[0];
        assert_eq!((clash.name.as_str(), clash.declared.as_str()), ("clash-two", "clash"));
        let refused: BTreeMap<_, _> = r.refused.iter().map(|x| (x.plugin.as_str(), x.reason.as_str())).collect();
        assert!(refused["dup-name"].contains("already served by plugin \"provider\""));
        assert!(refused["dup-uuid"].contains("uuid"));
        assert!(refused["takes-2fa"].contains("reserved"));
        assert!(refused["missing"].contains("33333333-3333-4333-8333-333333333333"));
        assert!(refused["wrong-major"].contains("major 1"));
        assert!(refused["escape"].contains("leaves the plugin folder"));
        assert!(refused.contains_key("bad-toml"));
        assert!(refused["windows-only"].contains("platform"));
        assert!(refused["steals-reflection"].contains("already served"));
        assert_eq!(r.skipped, ["elsewhere"]);
        assert_eq!(r.shadowed.len(), 1);

        let by = |n: &str| r.plugins.iter().find(|p| p.name == n).unwrap();
        assert_eq!(by("cx").tools[0].name, "reflection");
        assert_eq!(by("cx").tools[0].uuid, legacy_tool_uuid("reflection"));
        assert!(!by("game").services[0].enabled);
        assert!(!by("off-svc").services[0].enabled);
        assert!(by("journal").services[0].enabled);
        let dep = &by("dependent").resolved;
        assert_eq!(dep.tools[&U1.parse().unwrap()].name, "prov");
        assert_eq!(dep.contracts[&C1.parse().unwrap()][0].name, "prov");

        let r = run(&core, &other, "altair");
        assert!(r.plugins.iter().any(|p| p.name == "elsewhere"));
        assert!(r.warnings.iter().any(|w| w.contains("R140")));
    }

    #[test]
    fn refused_provider_cascades() {
        let t = tempfile::tempdir().unwrap();
        let (core, other) = (t.path().join("core"), t.path().join("other"));
        fs::create_dir_all(&core).unwrap();
        plugin(&other, "a", &format!("[plugin]\nname='a'\n[[service]]\nname='a'\n[[tool]]\nname='a'\nuuid='{U1}'\n[[needs.tool]]\nuuid='{U2}'\n"));
        plugin(&other, "b", &format!("[plugin]\nname='b'\n[[needs.tool]]\nuuid='{U1}'\n"));
        let r = run(&core, &other, "mizar");
        assert!(r.plugins.is_empty());
        assert_eq!(r.refused.len(), 2);
    }
}
