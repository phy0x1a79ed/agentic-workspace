//! The awm tree's own plugin roots load with no refusal: a manifest the loader refuses fails
//! here before it reaches a node.

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

use awm_gateway::loader::{load, LoadInput};

#[test]
fn every_plugin_of_the_tree_loads() {
    let awm = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../awm");
    let roots = [awm.join("services"), awm.join("plugins")];
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
    });
    let refused: Vec<String> = got.refused.iter().map(|r| format!("{}: {}", r.plugin, r.reason)).collect();
    assert!(refused.is_empty(), "refused:\n{}", refused.join("\n"));
    assert!(got.plugins.len() > 30, "found only {} plugins", got.plugins.len());
}
