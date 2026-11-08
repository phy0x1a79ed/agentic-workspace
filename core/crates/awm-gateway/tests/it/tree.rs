//! The awm tree's own plugin roots load with no refusal: a manifest the loader refuses fails
//! here before it reaches a node.

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

use awm_gateway::loader::{load, LoadInput, LoadResult};

fn load_tree(wants: &dyn Fn(&str) -> bool, profiles: &[String], doors: &[String]) -> LoadResult {
    let tree = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let roots = awm_gateway::config::tree_roots(&tree);
    let reserved: Vec<String> = awm_gateway::config::DEFAULT_RESERVED.iter().map(|s| s.to_string()).collect();
    let got = load(&LoadInput {
        wants,
        wants_service: &|_, _| true,
        core_roots: &roots,
        plugin_roots: &[],
        profiles,
        reserved: &reserved,
        enabled: &HashMap::new(),
        peer_tools: &BTreeMap::new(),
        doors,
        elsewhere: &BTreeMap::new(),
        failed_installs: &BTreeMap::new(),
    });
    let refused: Vec<String> = got.refused.iter().map(|r| format!("{}: {}", r.plugin, r.reason)).collect();
    assert!(refused.is_empty(), "refused:\n{}", refused.join("\n"));
    got
}

/// The whole tree loads; alcor's operations loads with trilium only behind deneb's door
/// (fleet.md R89); deneb's plugins load with no door and no peer (fleet.md R79; keep the list in
/// step with cloud's homelab/inventory/host_vars/deneb.yml `awm_plugins`).
#[test]
fn the_tree_loads_whole_behind_a_door_and_as_deneb() {
    let profiles = ["claude-science", "gamebot"].map(String::from);
    let got = load_tree(&|_| true, &profiles, &[]);
    assert!(got.plugins.len() > 30, "found only {} plugins", got.plugins.len());

    let got = load_tree(&|p| p != "trilium", &profiles, &["trilium".to_string()]);
    assert!(got.plugins.iter().any(|p| p.name == "operations"), "operations loads");

    const DENEB: [&str; 7] = ["auth", "board", "fileviewer", "httpsfront", "relay", "trilium", "zotero"];
    let got = load_tree(&|p| DENEB.contains(&p), &[], &[]);
    let mut loaded: Vec<&str> = got.plugins.iter().map(|p| p.name.as_str()).collect();
    loaded.sort();
    assert_eq!(loaded, DENEB);
}
