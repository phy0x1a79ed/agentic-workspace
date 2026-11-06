//! The awm tree's own plugin roots load with no refusal: a manifest the loader refuses fails
//! here before it reaches a node.

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

use awm_gateway::loader::{load, LoadInput};

#[test]
fn every_plugin_of_the_tree_loads() {
    let awm = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../awm");
    let roots = awm_gateway::config::tree_roots(&awm);
    let profiles = ["claude-science", "gamebot"].map(String::from);
    let reserved: Vec<String> = awm_gateway::config::DEFAULT_RESERVED.iter().map(|s| s.to_string()).collect();
    let got = load(&LoadInput {
        wants: &|_| true,
        core_roots: &roots,
        plugin_roots: &[],
        profiles: &profiles,
        reserved: &reserved,
        enabled: &HashMap::new(),
        peer_tools: &BTreeMap::new(),
        doors: &[],
        elsewhere: &BTreeMap::new(),
    });
    let refused: Vec<String> = got.refused.iter().map(|r| format!("{}: {}", r.plugin, r.reason)).collect();
    assert!(refused.is_empty(), "refused:\n{}", refused.join("\n"));
    assert!(got.plugins.len() > 30, "found only {} plugins", got.plugins.len());
}

/// alcor serves operations and reaches trilium only through deneb's door (fleet.md R89). With
/// no trilium plugin here and the door unasked, no plugin of the tree is refused.
#[test]
fn a_singleton_behind_a_door_strands_no_plugin() {
    let awm = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../awm");
    let roots = awm_gateway::config::tree_roots(&awm);
    let profiles = ["claude-science", "gamebot"].map(String::from);
    let reserved: Vec<String> = awm_gateway::config::DEFAULT_RESERVED.iter().map(|s| s.to_string()).collect();
    let got = load(&LoadInput {
        wants: &|p| p != "trilium",
        core_roots: &roots,
        plugin_roots: &[],
        profiles: &profiles,
        reserved: &reserved,
        enabled: &HashMap::new(),
        peer_tools: &BTreeMap::new(),
        doors: &["trilium".to_string()],
        elsewhere: &BTreeMap::new(),
    });
    let refused: Vec<String> = got.refused.iter().map(|r| format!("{}: {}", r.plugin, r.reason)).collect();
    assert!(refused.is_empty(), "refused:\n{}", refused.join("\n"));
    assert!(got.plugins.iter().any(|p| p.name == "operations"), "operations loads");
}

/// deneb runs no agent session and opens no outbound ssh (fleet.md R79): no door, no edge, no
/// peer. Its plugins load on their own, so a need on alcor's singletons or on an agent tool
/// is a contract that may have no provider. Keep the list in step with cloud's
/// homelab/inventory/host_vars/deneb.yml `awm_plugins`.
#[test]
fn denebs_plugins_load_with_no_door_and_no_peer() {
    const DENEB: [&str; 7] = ["auth", "board", "fileviewer", "httpsfront", "relay", "trilium", "zotero"];
    let awm = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../awm");
    let roots = awm_gateway::config::tree_roots(&awm);
    let reserved: Vec<String> = awm_gateway::config::DEFAULT_RESERVED.iter().map(|s| s.to_string()).collect();
    let got = load(&LoadInput {
        wants: &|p| DENEB.contains(&p),
        core_roots: &roots,
        plugin_roots: &[],
        profiles: &[],
        reserved: &reserved,
        enabled: &HashMap::new(),
        peer_tools: &BTreeMap::new(),
        doors: &[],
        elsewhere: &BTreeMap::new(),
    });
    let refused: Vec<String> = got.refused.iter().map(|r| format!("{}: {}", r.plugin, r.reason)).collect();
    assert!(refused.is_empty(), "refused:\n{}", refused.join("\n"));
    let mut loaded: Vec<&str> = got.plugins.iter().map(|p| p.name.as_str()).collect();
    loaded.sort();
    assert_eq!(loaded, DENEB);
}
