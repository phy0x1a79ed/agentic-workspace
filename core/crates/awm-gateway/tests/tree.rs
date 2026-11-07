//! The awm tree's own plugin roots load with no refusal: a manifest the loader refuses fails
//! here before it reaches a node.

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

use awm_gateway::loader::{load, LoadInput};

#[test]
fn every_plugin_of_the_tree_loads() {
    let tree = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let roots = awm_gateway::config::tree_roots(&tree);
    let profiles = ["claude-science", "gamebot"].map(String::from);
    let reserved: Vec<String> = awm_gateway::config::DEFAULT_RESERVED.iter().map(|s| s.to_string()).collect();
    let got = load(&LoadInput {
        wants: &|_| true,
        wants_service: &|_, _| true,
        core_roots: &roots,
        plugin_roots: &[],
        profiles: &profiles,
        reserved: &reserved,
        enabled: &HashMap::new(),
        peer_tools: &BTreeMap::new(),
        doors: &[],
        elsewhere: &BTreeMap::new(),
        failed_installs: &BTreeMap::new(),
    });
    let refused: Vec<String> = got.refused.iter().map(|r| format!("{}: {}", r.plugin, r.reason)).collect();
    assert!(refused.is_empty(), "refused:\n{}", refused.join("\n"));
    assert!(got.plugins.len() > 30, "found only {} plugins", got.plugins.len());
}

/// alcor serves operations and reaches trilium only through deneb's door (fleet.md R89). With
/// no trilium plugin here and the door unasked, no plugin of the tree is refused.
#[test]
fn a_singleton_behind_a_door_strands_no_plugin() {
    let tree = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let roots = awm_gateway::config::tree_roots(&tree);
    let profiles = ["claude-science", "gamebot"].map(String::from);
    let reserved: Vec<String> = awm_gateway::config::DEFAULT_RESERVED.iter().map(|s| s.to_string()).collect();
    let got = load(&LoadInput {
        wants: &|p| p != "trilium",
        wants_service: &|_, _| true,
        core_roots: &roots,
        plugin_roots: &[],
        profiles: &profiles,
        reserved: &reserved,
        enabled: &HashMap::new(),
        peer_tools: &BTreeMap::new(),
        doors: &["trilium".to_string()],
        elsewhere: &BTreeMap::new(),
        failed_installs: &BTreeMap::new(),
    });
    let refused: Vec<String> = got.refused.iter().map(|r| format!("{}: {}", r.plugin, r.reason)).collect();
    assert!(refused.is_empty(), "refused:\n{}", refused.join("\n"));
    assert!(got.plugins.iter().any(|p| p.name == "operations"), "operations loads");
}

/// deneb runs no agent session and opens no outbound ssh (fleet.md R79): no door, no edge, no
/// peer. Its plugins load on their own, so a need on alcor's singletons or on an agent tool
/// is a contract that may have no provider. deneb runs channel's `relay` half by its
/// `relay-host` profile and excludes the member half, `channel/channel`. Keep the list in step
/// with cloud's homelab/inventory/host_vars/deneb.yml `awm_plugins`.
#[test]
fn denebs_plugins_load_with_no_door_and_no_peer() {
    const DENEB: [&str; 7] = ["auth", "board", "channel", "fileviewer", "httpsfront", "trilium", "zotero"];
    let tree = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let roots = awm_gateway::config::tree_roots(&tree);
    let reserved: Vec<String> = awm_gateway::config::DEFAULT_RESERVED.iter().map(|s| s.to_string()).collect();
    let got = load(&LoadInput {
        wants: &|p| DENEB.contains(&p),
        wants_service: &|p, s| (p, s) != ("channel", "channel"),
        core_roots: &roots,
        plugin_roots: &[],
        profiles: &["relay-host".to_string()],
        reserved: &reserved,
        enabled: &HashMap::new(),
        peer_tools: &BTreeMap::new(),
        doors: &[],
        elsewhere: &BTreeMap::new(),
        failed_installs: &BTreeMap::new(),
    });
    let refused: Vec<String> = got.refused.iter().map(|r| format!("{}: {}", r.plugin, r.reason)).collect();
    assert!(refused.is_empty(), "refused:\n{}", refused.join("\n"));
    let mut loaded: Vec<&str> = got.plugins.iter().map(|p| p.name.as_str()).collect();
    loaded.sort();
    assert_eq!(loaded, DENEB);
    let channel = got.plugins.iter().find(|p| p.name == "channel").unwrap();
    let on: Vec<&str> = channel.services.iter().filter(|s| s.enabled).map(|s| s.name.as_str()).collect();
    assert_eq!(on, ["relay"], "deneb runs the relay half only");
}
