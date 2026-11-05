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
    });
    let refused: Vec<String> = got.refused.iter().map(|r| format!("{}: {}", r.plugin, r.reason)).collect();
    assert!(refused.is_empty(), "refused:\n{}", refused.join("\n"));
    assert!(got.plugins.iter().any(|p| p.name == "operations"), "operations loads");
}
